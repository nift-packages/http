#!/usr/bin/env python3
"""CP06 deterministic minimum HTTP semantics and lifecycle suite."""

import glob
import http.client
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time


NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, ".dogfood-work")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, ".nift"), exist_ok=True)
os.makedirs(os.path.join(WORK, "tmp"), exist_ok=True)
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)

# The helper must reject non-finite values rather than emitting invalid JSON.
helper_spec = importlib.util.spec_from_file_location("nift_http_helper", os.path.join(PKG, "helper", "http_helper.py"))
helper_module = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper_module)
try:
    helper_module.response_parts({"status": 200, "headers": {}, "body": {"kind": "json", "value": float("nan")}})
except helper_module.HttpError:
    pass
else:
    raise SystemExit("FAIL non-finite JSON response was accepted")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def environment(extra=None):
    values = {
        **os.environ,
        "NIFT_HTTP_NIFT": NIFT,
        "TMPDIR": os.path.join(WORK, "tmp"),
        "TEMP": os.path.join(WORK, "tmp"),
        "TMP": os.path.join(WORK, "tmp"),
    }
    values.update(extra or {})
    return values


def request(port, method, path, body=None, headers=None, retries=True):
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = (response.status, response.getheaders(), response.read())
            connection.close()
            return result
        except OSError:
            if not retries or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def raw_request(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
        client.sendall(payload)
        output = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            output.extend(chunk)
    status = int(bytes(output).split(b" ", 2)[1])
    return status, bytes(output)


def assert_response(result, status, body=None):
    if result[0] != status or (body is not None and result[2] != body):
        raise AssertionError(f"unexpected response: {result!r}")


port = free_port()
source = f'''@import("http")
app := http.server({{
    "host":"127.0.0.1",
    "port":{port},
    "max_requests":38,
    "max_request_line":256,
    "max_header_bytes":512,
    "max_headers":20,
    "max_body_bytes":64,
    "client_timeout_ms":200,
    "worker_timeout_ms":500
}})
http.get(app, "/", (request) => http.text("hello"))
http.post(app, "/echo", (request) => {{
    print("application log outside protocol")
    return http.text(request.body.text)
}})
http.post(app, "/json", (request) => http.json({{"received":request.json.value}}))
http.get(app, "/items/:id", (request) => http.json({{
    "id":request.params.id,
    "q":request.query.q,
    "header":request.headers.get("x-test")[0]
}}, {{"status":201,"headers":{{"x-created":"yes","Content-Type":"application/vnd.nift+json"}}}}))
http.post(app, "/worker-error", (request) => request.missing.value)
http.get(app, "/worker-timeout", (request) => {{
    while(true) {{}}
    return http.text("unreachable")
}})
http.get(app, "/invalid-response", (request) => http.text("bad", {{"headers":{{"x-bad":"snowman ☃"}}}}))
http.get(app, "/no-content", (request) => http.text("must not be sent", {{"status":204}}))
http.get(app, "/descendant", (request) => {{
    run("sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $! > descendant.pid")
    return http.text("spawned")
}})
if(getenv("NIFT_HTTP_WORKER") != "1") {{
    print(http.server_backend(app))
    print(http.backend())
}}
result := http.listen(app)
if(getenv("NIFT_HTTP_WORKER") != "1") {{ print(result.ok) }}
'''
with open(os.path.join(WORK, "app.f"), "w", encoding="utf-8") as output:
    output.write(source)

server = subprocess.Popen(
    [NIFT, "app.f"], cwd=WORK, env=environment(),
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
try:
    assert_response(request(port, "GET", "/"), 200, b"hello")
    assert_response(request(port, "POST", "/echo", b"echo body", {"Content-Type": "text/plain"}), 200, b"echo body")
    json_result = request(port, "POST", "/json", b'{"value":"ok"}', {"Content-Type": "application/json"})
    assert_response(json_result, 200, b'{"received":"ok"}')
    inspect = request(port, "GET", "/items/a%20b?q=yes", headers={"X-Test": "present"})
    assert_response(inspect, 201, b'{"id":"a b","q":"yes","header":"present"}')
    if dict((key.lower(), value) for key, value in inspect[1]).get("x-created") != "yes":
        raise AssertionError(f"missing custom header: {inspect!r}")
    content_types = [value for key, value in inspect[1] if key.lower() == "content-type"]
    if content_types != ["application/vnd.nift+json"]:
        raise AssertionError(f"custom Content-Type did not override default: {inspect!r}")
    assert_response(request(port, "GET", "/missing"), 404, b"not found")
    mismatch = request(port, "POST", "/")
    assert_response(mismatch, 405, b"method not allowed")
    if dict((key.lower(), value) for key, value in mismatch[1]).get("allow") != "GET, HEAD":
        raise AssertionError(f"missing Allow header: {mismatch!r}")
    assert_response(request(port, "GET", "/"), 200, b"hello")
    assert_response(request(port, "GET", "/"), 200, b"hello")
    malformed = raw_request(port, b"NOT HTTP\r\n\r\n")
    if malformed[0] != 400:
        raise AssertionError(f"malformed request was accepted: {malformed!r}")
    duplicate = raw_request(port, b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n")
    if duplicate[0] != 400:
        raise AssertionError(f"duplicate framing was accepted: {duplicate!r}")
    assert_response(request(port, "POST", "/json", b"{", {"Content-Type": "application/json"}), 400, b"malformed JSON body")
    too_large = raw_request(port, b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 65\r\n\r\n" + b"x" * 65)
    if too_large[0] != 413:
        raise AssertionError(f"oversized body was accepted: {too_large!r}")
    assert_response(request(port, "POST", "/worker-error"), 500, b"application worker failed")
    assert_response(request(port, "GET", "/worker-timeout"), 504, b"application worker timed out")
    invalid_length = raw_request(port, b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: \xb2\r\n\r\n")
    if invalid_length[0] != 400:
        raise AssertionError(f"invalid content length was accepted: {invalid_length!r}")
    huge_length = raw_request(port, b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: " + b"9" * 100 + b"\r\n\r\n")
    if huge_length[0] != 413:
        raise AssertionError(f"huge content length was accepted: {huge_length!r}")
    if request(port, "GET", "/?q=%ZZ")[0] != 400:
        raise AssertionError("invalid query escape was accepted")
    assert_response(request(port, "POST", "/json", b'{"value":NaN}', {"Content-Type": "application/json"}), 400, b"malformed JSON body")
    assert_response(request(port, "POST", "/json", b"", {"Content-Type": "application/json"}), 400, b"malformed JSON body")
    coalesced_body = b"x" * 64
    coalesced = raw_request(
        port,
        b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Type: text/plain\r\nX-Pad: " + b"p" * 390 +
        b"\r\nContent-Length: 64\r\n\r\n" + coalesced_body,
    )
    if coalesced[0] != 200 or not coalesced[1].endswith(coalesced_body):
        raise AssertionError(f"coalesced body counted as headers: {coalesced!r}")
    assert_response(request(port, "GET", "/invalid-response"), 500, b"invalid application response header value")
    no_content = request(port, "GET", "/no-content")
    if no_content[0] != 204 or no_content[2] != b"" or any(key.lower() == "content-length" for key, _ in no_content[1]):
        raise AssertionError(f"invalid 204 framing: {no_content!r}")
    assert_response(request(port, "GET", "/descendant"), 200, b"spawned")
    with open(os.path.join(WORK, "descendant.pid"), encoding="ascii") as source_file:
        descendant_pid = int(source_file.read().strip())
    descendant_deadline = time.monotonic() + 2
    while time.monotonic() < descendant_deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"worker descendant survived: {descendant_pid}")
    if raw_request(port, b"GET / HTTP/1.1\r\n\r\n")[0] != 400:
        raise AssertionError("missing Host was accepted")
    if raw_request(port, b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")[0] != 501:
        raise AssertionError("transfer encoding was accepted")
    if raw_request(port, b"GET / HTTP/1.1\r\nHost: x\r\n folded: bad\r\n\r\n")[0] != 400:
        raise AssertionError("folded header was accepted")
    if raw_request(port, b"GET /" + b"x" * 260 + b" HTTP/1.1\r\nHost: x\r\n\r\n")[0] != 414:
        raise AssertionError("long request line was accepted")
    many_headers = b"GET / HTTP/1.1\r\nHost: x\r\n" + b"".join(
        f"X-{index}: x\r\n".encode() for index in range(21)
    ) + b"\r\n"
    if raw_request(port, many_headers)[0] != 431:
        raise AssertionError("header count limit was not enforced")
    reset = socket.create_connection(("127.0.0.1", port), timeout=2)
    reset.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    reset.sendall(b"BAD\r\n\r\n")
    reset.close()
    assert_response(request(port, "GET", "/"), 200, b"hello")
    slow = socket.create_connection(("127.0.0.1", port), timeout=2)
    slow.sendall(b"GET / HTTP/1.1\r\n")
    time.sleep(0.3)
    slow_result = bytearray()
    while True:
        chunk = slow.recv(4096)
        if not chunk:
            break
        slow_result.extend(chunk)
    slow.close()
    if not bytes(slow_result).startswith(b"HTTP/1.1 408 "):
        raise AssertionError(f"elapsed client deadline not enforced: {slow_result!r}")
    if raw_request(port, b"GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n")[0] != 400:
        raise AssertionError("duplicate Host was accepted")
    if raw_request(port, b"GET /%ZZ HTTP/1.1\r\nHost: x\r\n\r\n")[0] != 400:
        raise AssertionError("invalid path escape was accepted")
    if raw_request(port, b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n\r\n\xff")[0] != 415:
        raise AssertionError("binary body was accepted as text")
    lower_method = raw_request(port, b"get / HTTP/1.1\r\nHost: x\r\n\r\n")
    if lower_method[0] != 405:
        raise AssertionError("lowercase HTTP method incorrectly matched GET")
    head_error = raw_request(port, b"HEAD / HTTP/1.1\r\n\r\n")
    if head_error[0] != 400 or not head_error[1].endswith(b"\r\n\r\n"):
        raise AssertionError(f"HEAD parser error emitted a body: {head_error!r}")
    if raw_request(port, b"GET / HTTP/1.1\r\nHost: x\r\nX-Large: " + b"x" * 600 + b"\r\n\r\n")[0] != 431:
        raise AssertionError("header byte limit was not enforced")
    if raw_request(port, b"GET / HTTP/1.1\r\nHost: x\r\nBad Header: x\r\n\r\n")[0] != 400:
        raise AssertionError("invalid header name was accepted")
finally:
    try:
        stdout, stderr = server.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        server.terminate()
        stdout, stderr = server.communicate(timeout=5)

if server.returncode != 0 or stdout.strip().splitlines() != ["process", "process", "true"]:
    raise SystemExit(f"FAIL server lifecycle: rc={server.returncode} stdout={stdout!r} stderr={stderr!r}")
if glob.glob(os.path.join(WORK, "tmp", "nift-http-*")):
    raise SystemExit("FAIL helper temporary root leaked after normal shutdown")

# A helper whose Nift parent is terminated must observe parent death and exit.
shutdown_port = free_port()
shutdown_source = source.replace(f'"port":{port}', f'"port":{shutdown_port}').replace('"max_requests":38', '"max_requests":0')
with open(os.path.join(WORK, "shutdown.f"), "w", encoding="utf-8") as output:
    output.write(shutdown_source)
shutdown = subprocess.Popen([NIFT, "shutdown.f"], cwd=WORK, env=environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
assert_response(request(shutdown_port, "GET", "/"), 200, b"hello")
shutdown.terminate()
shutdown.wait(timeout=5)
time.sleep(0.6)
if glob.glob(os.path.join(WORK, "tmp", "nift-http-*")):
    raise SystemExit("FAIL helper survived parent shutdown or leaked its temporary root")

# Helper startup failure is returned as a stable package error.
blocked_port = free_port()
blocker = socket.socket()
blocker.bind(("127.0.0.1", blocked_port))
blocker.listen(1)
failure_source = f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{blocked_port}}})
result := http.listen(app)
print(result.ok)
print(result.error_code)
print(result.backend)
'''
with open(os.path.join(WORK, "failure.f"), "w", encoding="utf-8") as output:
    output.write(failure_source)
failure = subprocess.run([NIFT, "failure.f"], cwd=WORK, env=environment(), capture_output=True, text=True, timeout=10)
blocker.close()
if failure.returncode != 0 or failure.stdout.strip().splitlines() != ["false", "helper_failed", "process"]:
    raise SystemExit(f"FAIL helper failure contract: {failure.stdout!r} {failure.stderr!r}")
if glob.glob(os.path.join(WORK, "tmp", "nift-http-*")):
    raise SystemExit("FAIL helper temporary root leaked after bind failure")

# Disabled process execution is unavailable before launch and remains pinned.
disabled_source = '''@import("http")
print(http.backends().size())
app := http.server({"port":8080})
print(http.backend())
print(http.server_backend(app))
print(app.error_code)
print(http.listen(app).error_code)
'''
with open(os.path.join(WORK, "disabled.f"), "w", encoding="utf-8") as output:
    output.write(disabled_source)
disabled = subprocess.run(
    [NIFT, "disabled.f"], cwd=WORK,
    env=environment({"NIFT_NO_PROCESS": "1"}), capture_output=True, text=True,
)
if disabled.returncode != 0 or disabled.stdout.strip().splitlines() != ["0", "null", "null", "backend_unavailable", "backend_unavailable"]:
    raise SystemExit(f"FAIL disabled backend contract: {disabled.stdout!r} {disabled.stderr!r}")

print("PASS http CP06 dogfood")
