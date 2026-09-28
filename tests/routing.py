#!/usr/bin/env python3
"""CP05 method and routing dogfood."""

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
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


port = free_port()
source = f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":8}})
http.get(app, "/", (request) => http.text("root"))
http.get(app, "/users/:id", (request) => http.json({{"id":request.params.id,"q":request.query.q,"header":request.headers.get("x-test")[0]}}))
http.post(app, "/method", (request) => http.text("POST"))
http.put(app, "/method", (request) => http.text("PUT"))
http.patch(app, "/method", (request) => http.text("PATCH"))
http.delete(app, "/method", (request) => http.text("DELETE"))
http.listen(app)
'''
with open(os.path.join(WORK, "app.f"), "w", encoding="utf-8") as output:
    output.write(source)

process = subprocess.Popen(
    [NIFT, "app.f"], cwd=WORK,
    env={**os.environ, "NIFT_HTTP_NIFT": NIFT},
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)


def request(method, path, headers=None):
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            values = (response.status, dict(response.getheaders()), data)
            connection.close()
            return values
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


results = [
    request("GET", "/"),
    request("GET", "/users/a%20b?q=yes", {"X-Test": "present"}),
    request("POST", "/method"),
    request("PUT", "/method"),
    request("PATCH", "/method"),
    request("DELETE", "/method"),
    request("HEAD", "/"),
    request("GET", "/missing"),
]
stdout, stderr = process.communicate(timeout=15)
if process.returncode != 0:
    raise SystemExit(f"FAIL routing app: {stdout=} {stderr=}")
if results[0][0::2] != (200, b"root"):
    raise SystemExit(f"FAIL GET route: {results[0]!r}")
if results[1][0] != 200:
    process.terminate()
    raise SystemExit(f"FAIL parameter route status: {results[1]!r}")
payload = json.loads(results[1][2])
if results[1][0] != 200 or payload != {"id": "a b", "q": "yes", "header": "present"}:
    raise SystemExit(f"FAIL parameter route: {results[1]!r}")
for result, method in zip(results[2:6], (b"POST", b"PUT", b"PATCH", b"DELETE")):
    if result[0] != 200 or result[2] != method:
        raise SystemExit(f"FAIL method route: {result!r}")
if results[6][0] != 200 or results[6][2] != b"" or results[6][1].get("Content-Length") != "4":
    raise SystemExit(f"FAIL HEAD fallback: {results[6]!r}")
if results[7][0] != 404:
    raise SystemExit(f"FAIL missing route: {results[7]!r}")

print("PASS http CP05 routing")
