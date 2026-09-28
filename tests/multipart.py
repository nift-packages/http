#!/usr/bin/env python3
"""CP08 spooled body and bounded multipart upload tests."""

import glob
import http.client
import json
import os
import shutil
import socket
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


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def multipart(boundary, parts, close=True):
    output = bytearray()
    for headers, content in parts:
        output.extend(b"--" + boundary + b"\r\n")
        for key, value in headers:
            output.extend(key.encode("ascii") + b": " + value.encode("latin-1") + b"\r\n")
        output.extend(b"\r\n")
        output.extend(content)
        output.extend(b"\r\n")
    output.extend(b"--" + boundary + (b"--\r\n" if close else b"\r\n"))
    return bytes(output)


def field(name, value):
    return [
        ("Content-Disposition", f'form-data; name="{name}"'),
    ], value.encode("utf-8")


def upload(name, filename, content, content_type="application/octet-stream", extra_headers=()):
    return [
        ("Content-Disposition", f'form-data; name="{name}"; filename="{filename}"'),
        ("Content-Type", content_type),
        *extra_headers,
    ], content


port = free_port()
source = f'''@import("http")
app := http.server({{
    "host":"127.0.0.1","port":{port},"max_requests":18,
    "max_body_bytes":1024,"max_temp_bytes":2048,"max_multipart_parts":4,
    "max_multipart_files":2,"max_part_header_bytes":256,"max_part_headers":4,
    "max_file_bytes":32,"max_filename_bytes":40,"worker_timeout_ms":300
}})
http.post(app, "/one", (request) => {{
    upload := request.files.attachment
    saved := http.save_upload(upload, "saved-one.bin")
    return http.json({{"saved":saved.ok,"form":request.form,"upload":upload}})
}})
http.post(app, "/many", (request) => {{
    docs := request.files.docs
    first := http.save_upload(docs[0], "saved-a.bin")
    second := http.save_upload(docs[1], "saved-b.bin")
    return http.json({{"ok":first.ok && second.ok,"form":request.form,"files":docs}})
}})
http.post(app, "/body", (request) => {{
    saved := http.save_body(request.body, "saved-body.bin")
    return http.json({{"saved":saved.ok,"kind":request.body.kind,"size":request.body.size}})
}})
http.get(app, "/forged", (request) => http.json({{
    "code":http.save_upload({{"kind":"upload","_upload_id":"forged"}}, "bad.bin").error_code
}}))
http.post(app, "/failure", (request) => request.missing.value)
http.post(app, "/timeout", (request) => {{ while(true) {{}} return http.text("never") }})
http.listen(app)
'''
with open(os.path.join(WORK, "app.f"), "w", encoding="utf-8") as output:
    output.write(source)

