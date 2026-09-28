#!/usr/bin/env python3
"""Review Gate 2 measurements for the one-Nift-worker-per-request model."""

import http.client
import json
import math
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time


if not sys.platform.startswith("linux"):
    raise SystemExit("gate2.py currently measures Linux /proc only")

NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, ".dogfood-work")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, ".nift"), exist_ok=True)
os.makedirs(os.path.join(WORK, "tmp"), exist_ok=True)
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)
ENV = {
    **os.environ,
    "NIFT_HTTP_NIFT": NIFT,
    "TMPDIR": os.path.join(WORK, "tmp"),
    "TEMP": os.path.join(WORK, "tmp"),
    "TMP": os.path.join(WORK, "tmp"),
}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_app(filename, port, max_requests, worker_timeout=30000):
    source = f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":{max_requests},"worker_timeout_ms":{worker_timeout}}})
http.get(app, "/", (request) => http.text("ok"))
http.get(app, "/spin", (request) => {{ while(true) {{}} return http.text("never") }})
http.listen(app)
'''
    path = os.path.join(WORK, filename)
    with open(path, "w", encoding="utf-8") as output:
        output.write(source)
    return path


def wait_connect(port, deadline=10):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        sock = socket.socket()
        sock.settimeout(0.1)
        try:
            sock.connect(("127.0.0.1", port))
            return sock
        except OSError:
            sock.close()
            time.sleep(0.002)
    raise TimeoutError("server did not listen")


def http_get(port, path="/"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    status = response.status
    connection.close()
    if status != 200 or body != b"ok":
        raise RuntimeError((status, body))


def children(pid):
    try:
        with open(f"/proc/{pid}/task/{pid}/children", encoding="ascii") as source:
            return [int(value) for value in source.read().split()]
    except OSError:
        return []


def wait_child(pid, deadline=5):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        values = children(pid)
        if values:
            return values[0]
        time.sleep(0.005)
    raise TimeoutError(f"process {pid} has no child")


def rss_kib(pid):
    with open(f"/proc/{pid}/status", encoding="ascii") as source:
        for line in source:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    raise RuntimeError(f"VmRSS unavailable for {pid}")


# User-visible startup: Nift application launch until the helper accepts TCP.
startup_ms = []
for index in range(10):
    port = free_port()
    filename = f"startup-{index}.f"
    write_app(filename, port, 1)
    started = time.perf_counter()
    process = subprocess.Popen([NIFT, filename], cwd=WORK, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    connection = wait_connect(port)
    startup_ms.append((time.perf_counter() - started) * 1000)
    connection.close()
    if process.wait(timeout=10) != 0:
        raise RuntimeError("startup probe server failed")


# One-shot worker invocation without socket/helper overhead.
worker_port = free_port()
worker_path = write_app("worker.f", worker_port, 1)
request_path = os.path.join(WORK, "request.json")
response_path = os.path.join(WORK, "response.json")
with open(request_path, "w", encoding="utf-8") as output:
    json.dump({
        "protocol": 1, "request_id": "measure", "method": "GET",
        "target": "/", "path": "/", "segments": [], "query": {},
        "headers": {"host": ["localhost"]},
        "body": {"kind": "text", "text": ""}, "json": None,
        "remote_addr": "127.0.0.1",
    }, output, separators=(",", ":"))
worker_env = {
    **ENV,
    "NIFT_HTTP_WORKER": "1",
    "NIFT_HTTP_REQUEST": request_path,
    "NIFT_HTTP_RESPONSE": response_path,
}
worker_ms = []
for _ in range(20):
    started = time.perf_counter()
    result = subprocess.run([NIFT, worker_path], cwd=WORK, env=worker_env, capture_output=True)
    worker_ms.append((time.perf_counter() - started) * 1000)
    if result.returncode != 0:
        raise SystemExit(f"worker measurement failed: {result.stderr!r}")


# Full cold response plus repeated sequential HTTP requests.
latency_port = free_port()
write_app("latency.f", latency_port, 31)
server = subprocess.Popen([NIFT, "latency.f"], cwd=WORK, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
cold_started = time.perf_counter()
while True:
    try:
        http_get(latency_port)
        break
    except OSError:
        time.sleep(0.002)
cold_response_ms = (time.perf_counter() - cold_started) * 1000
request_ms = []
for _ in range(30):
    started = time.perf_counter()
    http_get(latency_port)
    request_ms.append((time.perf_counter() - started) * 1000)
server.wait(timeout=30)


# Rough idle-helper and active-worker resident memory snapshots.
memory_port = free_port()
write_app("memory.f", memory_port, 0, 10000)
memory_server = subprocess.Popen([NIFT, "memory.f"], cwd=WORK, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while True:
    try:
        http_get(memory_port)
        break
    except OSError:
        time.sleep(0.002)
helper_pid = wait_child(memory_server.pid)
parent_rss = rss_kib(memory_server.pid)
helper_rss = rss_kib(helper_pid)
spin_error = []


def spin_request():
    try:
        connection = http.client.HTTPConnection("127.0.0.1", memory_port, timeout=15)
        connection.request("GET", "/spin")
        response = connection.getresponse()
        response.read()
        connection.close()
    except Exception as exc:  # Expected when the measured helper is terminated.
        spin_error.append(str(exc))


spin_thread = threading.Thread(target=spin_request)
spin_thread.start()
worker_pid = wait_child(helper_pid)
active_parent_rss = rss_kib(memory_server.pid)
active_helper_rss = rss_kib(helper_pid)
worker_rss = rss_kib(worker_pid)
os.kill(helper_pid, signal.SIGTERM)
spin_thread.join(timeout=5)
if spin_thread.is_alive():
    raise RuntimeError("active request survived helper shutdown")
if memory_server.wait(timeout=10) != 0:
    raise RuntimeError("Nift server parent failed after helper shutdown")
for measured_pid in (helper_pid, worker_pid):
    if os.path.exists(f"/proc/{measured_pid}"):
        raise RuntimeError(f"measured process survived shutdown: {measured_pid}")


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))]


report = {
    "platform": sys.platform,
    "iterations": {"startup": 10, "worker": 20, "requests": 30},
    "startup_ms": {
        "median": round(statistics.median(startup_ms), 3),
        "min": round(min(startup_ms), 3),
        "max": round(max(startup_ms), 3),
    },
    "worker_ms": {
        "median": round(statistics.median(worker_ms), 3),
        "p95": round(percentile(worker_ms, 0.95), 3),
    },
    "cold_response_ms": round(cold_response_ms, 3),
    "repeated_request_ms": {
        "median": round(statistics.median(request_ms), 3),
        "p95": round(percentile(request_ms, 0.95), 3),
        "min": round(min(request_ms), 3),
        "max": round(max(request_ms), 3),
    },
    "rss_kib": {
        "nift_parent_idle": parent_rss,
        "helper_idle": helper_rss,
        "worker_active": worker_rss,
        "idle_total": parent_rss + helper_rss,
        "active_total": active_parent_rss + active_helper_rss + worker_rss,
    },
}
print(json.dumps(report, indent=2, sort_keys=True))
