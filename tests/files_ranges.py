#!/usr/bin/env python3
"""CP09 file response, range, root-safety, and limit tests."""

import http.client
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
os.makedirs(os.path.join(WORK, "static", "nested"), exist_ok=True)
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)

payload = bytes(range(256)) * 128
with open(os.path.join(WORK, "payload.bin"), "wb") as output:
    output.write(payload)
with open(os.path.join(WORK, "empty.bin"), "wb"):
    pass
with open(os.path.join(WORK, "too-big.bin"), "wb") as output:
    output.write(b"x" * 65537)
with open(os.path.join(WORK, "static", "nested", "data.bin"), "wb") as output:
    output.write(b"rooted")
with open(os.path.join(WORK, "outside.bin"), "wb") as output:
    output.write(b"outside")
if hasattr(os, "symlink"):
    os.symlink(os.path.join(WORK, "outside.bin"), os.path.join(WORK, "static", "link.bin"))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


port = free_port()
source = f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":23,"max_file_response_bytes":65536}})
http.get(app, "/file", (request) => http.file("payload.bin", {{"content_type":"application/octet-stream","download_name":"payload.bin"}}))
http.get(app, "/empty", (request) => http.file("empty.bin"))
http.get(app, "/missing", (request) => http.file("missing.bin"))
http.get(app, "/directory", (request) => http.file("static"))
http.get(app, "/too-big", (request) => http.file("too-big.bin"))
http.get(app, "/root/:path", (request) => http.file_from("static", request.params.path))
http.listen(app)
'''
with open(os.path.join(WORK, "app.f"), "w", encoding="utf-8") as output:
    output.write(source)
server = subprocess.Popen(
    [NIFT, "app.f"], cwd=WORK, env={**os.environ, "NIFT_HTTP_NIFT": NIFT},
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)


def request(method, path, headers=None):
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            result = response.status, response.getheaders(), response.read()
            connection.close()
            return result
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def header(result, name):
    values = [value for key, value in result[1] if key.lower() == name.lower()]
    return values[-1] if values else None


try:
    full = request("GET", "/file")
    if full[0] != 200 or full[2] != payload or header(full, "Content-Length") != str(len(payload)):
        raise AssertionError(full[:2])
    if header(full, "Content-Disposition") != 'attachment; filename="payload.bin"':
        raise AssertionError(full[1])
    head = request("HEAD", "/file")
    if head[0] != 200 or head[2] != b"" or header(head, "Content-Length") != str(len(payload)):
        raise AssertionError(head)
    closed = request("GET", "/file", {"Range": "bytes=0-3"})
    if closed[0] != 206 or closed[2] != payload[:4] or header(closed, "Content-Range") != f"bytes 0-3/{len(payload)}":
        raise AssertionError(closed)
    opened = request("GET", "/file", {"Range": "bytes=5-"})
    if opened[0] != 206 or opened[2] != payload[5:]:
        raise AssertionError(opened[:2])
    suffix = request("GET", "/file", {"Range": "bytes=-4"})
    if suffix[0] != 206 or suffix[2] != payload[-4:]:
        raise AssertionError(suffix)
    clamped = request("GET", "/file", {"Range": f"bytes={len(payload)-4}-999999"})
    if clamped[0] != 206 or clamped[2] != payload[-4:]:
        raise AssertionError(clamped)
    for value in (f"bytes={len(payload)}-", "bytes=bad", "bytes=0-1,3-4", "bytes=" + "9" * 100 + "-"):
        invalid = request("GET", "/file", {"Range": value})
        if invalid[0] != 416 or header(invalid, "Content-Range") != f"bytes */{len(payload)}":
            raise AssertionError(invalid)
    head_range = request("HEAD", "/file", {"Range": "bytes=2-5"})
    if head_range[0] != 206 or head_range[2] != b"" or header(head_range, "Content-Length") != "4":
        raise AssertionError(head_range)
    empty = request("GET", "/empty")
    if empty[0] != 200 or empty[2] != b"" or header(empty, "Content-Length") != "0":
        raise AssertionError(empty)
    for path in ("/missing", "/directory"):
        if request("GET", path)[0] != 404:
            raise AssertionError(path)
    if request("GET", "/too-big")[0] != 500:
        raise AssertionError("file response size limit not enforced")
    rooted = request("GET", "/root/nested%2Fdata.bin")
    if rooted[0] != 200 or rooted[2] != b"rooted":
        raise AssertionError(rooted)
    for path in (
        "/root/..%2Foutside.bin", "/root/%2Fabsolute.bin",
        "/root/..%5Coutside.bin", "/root/%252e%252e%252foutside.bin",
        "/root/link.bin",
    ):
        result = request("GET", path)
        if result[0] != 404:
            raise AssertionError((path, result[0]))
    disconnected = socket.create_connection(("127.0.0.1", port), timeout=3)
    disconnected.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    disconnected.sendall(b"GET /file HTTP/1.1\r\nHost: x\r\n\r\n")
    disconnected.close()
    time.sleep(0.05)
    if request("GET", "/file")[0] != 200:
        raise AssertionError("server failed after download disconnect")
finally:
    stdout, stderr = server.communicate(timeout=25)

if server.returncode != 0:
    raise SystemExit(f"FAIL CP09 file server: {stdout!r} {stderr!r}")
print("PASS http CP09 files and ranges")
