#!/usr/bin/env python3
"""CP13 Linux dynamic-streaming contract and lifecycle suite."""

import glob
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time


if not sys.platform.startswith("linux"):
    raise SystemExit("streaming.py currently verifies Linux/POSIX FIFO behavior")

NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, ".streaming-work")
TMP = os.path.join(WORK, "tmp")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, ".nift"), exist_ok=True)
os.makedirs(TMP, exist_ok=True)
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)
ENV = {**os.environ, "NIFT_HTTP_NIFT": NIFT, "TMPDIR": TMP, "TEMP": TMP, "TMP": TMP}

LARGE_CHUNK = b"0123456789abcdef" * 2048
BINARY_A = bytes(range(256)) * 7
BINARY_B = bytes(reversed(range(256))) * 6 + b"\x00\xffbinary\r\n"
SLOW_CHUNK = b"s" * 65536
NDJSON = b"".join(
    json.dumps({"index": i, "square": i * i}, separators=(",", ":")).encode() + b"\n"
    for i in range(100)
)
for filename, value in (
    ("large.chunk", LARGE_CHUNK),
    ("binary-a.bin", BINARY_A),
    ("binary-b.bin", BINARY_B),
    ("slow.chunk", SLOW_CHUNK),
    ("records.ndjson", NDJSON),
    ("print.txt", b"application-output" * 8192),
):
    with open(os.path.join(WORK, filename), "wb") as output:
        output.write(value)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, message, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise TimeoutError(message)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError):
        return None


def children(pid):
    result = set()
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return []
    for task in tasks:
        try:
            with open(f"/proc/{pid}/task/{task}/children", encoding="ascii") as source:
                result.update(int(value) for value in source.read().split())
        except OSError:
            pass
    return sorted(result)


def helper_child(pid):
    for child in children(pid):
        try:
            with open(f"/proc/{child}/cmdline", "rb") as source:
                if b"http_helper.py" in source.read():
                    return child
        except OSError:
            pass
    return None


def assert_no_temp_roots(label):
    roots = glob.glob(os.path.join(TMP, "nift-http-*"))
    if roots:
        raise AssertionError(f"{label} leaked temporary roots: {roots!r}")


