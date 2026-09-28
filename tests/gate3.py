#!/usr/bin/env python3
"""Review Gate 3 concurrency and upload/download resource measurements."""

import http.client
import json
import math
import os
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time


if not sys.platform.startswith("linux"):
    raise SystemExit("gate3.py currently measures Linux /proc only")

NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
MAX_CONCURRENCY = int(sys.argv[3]) if len(sys.argv) > 3 else 32
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


def descendants(pid):
    result = []
    pending = children(pid)
    while pending:
        child = pending.pop()
        result.append(child)
        pending.extend(children(child))
    return result


def rss_kib(pid):
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as source:
            for line in source:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def wait_child(pid, deadline=10):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        values = children(pid)
        if values:
            return values[0]
        if not os.path.exists(f"/proc/{pid}"):
            raise RuntimeError(f"process {pid} exited before creating its helper")
        time.sleep(0.002)
    raise TimeoutError(f"process {pid} has no child")


def temp_usage():
    total = 0
    files = 0
    roots = 0
    temp = os.path.join(WORK, "tmp")
    try:
        names = os.listdir(temp)
    except OSError:
        return 0, 0, 0
    for name in names:
        path = os.path.join(temp, name)
        if not name.startswith("nift-http-") or not os.path.isdir(path):
            continue
        roots += 1
        for directory, _subdirectories, filenames in os.walk(path):
            for filename in filenames:
                try:
                    total += os.path.getsize(os.path.join(directory, filename))
                    files += 1
                except OSError:
                    pass
    return total, files, roots


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))]


def request(port, method="GET", path="/", body=None, headers=None, read_delay=0):
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            chunks = []
            while True:
                chunk = response.read(16384 if read_delay else -1)
                if not chunk:
                    break
                chunks.append(chunk)
                if read_delay:
                    time.sleep(read_delay)
            result = response.status, b"".join(chunks)
            connection.close()
            return result
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.002)


def sample_topology(parent_pid, helper_pid, maxima):
    direct_workers = children(helper_pid)
    all_descendants = descendants(parent_pid)
    temporary_bytes, temporary_files, temporary_roots = temp_usage()
    maxima["parent_rss_kib"] = max(maxima["parent_rss_kib"], rss_kib(parent_pid))
    maxima["helper_rss_kib"] = max(maxima["helper_rss_kib"], rss_kib(helper_pid))
    maxima["worker_rss_kib"] = max(
        maxima["worker_rss_kib"], sum(rss_kib(pid) for pid in direct_workers),
    )
    maxima["worker_processes"] = max(maxima["worker_processes"], len(direct_workers))
    maxima["topology_processes"] = max(maxima["topology_processes"], 1 + len(all_descendants))
    maxima["topology_rss_kib"] = max(
        maxima["topology_rss_kib"],
        rss_kib(parent_pid) + sum(rss_kib(pid) for pid in all_descendants),
    )
    maxima["temporary_bytes"] = max(maxima["temporary_bytes"], temporary_bytes)
    maxima["temporary_files"] = max(maxima["temporary_files"], temporary_files)
    maxima["temporary_roots"] = max(maxima["temporary_roots"], temporary_roots)


def empty_maxima():
    return {
        "parent_rss_kib": 0,
        "helper_rss_kib": 0,
        "worker_rss_kib": 0,
        "worker_processes": 0,
        "topology_processes": 0,
        "topology_rss_kib": 0,
        "temporary_bytes": 0,
        "temporary_files": 0,
        "temporary_roots": 0,
    }


# Simultaneous-client probe. The handler delay keeps the bounded worker topology
# observable. Pass an explicit limit to compare another concurrency bound.
concurrency_port = free_port()
with open(os.path.join(WORK, "concurrency.f"), "w", encoding="utf-8") as output:
    output.write(f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{concurrency_port},"max_requests":42,"max_concurrency":{MAX_CONCURRENCY},"backlog":64}})
