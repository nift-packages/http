#!/usr/bin/env python3
"""Gate 5 Linux streaming measurements for one-shot and persistent workers."""

import glob
import fcntl
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time


if not sys.platform.startswith("linux"):
    raise SystemExit("gate5.py measures Linux /proc topology")

NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, ".gate5-work")
TMP = os.path.join(WORK, "tmp")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, ".nift"), exist_ok=True)
os.makedirs(TMP, exist_ok=True)
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)
ENV = {**os.environ, "NIFT_HTTP_NIFT": NIFT, "TMPDIR": TMP, "TEMP": TMP, "TMP": TMP}

BULK_CHUNK = b"0123456789abcdef" * 2048
SLOW_CHUNK = b"z" * 65536
with open(os.path.join(WORK, "bulk.chunk"), "wb") as output:
    output.write(BULK_CHUNK)
with open(os.path.join(WORK, "slow.chunk"), "wb") as output:
    output.write(SLOW_CHUNK)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, message, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
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


def descendants(pid):
    result = []
    pending = children(pid)
    while pending:
        child = pending.pop()
        result.append(child)
        pending.extend(children(child))
    return result


def helper_child(pid):
    for child in children(pid):
        try:
            with open(f"/proc/{child}/cmdline", "rb") as source:
                if b"http_helper.py" in source.read():
                    return child
        except OSError:
            pass
    return None


def rss_kib(pid):
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as source:
            for line in source:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def temp_usage():
    total = files = roots = 0
    for root in glob.glob(os.path.join(TMP, "nift-http-*")):
        if not os.path.isdir(root):
            continue
        roots += 1
        for directory, _subdirectories, filenames in os.walk(root):
            for filename in filenames:
                try:
                    total += os.path.getsize(os.path.join(directory, filename))
                    files += 1
                except OSError:
                    pass
    return {"bytes": total, "files": files, "roots": roots}


def fifo_capacity():
    paths = glob.glob(os.path.join(TMP, "nift-http-*", "*", "response.pipe"))
    if not paths:
        return 0
    descriptor = os.open(paths[0], os.O_RDONLY | os.O_NONBLOCK)
    try:
        return fcntl.fcntl(descriptor, fcntl.F_GETPIPE_SZ)
    finally:
        os.close(descriptor)


def topology(parent, helper):
    helper_descendants = descendants(helper)
    parent_descendants = descendants(parent)
    worker_pids = children(helper)
    return {
        "parent_rss_kib": rss_kib(parent),
        "helper_rss_kib": rss_kib(helper),
        "worker_rss_kib": sum(rss_kib(pid) for pid in worker_pids),
        "worker_rss_kib_each": [rss_kib(pid) for pid in worker_pids],
        "worker_processes": len(worker_pids),
        "helper_descendant_processes": len(helper_descendants),
        "topology_processes": 1 + len(parent_descendants),
        "topology_rss_kib": rss_kib(parent) + sum(rss_kib(pid) for pid in parent_descendants),
        "temporary_storage": temp_usage(),
    }


def connect_request(port, path, receive_buffer=None):
    client = socket.create_connection(("127.0.0.1", port), timeout=20)
    if receive_buffer is not None:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
    client.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    return client


def read_until(sock, marker, buffer=b""):
    while marker not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError(f"connection closed before {marker!r}")
        buffer += chunk
    end = buffer.index(marker) + len(marker)
    return buffer[:end], buffer[end:]


def read_chunk(sock, buffer=b""):
    line, buffer = read_until(sock, b"\r\n", buffer)
    length = int(line[:-2], 16)
    while len(buffer) < length + 2:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("connection closed inside chunk")
        buffer += chunk
    if buffer[length:length + 2] != b"\r\n":
        raise RuntimeError("invalid chunk terminator")
    return buffer[:length], buffer[length + 2:]


