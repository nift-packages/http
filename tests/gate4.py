#!/usr/bin/env python3
"""Gate 4 three-topology process-backend benchmark matrix."""

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
    raise SystemExit("gate4.py currently measures Linux /proc only")

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


def port_listening(port):
    expected = f"{port:04X}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as source:
                for line in source:
                    fields = line.split()
                    if len(fields) > 3 and fields[1].split(":")[-1] == expected and fields[3] == "0A":
                        return True
        except OSError:
            pass
    return False


def wait_for(predicate, message, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    raise TimeoutError(message)


def helper_child(pid):
    for child in children(pid):
        try:
            with open(f"/proc/{child}/cmdline", "rb") as command:
                if b"http_helper.py" in command.read():
                    return child
        except OSError:
            pass
    return None


def request(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=40)
    connection.request("GET", "/work")
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))]


def temp_usage():
    total = 0
    files = 0
    roots = 0
    for name in os.listdir(os.path.join(WORK, "tmp")):
        root = os.path.join(WORK, "tmp", name)
        if not name.startswith("nift-http-") or not os.path.isdir(root):
            continue
        roots += 1
        for directory, _subdirectories, filenames in os.walk(root):
            for filename in filenames:
                try:
                    total += os.path.getsize(os.path.join(directory, filename))
                    files += 1
                except OSError:
                    pass
    return total, files, roots


def topology(parent, helper):
    processes = descendants(parent)
    temporary_bytes, temporary_files, temporary_roots = temp_usage()
    return {
        "parent_rss_kib": rss_kib(parent),
        "helper_rss_kib": rss_kib(helper),
        "worker_processes": len(children(helper)),
        "topology_processes": 1 + len(processes),
        "topology_rss_kib": rss_kib(parent) + sum(rss_kib(pid) for pid in processes),
        "temporary_bytes": temporary_bytes,
        "temporary_files": temporary_files,
        "temporary_roots": temporary_roots,
    }


def merge_peak(peak, sample):
    for key, value in sample.items():
        peak[key] = max(peak.get(key, 0), value)


modes = (
    ("sequential_oneshot", "oneshot", 1, 30000),
    ("concurrent_oneshot", "oneshot", 32, 20),
    ("persistent_pool", "persistent", 32, 20),
)
report = {
    "platform": sys.platform,
    "workload": "GET handler executes sleep 0.04; one warm first request plus one simultaneous batch",
    "cpu": "not captured: transient descendant CPU cannot be attributed reliably without cgroup/process accounting",
    "modes": {},
}

for mode_name, worker_mode, max_concurrency, admission_timeout in modes:
    mode_results = {}
    for client_count in (1, 8, 32):
        port = free_port()
        filename = f"gate4-{mode_name}-{client_count}.f"
        with open(os.path.join(WORK, filename), "w", encoding="utf-8") as output:
            output.write(f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":{client_count + 1},"max_concurrency":{max_concurrency},"admission_timeout_ms":{admission_timeout},"worker_mode":"{worker_mode}","worker_pool_size":{max_concurrency},"backlog":64}})
http.get(app, "/work", (request) => {{ run("sh", "-c", "sleep 0.04"); return http.text("ok") }})
http.listen(app)
''')
        started = time.perf_counter()
        server = subprocess.Popen(
            [NIFT, filename], cwd=WORK, env=ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        wait_for(lambda: port_listening(port), f"{mode_name}/{client_count} did not listen")
        startup_ms = (time.perf_counter() - started) * 1000
        helper = wait_for(lambda: helper_child(server.pid), "helper process not found")
        first_started = time.perf_counter()
        if request(port) != (200, b"ok"):
            raise RuntimeError("first request failed")
        first_request_ms = (time.perf_counter() - first_started) * 1000
        expected_idle_workers = max_concurrency if worker_mode == "persistent" else 0
        wait_for(
            lambda: len(children(helper)) == expected_idle_workers,
            f"{mode_name}/{client_count} did not reach idle topology",
        )
        idle = topology(server.pid, helper)

        barrier = threading.Barrier(client_count + 1)
        latencies = []
        errors = []

        def client():
            try:
                barrier.wait()
                request_started = time.perf_counter()
                result = request(port)
                latencies.append((time.perf_counter() - request_started) * 1000)
                if result != (200, b"ok"):
                    errors.append(repr(result))
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=client) for _ in range(client_count)]
        for thread in threads:
            thread.start()
        peak = {}
        batch_started = time.perf_counter()
        barrier.wait()
        while any(thread.is_alive() for thread in threads):
            merge_peak(peak, topology(server.pid, helper))
            time.sleep(0.002)
        for thread in threads:
            thread.join()
        batch_seconds = time.perf_counter() - batch_started
        stdout, stderr = server.communicate(timeout=45)
        if server.returncode != 0 or errors or len(latencies) != client_count:
            raise RuntimeError(
                f"{mode_name}/{client_count} failed: {errors!r} {stdout!r} {stderr!r}",
            )
        if temp_usage() != (0, 0, 0):
            raise RuntimeError(f"{mode_name}/{client_count} left temporary files")
        mode_results[str(client_count)] = {
            "startup_ms": round(startup_ms, 3),
            "first_request_ms": round(first_request_ms, 3),
            "batch_ms": round(batch_seconds * 1000, 3),
            "throughput_requests_per_second": round(client_count / batch_seconds, 3),
            "latency_ms": {
                "median": round(statistics.median(latencies), 3),
                "p95": round(percentile(latencies, 0.95), 3),
                "max": round(max(latencies), 3),
            },
            "errors": len(errors),
            "idle": idle,
            "peak": peak,
        }
    report["modes"][mode_name] = mode_results

print(json.dumps(report, indent=2, sort_keys=True))