def start_server(name, mode, routes, *, max_requests, extra="", worker_timeout_ms=15000):
    port = free_port()
    status = os.path.join(WORK, name + ".status.json")
    try:
        os.remove(status)
    except FileNotFoundError:
        pass
    pool = ',"worker_pool_size":1' if mode == "persistent" else ""
    source = f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":{max_requests},"max_concurrency":1,"worker_mode":"{mode}"{pool},"worker_timeout_ms":{worker_timeout_ms},"response_timeout_ms":10000,"shutdown_grace_ms":1200,"status_path":"{status}"{extra}}})
{routes}
http.listen(app)
'''
    filename = name + ".f"
    with open(os.path.join(WORK, filename), "w", encoding="utf-8") as output:
        output.write(source)
    server = subprocess.Popen(
        [NIFT, filename], cwd=WORK, env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    wait_for(lambda: (load_json(status) or {}).get("ready"), f"{name} did not become ready")
    return server, port, status


def finish(server, label, timeout=20):
    try:
        stdout, stderr = server.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        server.terminate()
        stdout, stderr = server.communicate(timeout=5)
        raise AssertionError(f"{label} did not stop: {stdout!r} {stderr!r}")
    if server.returncode != 0:
        raise AssertionError(f"{label} failed ({server.returncode}): {stdout!r} {stderr!r}")
    assert_no_temp_roots(label)
    return stdout, stderr


def connect_request(port, path, method="GET", receive_buffer=None):
    client = socket.create_connection(("127.0.0.1", port), timeout=15)
    if receive_buffer is not None:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
    client.sendall(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    return client


def read_until(sock, marker, buffer=b""):
    while marker not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError(f"connection closed before {marker!r}")
        buffer += chunk
    position = buffer.index(marker) + len(marker)
    return buffer[:position], buffer[position:]


def read_headers(sock):
    headers, buffered = read_until(sock, b"\r\n\r\n")
    status = int(headers.split(b" ", 2)[1])
    return status, headers, buffered


def read_chunk(sock, buffer=b""):
    line, buffer = read_until(sock, b"\r\n", buffer)
    try:
        length = int(line[:-2], 16)
    except ValueError as exc:
        raise AssertionError(f"invalid chunk length {line!r}") from exc
    while len(buffer) < length + 2:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError("connection closed inside chunk")
        buffer += chunk
    if buffer[length:length + 2] != b"\r\n":
        raise AssertionError("invalid chunk terminator")
    return buffer[:length], buffer[length + 2:]


def read_all(sock, buffered=b"", tolerate_reset=False):
    output = bytearray(buffered)
    while True:
        try:
            chunk = sock.recv(65536)
        except ConnectionResetError:
            if tolerate_reset:
                return bytes(output)
            raise
        if not chunk:
            return bytes(output)
        output.extend(chunk)


def read_stream(port, path, method="GET"):
    with connect_request(port, path, method) as client:
        status, headers, buffered = read_headers(client)
        chunks = []
        while method != "HEAD":
            try:
                chunk, buffered = read_chunk(client, buffered)
            except AssertionError as exc:
                raise AssertionError(f"{path} ended after {len(chunks)} chunks: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
        if method == "HEAD":
            buffered = read_all(client, buffered)
        return status, headers, chunks, buffered


CORE_ROUTES = '''
http.get(app, "/timed", (request) => http.stream((write) => {
    write("first\\n")
    run("sh", "-c", "sleep 0.35")
    write("second\\n")
    touch("timed.done")
}))
http.get(app, "/handler-tail", (request) => {
    response := http.stream((write) => write("body"))
    run("sh", "-c", "sleep 0.30")
    touch("handler-tail.done")
    return response
})
http.get(app, "/ndjson", (request) => http.stream((write) => {
    write(open("records.ndjson"))
}, {"content_type":"application/x-ndjson"}))
http.get(app, "/large", (request) => http.stream((write) => {
''' + "\n".join('    write(open("large.chunk"))' for _ in range(128)) + '''
}))
http.get(app, "/binary", (request) => http.stream((write) => {
    write(open("binary-a.bin"))
    write(open("binary-b.bin"))
}, {"content_type":"application/octet-stream"}))
http.get(app, "/printed", (request) => http.stream((write) => {
    print(open("print.txt"))
    write("clean")
}))
http.get(app, "/head", (request) => {
    response := http.stream((write) => {
        touch("head-invoked")
        write("must-not-run")
    })
    run("sh", "-c", "sleep 0.30")
    touch("head-tail.done")
    return response
})
http.get(app, "/partial-error", (request) => {
    response := http.stream((write) => write("partial"))
    failure := request.missing.value
    return response
})
http.get(app, "/late", (request) => {
    run("sh", "-c", "sleep 5")
    return http.stream((write) => write("too-late"))
})
http.get(app, "/idle", (request) => http.stream((write) => {
    write("first")
    run("sh", "-c", "sleep 5")
    write("too-late")
}))
http.get(app, "/slow", (request) => http.stream((write) => {
''' + "\n".join('    write(open("slow.chunk"))' for _ in range(1024)) + '''
    touch("slow.done")
}))
http.get(app, "/recovery", (request) => http.stream((write) => write("recovered")))
'''


def run_core(mode):
    for marker in ("timed.done", "handler-tail.done", "head-invoked", "head-tail.done", "slow.done"):
        try:
            os.remove(os.path.join(WORK, marker))
        except FileNotFoundError:
            pass
    server, port, status_path = start_server(f"core-{mode}", mode, CORE_ROUTES, max_requests=12)

    with connect_request(port, "/timed") as client:
        started = time.monotonic()
        status, headers, buffered = read_headers(client)
        first, buffered = read_chunk(client, buffered)
        first_at = time.monotonic()
        if os.path.exists(os.path.join(WORK, "timed.done")):
            raise AssertionError(f"{mode}: producer completed before first chunk arrived")
        second, buffered = read_chunk(client, buffered)
        second_at = time.monotonic()
        terminal, buffered = read_chunk(client, buffered)
    if status != 200 or b"Transfer-Encoding: chunked" not in headers:
        raise AssertionError(f"{mode}: invalid timed response {status} {headers!r}")
    if (first, second, terminal, buffered) != (b"first\n", b"second\n", b"", b""):
        raise AssertionError(f"{mode}: invalid timed chunks")
    if first_at - started >= 0.25 or second_at - first_at < 0.2:
        raise AssertionError(f"{mode}: streaming timing was buffered: {first_at-started:.3f}/{second_at-first_at:.3f}s")

    with connect_request(port, "/handler-tail") as client:
        started = time.monotonic()
        status, _headers, buffered = read_headers(client)
        body, buffered = read_chunk(client, buffered)
        body_at = time.monotonic()
        terminal, buffered = read_chunk(client, buffered)
        completed_at = time.monotonic()
    if status != 200 or body != b"body" or terminal or buffered:
        raise AssertionError(f"{mode}: handler-tail response was invalid")
    if body_at - started >= 0.2 or completed_at - body_at < 0.2:
        raise AssertionError(f"{mode}: stream completed before its handler returned")

    status, headers, chunks, trailing = read_stream(port, "/ndjson")
    records = [json.loads(line) for line in b"".join(chunks).splitlines()]
    if status != 200 or b"application/x-ndjson" not in headers or trailing:
        raise AssertionError(f"{mode}: invalid NDJSON framing")
    if records != [{"index": i, "square": i * i} for i in range(100)]:
        raise AssertionError(f"{mode}: NDJSON records are invalid or unordered")

    status, _headers, chunks, trailing = read_stream(port, "/large")
    if status != 200 or trailing or b"".join(chunks) != LARGE_CHUNK * 128:
        raise AssertionError(f"{mode}: generated large response mismatch")

    status, headers, chunks, trailing = read_stream(port, "/binary")
    if (status != 200 or b"application/octet-stream" not in headers or trailing
            or b"".join(chunks) != BINARY_A + BINARY_B):
        raise AssertionError(f"{mode}: file-backed arbitrary binary stream failed")

    status, _headers, chunks, trailing = read_stream(port, "/printed")
    if status != 200 or b"".join(chunks) != b"clean" or trailing:
        raise AssertionError(f"{mode}: application print contaminated stream")

    started = time.monotonic()
    status, headers, chunks, trailing = read_stream(port, "/head", "HEAD")
    head_elapsed = time.monotonic() - started
    if status != 200 or chunks or trailing or b"Transfer-Encoding: chunked" not in headers:
        raise AssertionError(f"{mode}: invalid HEAD stream response")
    if os.path.exists(os.path.join(WORK, "head-invoked")):
        raise AssertionError(f"{mode}: HEAD invoked stream producer")
    if head_elapsed < 0.2 or not os.path.exists(os.path.join(WORK, "head-tail.done")):
        raise AssertionError(f"{mode}: HEAD released its worker before handler completion")

    with connect_request(port, "/partial-error") as client:
        status, _headers, buffered = read_headers(client)
        partial, buffered = read_chunk(client, buffered)
        remainder = read_all(client, buffered, tolerate_reset=True)
    if status != 200 or partial != b"partial" or b"0\r\n\r\n" in remainder or b"HTTP/1.1 500" in remainder:
        raise AssertionError(f"{mode}: partial producer error was falsely completed: {remainder!r}")
    wait_for(
        lambda: (value := load_json(status_path))
        and value["counters"]["stream_failed"] >= 1
        and value["counters"]["errors"] >= 1,
        f"{mode}: partial stream failure was not observable",
    )
    failure_status = load_json(status_path)
    if failure_status["counters"]["errors"] < 1:
        raise AssertionError(f"{mode}: partial stream failure did not increment errors")

    accepted_before = (load_json(status_path) or {})["counters"]["accepted"]
    late = connect_request(port, "/late")
    late.close()
    wait_for(
        lambda: (value := load_json(status_path))
        and value["counters"]["accepted"] > accepted_before
        and value.get("active_requests") == 0,
        f"{mode}: orderly disconnect before metadata did not cancel the worker",
        timeout=2,
    )

    idle = connect_request(port, "/idle")
    status, _headers, buffered = read_headers(idle)
    first, buffered = read_chunk(idle, buffered)
    if status != 200 or first != b"first":
        raise AssertionError(f"{mode}: idle stream did not begin")
    idle.close()
    wait_for(
        lambda: (load_json(status_path) or {}).get("active_requests") == 0,
        f"{mode}: orderly disconnect during an idle stream was not cancelled",
        timeout=2,
    )

    slow = connect_request(port, "/slow", receive_buffer=4096)
    status, _headers, _buffered = read_headers(slow)
    if status != 200:
        raise AssertionError(f"{mode}: slow stream did not start")
    time.sleep(0.35)
    snapshot = load_json(status_path) or {}
    if os.path.exists(os.path.join(WORK, "slow.done")):
        raise AssertionError(f"{mode}: producer completed despite blocked slow reader")
    if snapshot.get("active_requests") != 1 or snapshot.get("active_workers") != 1:
        raise AssertionError(f"{mode}: blocked stream was not bounded as one active request: {snapshot!r}")
    slow.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    slow.close()
    wait_for(lambda: (load_json(status_path) or {}).get("active_requests") == 0,
             f"{mode}: disconnect was not cleaned up", timeout=5)
    if os.path.exists(os.path.join(WORK, "slow.done")):
        raise AssertionError(f"{mode}: disconnected producer reached completion marker")

    status, _headers, chunks, trailing = read_stream(port, "/recovery")
    if status != 200 or b"".join(chunks) != b"recovered" or trailing:
        raise AssertionError(f"{mode}: server did not recover after disconnect")
    finish(server, f"core-{mode}")


LIMIT_ROUTES = '''
http.get(app, "/chunk", (request) => http.stream((write) => write(open("limit-over.bin"))))
http.get(app, "/aggregate", (request) => http.stream((write) => {
    write(open("limit.bin"))
    write(open("limit.bin"))
    write(open("limit.bin"))
}))
http.get(app, "/recovery", (request) => http.stream((write) => write("ok")))
'''


def run_limits(mode):
    with open(os.path.join(WORK, "limit.bin"), "wb") as output:
        output.write(b"l" * 1024)
    with open(os.path.join(WORK, "limit-over.bin"), "wb") as output:
        output.write(b"x" * 1025)
    server, port, status_path = start_server(
        f"limits-{mode}", mode, LIMIT_ROUTES, max_requests=3,
        extra=',"max_stream_chunk_bytes":1024,"max_stream_response_bytes":2048',
    )
    status, _headers, chunks, trailing = read_stream(port, "/chunk")
    if status != 200 or trailing or b"".join(chunks) != b"x" * 1025:
        raise AssertionError(f"{mode}: helper chunking changed stream bytes")
    wait_for(lambda: (load_json(status_path) or {}).get("active_requests") == 0,
             f"{mode}: chunk-limit request did not clean up")
    with connect_request(port, "/aggregate") as client:
        status, _headers, buffered = read_headers(client)
        aggregate_wire = read_all(client, buffered, tolerate_reset=True)
    expected = b"400\r\n" + b"l" * 1024 + b"\r\n"
    if (status != 200 or aggregate_wire not in (b"", expected, expected + expected)
            or b"0\r\n\r\n" in aggregate_wire):
        raise AssertionError(
            f"{mode}: aggregate stream limit framing failed: status={status}, "
            f"wire={len(aggregate_wire)} bytes",
        )
    wait_for(
        lambda: (load_json(status_path) or {}).get("active_requests") == 0,
        f"{mode}: aggregate-limit request did not clean up",
    )
    status, _headers, chunks, trailing = read_stream(port, "/recovery")
    if status != 200 or b"".join(chunks) != b"ok" or trailing:
        raise AssertionError(f"{mode}: aggregate-limit failure did not recover")
    finish(server, f"limits-{mode}")


EDGE_ROUTES = '''
http.get(app, "/deadline", (request) => http.stream((write) => {
    write("partial")
    run("sh", "-c", "sleep 5")
}))
http.get(app, "/204", (request) => http.stream((write) => touch("bodyless-204"), {"status":204}))
http.get(app, "/205", (request) => http.stream((write) => touch("bodyless-205"), {"status":205}))
http.get(app, "/304", (request) => http.stream((write) => touch("bodyless-304"), {"status":304}))
http.get(app, "/recovery", (request) => http.stream((write) => write("ok")))
'''


def run_edges(mode):
    for marker in ("bodyless-204", "bodyless-205", "bodyless-304"):
        try:
            os.remove(os.path.join(WORK, marker))
        except FileNotFoundError:
            pass
    server, port, status_path = start_server(
        f"edges-{mode}", mode, EDGE_ROUTES, max_requests=5, worker_timeout_ms=400,
    )
    with connect_request(port, "/deadline") as client:
        status, _headers, buffered = read_headers(client)
        partial, buffered = read_chunk(client, buffered)
        remainder = read_all(client, buffered, tolerate_reset=True)
    if status != 200 or partial != b"partial" or b"0\r\n\r\n" in remainder:
        raise AssertionError(f"{mode}: stream deadline was falsely completed")
    wait_for(
        lambda: (value := load_json(status_path)) and value["counters"]["stream_failed"] >= 1,
        f"{mode}: stream deadline was not observable",
    )
    for code in (204, 205, 304):
        with connect_request(port, "/" + str(code)) as client:
            returned, _headers, buffered = read_headers(client)
            body = read_all(client, buffered)
        if returned != 500 or body != b"invalid application response status" and b"stream" not in body:
            raise AssertionError(f"{mode}: streamed {code} was not rejected: {returned} {body!r}")
        if os.path.exists(os.path.join(WORK, "bodyless-" + str(code))):
            raise AssertionError(f"{mode}: streamed {code} invoked its producer")
        wait_for(
            lambda: (load_json(status_path) or {}).get("active_requests") == 0,
            f"{mode}: streamed {code} did not clean up",
        )
    status, _headers, chunks, trailing = read_stream(port, "/recovery")
    if status != 200 or b"".join(chunks) != b"ok" or trailing:
        raise AssertionError(f"{mode}: edge failures did not recover")
    finish(server, f"edges-{mode}")


SHUTDOWN_ROUTES = '''
http.get(app, "/graceful", (request) => http.stream((write) => {
    write("begin")
    run("sh", "-c", "sleep 0.35")
    write("end")
}))
http.get(app, "/forced", (request) => http.stream((write) => {
    write("begin")
    run("sh", "-c", "sleep 30")
    write("unreachable")
}))
'''


def run_shutdown(mode, forced):
    label = ("forced" if forced else "graceful") + "-" + mode
    server, port, _status = start_server(label, mode, SHUTDOWN_ROUTES, max_requests=0)
    client = connect_request(port, "/forced" if forced else "/graceful")
    status, _headers, buffered = read_headers(client)
    first, buffered = read_chunk(client, buffered)
    if status != 200 or first != b"begin":
        raise AssertionError(f"{label}: stream did not begin")
    helper = wait_for(lambda: helper_child(server.pid), f"{label}: helper not found")
    started = time.monotonic()
    os.kill(helper, signal.SIGTERM)
    if forced:
        time.sleep(0.05)
        os.kill(helper, signal.SIGTERM)
        remainder = read_all(client, buffered, tolerate_reset=True)
        if b"0\r\n\r\n" in remainder or b"unreachable" in remainder:
            raise AssertionError(f"{label}: forced shutdown completed stream")
    else:
        second, buffered = read_chunk(client, buffered)
        terminal, buffered = read_chunk(client, buffered)
        if second != b"end" or terminal or buffered:
            raise AssertionError(f"{label}: graceful shutdown truncated stream")
    client.close()
    finish(server, label, timeout=6)
    elapsed = time.monotonic() - started
    if forced and elapsed >= 3:
        raise AssertionError(f"{label}: forced shutdown took {elapsed:.3f}s")


for worker_mode in ("oneshot", "persistent"):
    run_core(worker_mode)
    run_limits(worker_mode)
    run_edges(worker_mode)
    run_shutdown(worker_mode, forced=False)
    run_shutdown(worker_mode, forced=True)

assert_no_temp_roots("suite")
print("PASS http CP13 bounded dynamic streaming (oneshot, persistent)")