def start_server(mode):
    port = free_port()
    status_path = os.path.join(WORK, f"gate5-{mode}.status.json")
    marker = os.path.join(WORK, f"gate5-{mode}.slow.done")
    for path in (status_path, marker):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    pool = ',"worker_pool_size":1' if mode == "persistent" else ""
    routes = '''
http.get(app, "/timed", (request) => http.stream((write) => {
    write("first")
    run("sh", "-c", "sleep 0.20")
    write("second")
}))
http.get(app, "/bulk", (request) => http.stream((write) => {
''' + "\n".join('    write(open("bulk.chunk"))' for _ in range(256)) + '''
}))
http.get(app, "/slow", (request) => http.stream((write) => {
''' + "\n".join('    write(open("slow.chunk"))' for _ in range(1024)) + f'''
    touch("{marker}")
}}))
http.get(app, "/recovery", (request) => http.stream((write) => write("ok")))
'''
    source = f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":4,"max_concurrency":1,"worker_mode":"{mode}"{pool},"worker_timeout_ms":20000,"response_timeout_ms":15000,"status_path":"{status_path}"}})
{routes}
http.listen(app)
'''
    filename = f"gate5-{mode}.f"
    with open(os.path.join(WORK, filename), "w", encoding="utf-8") as output:
        output.write(source)
    started = time.perf_counter()
    server = subprocess.Popen(
        [NIFT, filename], cwd=WORK, env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    wait_for(lambda: (load_json(status_path) or {}).get("ready"), f"{mode} server not ready")
    startup_ms = (time.perf_counter() - started) * 1000
    helper = wait_for(lambda: helper_child(server.pid), f"{mode} helper not found")
    return server, port, status_path, marker, helper, startup_ms


def measure_mode(mode):
    server, port, status_path, marker, helper, startup_ms = start_server(mode)
    idle = topology(server.pid, helper)

    client = connect_request(port, "/timed")
    started = time.perf_counter()
    first_byte = client.recv(1)
    first_byte_ms = (time.perf_counter() - started) * 1000
    headers, buffered = read_until(client, b"\r\n\r\n", first_byte)
    first, buffered = read_chunk(client, buffered)
    first_chunk_ms = (time.perf_counter() - started) * 1000
    second, buffered = read_chunk(client, buffered)
    terminal, buffered = read_chunk(client, buffered)
    completion_ms = (time.perf_counter() - started) * 1000
    client.close()
    if not headers.startswith(b"HTTP/1.1 200") or (first, second, terminal) != (b"first", b"second", b""):
        raise RuntimeError(f"{mode} timing stream failed")

    client = connect_request(port, "/bulk")
    bulk_started = time.perf_counter()
    _headers, buffered = read_until(client, b"\r\n\r\n")
    received = chunks = 0
    while True:
        chunk, buffered = read_chunk(client, buffered)
        if not chunk:
            break
        received += len(chunk)
        chunks += 1
    bulk_seconds = time.perf_counter() - bulk_started
    client.close()
    if received != len(BULK_CHUNK) * 256:
        raise RuntimeError(f"{mode} bulk stream mismatch")

    slow = connect_request(port, "/slow", receive_buffer=4096)
    slow_receive_buffer = slow.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    _headers, buffered = read_until(slow, b"\r\n\r\n")
    blocked_started = time.monotonic()
    time.sleep(1.0)
    blocked = topology(server.pid, helper)
    blocked["fifo_capacity_bytes"] = fifo_capacity()
    marker_absent = not os.path.exists(marker)
    bounded = (
        marker_absent
        and blocked["worker_processes"] <= 1
        and (load_json(status_path) or {}).get("active_requests") == 1
        and blocked["helper_rss_kib"] <= idle["helper_rss_kib"] + 8192
        and blocked["topology_rss_kib"] <= idle["topology_rss_kib"] + 32768
        and blocked["topology_processes"] <= idle["topology_processes"] + 1
        and blocked["temporary_storage"]["bytes"] < 1048576
        and 0 < blocked["fifo_capacity_bytes"] < 1048576
    )
    if not bounded:
        raise RuntimeError(f"{mode} slow-reader resources were not bounded: {blocked!r}")
    observed_descendants = set(descendants(server.pid))
    slow.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    slow.close()
    disconnected = time.monotonic()
    wait_for(lambda: (load_json(status_path) or {}).get("active_requests") == 0,
             f"{mode} disconnect cleanup timed out", timeout=8)
    disconnect_cleanup_ms = (time.monotonic() - disconnected) * 1000

    recovery = connect_request(port, "/recovery")
    headers, buffered = read_until(recovery, b"\r\n\r\n")
    value, buffered = read_chunk(recovery, buffered)
    terminal, buffered = read_chunk(recovery, buffered)
    recovery.close()
    if not headers.startswith(b"HTTP/1.1 200") or value != b"ok" or terminal:
        raise RuntimeError(f"{mode} did not recover after disconnect")
    observed_descendants.update(descendants(server.pid))

    stdout, stderr = server.communicate(timeout=25)
    if server.returncode != 0:
        raise RuntimeError(f"{mode} server failed: {stdout!r} {stderr!r}")
    final_temp = temp_usage()
    if final_temp != {"bytes": 0, "files": 0, "roots": 0}:
        raise RuntimeError(f"{mode} leaked temporary storage: {final_temp!r}")
    surviving_descendants = [pid for pid in observed_descendants if os.path.exists(f"/proc/{pid}")]
    if surviving_descendants:
        raise RuntimeError(f"{mode} leaked descendants: {surviving_descendants!r}")
    return {
        "startup_ms": round(startup_ms, 3),
        "timing_ms": {
            "first_byte": round(first_byte_ms, 3),
            "first_chunk": round(first_chunk_ms, 3),
            "completion": round(completion_ms, 3),
        },
        "bulk": {
            "bytes": received,
            "chunks": chunks,
            "completion_ms": round(bulk_seconds * 1000, 3),
            "throughput_bytes_per_second": round(received / bulk_seconds, 3),
            "throughput_mib_per_second": round(received / bulk_seconds / 1048576, 3),
        },
        "topology_idle": idle,
        "topology_slow_reader": blocked,
        "slow_reader": {
            "observation_ms": round((disconnected - blocked_started) * 1000, 3),
            "bounded": bounded,
            "producer_completion_marker_absent": marker_absent,
            "client_receive_buffer_bytes": slow_receive_buffer,
            "disconnect_cleanup_ms": round(disconnect_cleanup_ms, 3),
            "recovery_succeeded": True,
        },
        "final_temporary_storage": final_temp,
        "final_descendant_processes": len(surviving_descendants),
    }


report = {
    "platform": sys.platform,
    "workload": {
        "timed_stream_delay_ms": 200,
        "bulk_bytes": len(BULK_CHUNK) * 256,
        "bulk_producer_writes": 256,
        "slow_reader_producer_bytes": len(SLOW_CHUNK) * 1024,
    },
    "cpu_attribution": {
        "available": False,
        "value": None,
        "reason": "transient worker CPU cannot be attributed reliably without cgroup or process accounting",
    },
    "modes": {},
}
for worker_mode in ("oneshot", "persistent"):
    report["modes"][worker_mode] = measure_mode(worker_mode)

print(json.dumps(report, indent=2, sort_keys=True))
