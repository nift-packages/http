#!/usr/bin/env python3
"""CP04 deterministic helper/worker bootstrap test."""

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
app_source = f'''@import("http")
print(http.backends().join(","))
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":1}})
if(getenv("NIFT_HTTP_WORKER") != "1") {{
    print(http.backend())
    print(http.use_backend("auto").error_code)
}}
result := http.listen(app)
if(getenv("NIFT_HTTP_WORKER") != "1") {{
    print(result.ok)
    if(!result.ok) {{ print(result.error) }}
}}
'''
app_path = os.path.join(WORK, "app.f")
with open(app_path, "w", encoding="utf-8") as output:
    output.write(app_source)

environment = {**os.environ, "NIFT_HTTP_NIFT": NIFT}
process = subprocess.Popen(
    [NIFT, "app.f"], cwd=WORK, env=environment,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
response = b""
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2) as client:
            client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        break
    except OSError:
        time.sleep(0.05)
else:
    process.kill()
    raise SystemExit("FAIL helper did not start")

stdout, stderr = process.communicate(timeout=10)
if process.returncode != 0:
    raise SystemExit(f"FAIL app exited {process.returncode}\nstdout={stdout}\nstderr={stderr}")
if not response.startswith(b"HTTP/1.1 404 ") or not response.endswith(b"not found"):
    raise SystemExit(f"FAIL unexpected bootstrap response: {response!r} stdout={stdout!r} stderr={stderr!r}")
if stdout.strip().splitlines() != ["process", "process", "backend_locked", "true"]:
    raise SystemExit(f"FAIL facade contract: {stdout!r} stderr={stderr!r}")

private_source = '@import("http")\nprint(http_helper_path())\n'
with open(os.path.join(WORK, "private.f"), "w", encoding="utf-8") as output:
    output.write(private_source)
private = subprocess.run([NIFT, "private.f"], cwd=WORK, env=environment, capture_output=True)
if private.returncode == 0:
    raise SystemExit("FAIL private helper leaked")

print("PASS http CP04 bootstrap")