env = {
    **os.environ, "NIFT_HTTP_NIFT": NIFT,
    "TMPDIR": os.path.join(WORK, "tmp"), "TEMP": os.path.join(WORK, "tmp"),
    "TMP": os.path.join(WORK, "tmp"),
}
server = subprocess.Popen([NIFT, "app.f"], cwd=WORK, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def request(method, path, body=None, headers=None):
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = response.status, response.getheaders(), response.read()
            connection.close()
            return result
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def send_multipart(path, parts, boundary=b"nift-boundary", close=True):
    body = multipart(boundary, parts, close)
    return request("POST", path, body, {"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})


try:
    binary = b"\x00\xffbinary"
    one = send_multipart("/one", [field("title", "Example"), upload("attachment", "../evil.bin", binary)])
    one_value = json.loads(one[2])
    if one[0] != 200 or not one_value["saved"] or one_value["form"] != {"title": "Example"}:
        raise AssertionError(one)
    if one_value["upload"]["filename"] != "../evil.bin" or "path" in one_value["upload"]:
        raise AssertionError(one_value)
    if open(os.path.join(WORK, "saved-one.bin"), "rb").read() != binary:
        raise AssertionError("saved upload bytes differ")

    many = send_multipart("/many", [
        field("tag", "a"), field("tag", "b"),
        upload("docs", "same.bin", b"first"), upload("docs", "same.bin", b"second"),
    ])
    many_value = json.loads(many[2])
    if many[0] != 200 or not many_value["ok"] or many_value["form"] != {"tag": ["a", "b"]}:
        raise AssertionError(many)
    if len(many_value["files"]) != 2 or open(os.path.join(WORK, "saved-b.bin"), "rb").read() != b"second":
        raise AssertionError(many_value)

    empty = send_multipart("/one", [upload("attachment", "", b"")])
    if empty[0] != 200 or json.loads(empty[2])["upload"]["size"] != 0:
        raise AssertionError(empty)
    encoded = send_multipart("/one", [upload("attachment", "%2e%2e%2fescape.bin", b"safe")])
    if encoded[0] != 200 or json.loads(encoded[2])["upload"]["filename"] != "%2e%2e%2fescape.bin":
        raise AssertionError(encoded)

    missing_boundary = request("POST", "/one", b"body", {"Content-Type": "multipart/form-data"})
    if missing_boundary[0] != 400:
        raise AssertionError(missing_boundary)
    missing_close = send_multipart("/one", [field("x", "y")], close=False)
    if missing_close[0] != 400:
        raise AssertionError(missing_close)
    too_many = send_multipart("/one", [field(str(i), "x") for i in range(5)])
    if too_many[0] != 413:
        raise AssertionError(too_many)
    too_large = send_multipart("/one", [upload("attachment", "large.bin", b"x" * 33)])
    if too_large[0] != 413:
        raise AssertionError(too_large)
    aggregate = request("POST", "/one", b"x" * 1025, {"Content-Type": "multipart/form-data; boundary=x"})
    if aggregate[0] != 413:
        raise AssertionError(aggregate)
    large_headers = send_multipart("/one", [upload("attachment", "x", b"x", extra_headers=(("X-Pad", "x" * 260),))])
    if large_headers[0] != 400:
        raise AssertionError(large_headers)
    too_many_files = send_multipart("/one", [
        upload("a", "a", b"a"), upload("b", "b", b"b"), upload("c", "c", b"c"),
    ])
    if too_many_files[0] != 413:
        raise AssertionError(too_many_files)

    partial = socket.create_connection(("127.0.0.1", port), timeout=2)
    partial.sendall(b"POST /one HTTP/1.1\r\nHost: x\r\nContent-Type: multipart/form-data; boundary=x\r\nContent-Length: 100\r\n\r\n--x")
    partial.close()
    time.sleep(0.05)

    failure = send_multipart("/failure", [upload("attachment", "/absolute.bin", b"failure")])
    if failure[0] != 500:
        raise AssertionError(failure)
    timeout = send_multipart("/timeout", [upload("attachment", "timeout.bin", b"timeout")])
    if timeout[0] != 504:
        raise AssertionError(timeout)

    raw_binary = request("POST", "/body", b"\x00\xffraw", {"Content-Type": "application/octet-stream"})
    if raw_binary[0] != 200 or json.loads(raw_binary[2]) != {"saved": True, "kind": "spooled", "size": 5}:
        raise AssertionError(raw_binary)
    if open(os.path.join(WORK, "saved-body.bin"), "rb").read() != b"\x00\xffraw":
        raise AssertionError("saved binary body differs")
    forged = request("GET", "/forged")
    if forged[0] != 200 or json.loads(forged[2]) != {"code": "invalid_upload"}:
        raise AssertionError(forged)

    # A valid boundary-like byte sequence inside content is data, not a delimiter.
    boundary_like = send_multipart("/one", [upload("attachment", "marker.bin", b"a\r\n--nift-boundaryXb")])
    if boundary_like[0] != 200:
        raise AssertionError(boundary_like)
    # A malformed final trailer is rejected.
    malformed = request(
        "POST", "/one", b"--x\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nb\r\n--x--garbage",
        {"Content-Type": "multipart/form-data; boundary=x"},
    )
    if malformed[0] != 400:
        raise AssertionError(malformed)
finally:
    stdout, stderr = server.communicate(timeout=25)

if server.returncode != 0:
    raise SystemExit(f"FAIL CP08 server: {stdout!r} {stderr!r}")
if glob.glob(os.path.join(WORK, "tmp", "nift-http-*")):
    raise SystemExit("FAIL multipart temporary files leaked")
print("PASS http CP08 multipart and spools")
