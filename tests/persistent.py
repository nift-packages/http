#!/usr/bin/env python3
"""CP11 persistent concurrent worker-pool tests."""

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time


if not sys.platform.startswith("linux"):
    raise SystemExit("persistent.py currently verifies Linux process groups")

NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, ".dogfood-work")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, ".nift"), exist_ok=True)
os.makedirs(os.path.join(WORK, "tmp"), exist_ok=True)
os.makedirs(os.path.join(WORK, "saved"), exist_ok=True)
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


def wait_for(predicate, message, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise TimeoutError(message)


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


def start_server(name, source, expected_workers, port):
    filename = name + ".f"
    with open(os.path.join(WORK, filename), "w", encoding="utf-8") as output:
        output.write(source)
    process = subprocess.Popen(
        [NIFT, filename], cwd=WORK, env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    def helper_child():
        for child in children(process.pid):
            try:
                with open(f"/proc/{child}/cmdline", "rb") as command:
                    if b"http_helper.py" in command.read():
                        return child
            except OSError:
                pass
        return None

    helper = wait_for(helper_child, f"{name} helper did not start")
    wait_for(
        lambda: len(children(helper)) == expected_workers,
        f"{name} persistent pool did not start",
    )
    wait_for(lambda: port_listening(port), f"{name} listener did not become ready")
    return process, helper


def request(port, method="GET", path="/", body=None, headers=None, timeout=10):
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = response.status, response.getheaders(), response.read()
            connection.close()
            return result
        except ConnectionRefusedError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def finish_server(server, label, timeout=20):
    stdout, stderr = server.communicate(timeout=timeout)
    if server.returncode != 0:
        raise AssertionError(f"{label} failed: {stdout!r} {stderr!r}")


def wave(count, target):
    barrier = threading.Barrier(count + 1)
    results = []
    errors = []

    def invoke():
        try:
            barrier.wait()
            results.append(target())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=invoke) for _ in range(count)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    if errors:
        raise AssertionError(errors)
    return results


def threaded(target):
    results = []
    errors = []

    def invoke():
        try:
            results.append(target())
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    return thread, results, errors


def multipart(content):
    return (
        b"--cp11-boundary\r\n"
        b"Content-Disposition: form-data; name=\"attachment\"; filename=\"client.bin\"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n" + content + b"\r\n"
        b"--cp11-boundary--\r\n"
    )


# Four workers retain four independent counters. Concurrent waves produce four
# ones and then four twos, never one coherent global sequence.
port = free_port()
server, helper = start_server("state", f'''@import("http")
counter := 0
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":11,"max_concurrency":4,"worker_mode":"persistent","worker_pool_size":4}})
http.get(app, "/state", (request) => {{
    counter += 1
    run("sh", "-c", "sleep 0.1")
    return http.json({{"counter":counter}})
}})
http.get(app, "/slow", (request) => {{ run("sh", "-c", "sleep 0.5"); return http.text("slow") }})
http.get(app, "/fast", (request) => http.text("fast"))
http.get(app, "/print", (request) => {{
    i := 0
    while(i < 2000) {{ print("application output that exceeds retained diagnostics"); i += 1 }}
    return http.text("printed")
}})
http.listen(app)
''', 4, port)
first = wave(4, lambda: request(port, path="/state"))
second = wave(4, lambda: request(port, path="/state"))
if sorted(json.loads(result[2])["counter"] for result in first) != [1, 1, 1, 1]:
    raise AssertionError(first)
if sorted(json.loads(result[2])["counter"] for result in second) != [2, 2, 2, 2]:
    raise AssertionError(second)
slow, slow_result, slow_error = threaded(lambda: request(port, path="/slow"))
time.sleep(0.05)
fast_started = time.perf_counter()
fast = request(port, path="/fast")
if fast[0] != 200 or time.perf_counter() - fast_started >= 0.3 or not slow.is_alive():
    raise AssertionError((fast, slow.is_alive()))
slow.join()
if slow_error or slow_result[0][0] != 200:
    raise AssertionError((slow_error, slow_result))
if request(port, path="/print") != (200, [("content-type", "text/plain; charset=utf-8"), ("Content-Length", "7"), ("Connection", "close")], b"printed"):
    raise AssertionError("application output affected framing")
worker_pids = children(helper)
finish_server(server, "state")
if any(os.path.exists(f"/proc/{pid}") for pid in worker_pids):
    raise AssertionError("persistent state worker survived finite shutdown")


# A finite request count recycles a worker without changing the public request
# protocol. The new process starts with fresh worker-local state.
port = free_port()
server, helper = start_server("recycle", f'''@import("http")
counter := 0
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":4,"max_concurrency":1,"worker_mode":"persistent","worker_pool_size":1,"worker_max_requests":2}})
http.get(app, "/state", (request) => {{ counter += 1; return http.json({{"counter":counter}}) }})
http.listen(app)
''', 1, port)
values = [json.loads(request(port, path="/state")[2])["counter"] for _ in range(4)]
if values != [1, 2, 1, 2]:
    raise AssertionError(values)
finish_server(server, "recycle")


# An idle worker that dies between requests is detected before checkout and the
# fixed-size pool is restored before dispatch.
port = free_port()
server, helper = start_server("idle-health", f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":2,"max_concurrency":2,"worker_mode":"persistent","worker_pool_size":2}})
http.get(app, "/fast", (request) => {{ run("sh", "-c", "sleep 0.2"); return http.text("fast") }})
http.listen(app)
''', 2, port)
idle_workers = set(children(helper))
for worker_pid in idle_workers:
    os.kill(worker_pid, signal.SIGKILL)
health_one, health_one_result, health_one_error = threaded(lambda: request(port, path="/fast"))
health_two, health_two_result, health_two_error = threaded(lambda: request(port, path="/fast"))


def repaired_idle_pool():
    current = set(children(helper))
    return current if len(current) == 2 and len(current - idle_workers) == 2 else None


wait_for(repaired_idle_pool, "idle worker was not replaced before dispatch")
health_one.join()
health_two.join()
if health_one_error or health_two_error or health_one_result[0][0] != 200 or health_two_result[0][0] != 200:
    raise AssertionError((health_one_error, health_two_error, health_one_result, health_two_result))
finish_server(server, "idle-health")


# A crashing or timed-out worker is replaced without harming the other worker
# or replaying the uncertain request.
port = free_port()
server, helper = start_server("replacement", f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":6,"max_concurrency":2,"worker_mode":"persistent","worker_pool_size":2,"worker_timeout_ms":250}})
http.get(app, "/fast", (request) => http.text("fast"))
http.get(app, "/crash", (request) => {{ run("sh", "-c", "echo crash >> replay.log; touch crash.started; sleep 0.1"); return request.missing.value }})
http.get(app, "/timeout", (request) => {{ run("sh", "-c", "echo timeout >> replay.log; touch timeout.started"); while(true) {{}} return http.text("never") }})
http.listen(app)
''', 2, port)
initial_workers = set(children(helper))
crash, crash_result, crash_error = threaded(lambda: request(port, path="/crash"))
wait_for(lambda: os.path.exists(os.path.join(WORK, "crash.started")), "crash handler did not start")
if request(port, path="/fast")[0] != 200:
    raise AssertionError("healthy worker failed beside crash")
crash.join()
if crash_error or crash_result[0][0] != 500:
    raise AssertionError((crash_error, crash_result))


def crash_replacement():
    current = set(children(helper))
    return current if len(current) == 2 and len(current - initial_workers) == 1 else None


after_crash = set(wait_for(crash_replacement, "crashed worker was not replaced"))
timeout, timeout_result, timeout_error = threaded(lambda: request(port, path="/timeout"))
wait_for(lambda: os.path.exists(os.path.join(WORK, "timeout.started")), "timeout handler did not start")
if request(port, path="/fast")[0] != 200:
    raise AssertionError("healthy worker failed beside timeout")
timeout.join()
if timeout_error or timeout_result[0][0] != 504:
    raise AssertionError((timeout_error, timeout_result))


def timeout_replacement():
    current = set(children(helper))
    return current if len(current) == 2 and len(current - after_crash) == 1 else None


wait_for(timeout_replacement, "timed-out worker was not replaced")
if request(port, path="/fast")[0] != 200 or request(port, path="/fast")[0] != 200:
    raise AssertionError("replacement workers are not healthy")
finish_server(server, "replacement")
with open(os.path.join(WORK, "replay.log"), encoding="ascii") as source:
    if source.read().split() != ["crash", "timeout"]:
        raise AssertionError("failed requests were replayed")


# Request-scoped upload/body tokens expire explicitly in a persistent process;
# retained descriptors cannot access a deleted helper spool on later requests.
port = free_port()
server, helper = start_server("spools", f'''@import("http")
retained_upload := {{}}
retained_body := {{}}
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":6,"max_concurrency":1,"worker_mode":"persistent","worker_pool_size":1}})
http.post(app, "/upload", (request) => {{ retained_upload = request.files.attachment; return http.json(http.save_upload(retained_upload, "saved/upload.bin")) }})
http.get(app, "/reuse-upload", (request) => http.json(http.save_upload(retained_upload, "saved/reused.bin")))
http.post(app, "/body", (request) => {{ retained_body = request.body; return http.json(http.save_body(retained_body, "saved/body.bin")) }})
http.get(app, "/reuse-body", (request) => http.json(http.save_body(retained_body, "saved/reused-body.bin")))
http.get(app, "/file", (request) => http.file("saved/upload.bin"))
http.get(app, "/range", (request) => http.file("saved/body.bin"))
http.listen(app)
''', 1, port)
upload_bytes = b"\x00persistent-upload\xff"
upload = request(
    port, "POST", "/upload", multipart(upload_bytes),
    {"Content-Type": "multipart/form-data; boundary=cp11-boundary"},
)
if upload[0] != 200 or not json.loads(upload[2])["ok"]:
    raise AssertionError(upload)
if json.loads(request(port, path="/reuse-upload")[2])["error_code"] != "invalid_upload":
    raise AssertionError("retained upload token did not expire")
body_bytes = b"\x00persistent-body\xfe"
body = request(port, "POST", "/body", body_bytes, {"Content-Type": "application/octet-stream"})
if body[0] != 200 or not json.loads(body[2])["ok"]:
    raise AssertionError(body)
if json.loads(request(port, path="/reuse-body")[2])["error_code"] != "invalid_body":
    raise AssertionError("retained body token did not expire")
if request(port, path="/file")[2] != upload_bytes:
    raise AssertionError("persistent file response changed upload bytes")
ranged = request(port, path="/range", headers={"Range": "bytes=1-4"})
if ranged[0] != 206 or ranged[2] != body_bytes[1:5]:
    raise AssertionError(ranged)
finish_server(server, "spools")


# Abortive shutdown owns idle/active persistent workers and descendants.
descendant_file = os.path.join(WORK, "persistent-descendants.txt")
port = free_port()
server, helper = start_server("shutdown-persistent", f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_concurrency":4,"worker_mode":"persistent","worker_pool_size":4,"worker_timeout_ms":30000}})
http.get(app, "/hang", (request) => {{
    run("sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $! >> persistent-descendants.txt")
    while(true) {{}}
    return http.text("never")
}})
http.listen(app)
''', 4, port)
barrier = threading.Barrier(5)
clients = []


def hang():
    barrier.wait()
    try:
        request(port, path="/hang", timeout=35)
    except Exception:
        pass


for _index in range(4):
    client = threading.Thread(target=hang)
    clients.append(client)
    client.start()
barrier.wait()


def descendant_pids():
    try:
        with open(descendant_file, encoding="ascii") as source:
            values = [int(value) for value in source.read().split()]
        return values if len(values) == 4 else None
    except OSError:
        return None


worker_pids = children(helper)
spawned_descendants = wait_for(descendant_pids, "persistent descendants did not start")
os.kill(helper, signal.SIGTERM)
for client in clients:
    client.join(5)
if any(client.is_alive() for client in clients):
    raise AssertionError("persistent clients survived helper shutdown")
finish_server(server, "shutdown-persistent")
for pid in worker_pids + spawned_descendants:
    if os.path.exists(f"/proc/{pid}"):
        raise AssertionError(f"persistent process survived shutdown: {pid}")
if any(name.startswith("nift-http-") for name in os.listdir(os.path.join(WORK, "tmp"))):
    raise AssertionError("persistent temporary root survived shutdown")

print("PASS http CP11 persistent concurrent worker pool")