http.get(app, "/", (request) => {{
    run("sh", "-c", "sleep 0.04")
    return http.text("ok")
}})
http.listen(app)
''')
concurrency_server = subprocess.Popen(
    [NIFT, "concurrency.f"], cwd=WORK, env=ENV,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
concurrency_helper = wait_child(concurrency_server.pid)
if request(concurrency_port) != (200, b"ok"):
    raise RuntimeError("concurrency warm-up request failed")
concurrency_idle = {
    "parent_rss_kib": rss_kib(concurrency_server.pid),
    "helper_rss_kib": rss_kib(concurrency_helper),
}
concurrency_results = {}

for client_count in (1, 8, 32):
    barrier = threading.Barrier(client_count + 1)
    latencies = []
    errors = []

    def concurrent_request():
        try:
            barrier.wait()
            started = time.perf_counter()
            status, body = request(concurrency_port)
            latencies.append((time.perf_counter() - started) * 1000)
            if status != 200 or body != b"ok":
                errors.append(f"unexpected response {status}: {body!r}")
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=concurrent_request) for _ in range(client_count)]
    for thread in threads:
        thread.start()
    batch_maxima = empty_maxima()
    batch_started = time.perf_counter()
    barrier.wait()
    while any(thread.is_alive() for thread in threads):
        sample_topology(concurrency_server.pid, concurrency_helper, batch_maxima)
        time.sleep(0.002)
    for thread in threads:
        thread.join()
    batch_seconds = time.perf_counter() - batch_started
    if errors or len(latencies) != client_count:
        raise RuntimeError(f"concurrency {client_count}: {errors!r}")
    concurrency_results[str(client_count)] = {
        "requests": client_count,
        "errors": len(errors),
        "batch_ms": round(batch_seconds * 1000, 3),
        "throughput_requests_per_second": round(client_count / batch_seconds, 3),
        "latency_ms": {
            "median": round(statistics.median(latencies), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3),
        },
        "peak": batch_maxima,
    }

concurrency_stdout, concurrency_stderr = concurrency_server.communicate(timeout=30)
if concurrency_server.returncode != 0:
    raise RuntimeError(f"concurrency server failed: {concurrency_stdout!r} {concurrency_stderr!r}")


# Representative bounded upload and helper-streamed download resource probe.
upload_bytes = bytes(range(256)) * 4096
download_bytes = bytes(range(256)) * 16384
with open(os.path.join(WORK, "download.bin"), "wb") as output:
    output.write(download_bytes)
resource_port = free_port()
with open(os.path.join(WORK, "resources.f"), "w", encoding="utf-8") as output:
    output.write(f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{resource_port},"max_requests":2,"max_body_bytes":2097152,"max_file_bytes":1048576,"max_temp_bytes":3145728,"max_file_response_bytes":4194304}})
http.post(app, "/upload", (request) => {{
    result := http.save_upload(request.files.attachment, "saved.bin")
    run("sh", "-c", "sleep 0.15")
    return http.json({{"ok":result.ok,"size":request.files.attachment.size}})
}})
http.get(app, "/download", (request) => http.file("download.bin"))
http.listen(app)
''')
resource_server = subprocess.Popen(
    [NIFT, "resources.f"], cwd=WORK, env=ENV,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
resource_helper = wait_child(resource_server.pid)
time.sleep(0.1)
resource_idle = {
    "parent_rss_kib": rss_kib(resource_server.pid),
    "helper_rss_kib": rss_kib(resource_helper),
}
boundary = b"gate3-resource-boundary"
upload_body = (
    b"--" + boundary + b"\r\n"
    b"Content-Disposition: form-data; name=\"attachment\"; filename=\"resource.bin\"\r\n"
    b"Content-Type: application/octet-stream\r\n\r\n" + upload_bytes + b"\r\n"
    b"--" + boundary + b"--\r\n"
)
resource_results = {}


def measured_resource(name, target):
    outcome = []
    failure = []

    def run_target():
        try:
            outcome.append(target())
        except Exception as exc:
            failure.append(repr(exc))

    maxima = empty_maxima()
    thread = threading.Thread(target=run_target)
    started = time.perf_counter()
    thread.start()
    while thread.is_alive():
        sample_topology(resource_server.pid, resource_helper, maxima)
        time.sleep(0.002)
    thread.join()
    if failure or not outcome:
        raise RuntimeError(f"resource {name}: {failure!r}")
    resource_results[name] = {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "peak": maxima,
    }
    return outcome[0]


upload_response = measured_resource(
    "upload_1mib",
    lambda: request(
        resource_port, "POST", "/upload", upload_body,
        {"Content-Type": "multipart/form-data; boundary=gate3-resource-boundary"},
    ),
)
if upload_response[0] != 200 or json.loads(upload_response[1]) != {"ok": True, "size": len(upload_bytes)}:
    raise RuntimeError(f"upload failed: {upload_response!r}")
download_response = measured_resource(
    "download_4mib", lambda: request(resource_port, path="/download", read_delay=0.001),
)
if download_response != (200, download_bytes):
    raise RuntimeError("download failed or changed bytes")

resource_stdout, resource_stderr = resource_server.communicate(timeout=30)
if resource_server.returncode != 0:
    raise RuntimeError(f"resource server failed: {resource_stdout!r} {resource_stderr!r}")
with open(os.path.join(WORK, "saved.bin"), "rb") as source:
    if source.read() != upload_bytes:
        raise RuntimeError("saved upload changed bytes")
cleanup_usage = temp_usage()
if cleanup_usage != (0, 0, 0):
    raise RuntimeError(f"temporary roots remain after shutdown: {cleanup_usage!r}")

report = {
    "platform": sys.platform,
    "architecture": "bounded helper, one fresh Nift worker per request",
    "max_concurrency": MAX_CONCURRENCY,
    "idle_rss_kib": concurrency_idle,
    "resource_idle_rss_kib": resource_idle,
    "concurrency": concurrency_results,
    "resources": resource_results,
    "resource_sizes_bytes": {
        "upload": len(upload_bytes),
        "multipart_wire_body": len(upload_body),
        "download": len(download_bytes),
    },
    "temporary_cleanup_after_shutdown": True,
}
print(json.dumps(report, indent=2, sort_keys=True))
