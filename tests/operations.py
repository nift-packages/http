#!/usr/bin/env python3
"""CP12 graceful drain and bounded observability tests."""

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
    raise SystemExit("operations.py currently verifies Linux process lifecycle")

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


def wait_for(predicate, message, timeout=10):
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


def helper_child(pid):
    for child in children(pid):
        try:
            with open(f"/proc/{child}/cmdline", "rb") as command:
                if b"http_helper.py" in command.read():
                    return child
        except OSError:
            pass
    return None


def start_server(name, port, config, routes):
    filename = name + ".f"
    with open(os.path.join(WORK, filename), "w", encoding="utf-8") as output:
        output.write(f'''@import("http")
app := http.server({{{config}}})
{routes}
http.listen(app)
''')
    process = subprocess.Popen(
        [NIFT, filename], cwd=WORK, env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    helper = wait_for(lambda: helper_child(process.pid), f"{name} helper did not start")
    return process, helper


def request(port, path="/", timeout=10):
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            connection.request("GET", path)
            response = connection.getresponse()
            result = response.status, response.read()
            connection.close()
            return result
        except ConnectionRefusedError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


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


def finish(server, label, timeout=20):
    stdout, stderr = server.communicate(timeout=timeout)
    if server.returncode != 0:
        raise AssertionError(f"{label} failed: {stdout!r} {stderr!r}")


base = lambda port, extra: (
    f'"host":"127.0.0.1","port":{port},"max_concurrency":2,'
    f'"worker_mode":"persistent","worker_pool_size":2,{extra}'
)


# Status snapshots remain atomic and event logging remains valid/bounded while
# exposing admission, queue, worker, recycle, error, and overload counters.
port = free_port()
status_path = os.path.join(WORK, "status.json")
events_path = os.path.join(WORK, "events.ndjson")
config = base(port, '"max_requests":43,"worker_max_requests":10,"shutdown_grace_ms":1000,'
                    '"status_path":"status.json","event_log_path":"events.ndjson",'
                    '"max_event_log_bytes":4096')
server, helper = start_server(
    "observability", port, config,
    'http.get(app, "/fast", (request) => http.text("fast"))\n'
    'http.get(app, "/slow", (request) => { run("sh", "-c", "sleep 0.3"); return http.text("slow") })',
)
ready = wait_for(
    lambda: (value := load_json(status_path)) and value["phase"] == "ready" and value,
    "ready status was not published",
)
if ready["worker_processes"] != 2 or ready["active_requests"] != 0:
    raise AssertionError(ready)
for _index in range(40):
    if request(port, "/fast") != (200, b"fast"):
        raise AssertionError("fast observability request failed")
slow_one, slow_one_result, slow_one_error = threaded(lambda: request(port, "/slow"))
slow_two, slow_two_result, slow_two_error = threaded(lambda: request(port, "/slow"))
wait_for(
    lambda: (value := load_json(status_path)) and value["active_requests"] == 2,
    "active request count did not reach two",
)
overload, overload_result, overload_error = threaded(lambda: request(port, "/fast"))
queue_peak = 0
while overload.is_alive():
    value = load_json(status_path)
    if value:
        queue_peak = max(queue_peak, value["queue_depth"])
    time.sleep(0.001)
overload.join()
if overload_error or overload_result[0] != (503, b"") or queue_peak != 1:
    raise AssertionError((overload_error, overload_result, queue_peak))
slow_one.join()
slow_two.join()
if slow_one_error or slow_two_error or slow_one_result[0][0] != 200 or slow_two_result[0][0] != 200:
    raise AssertionError((slow_one_error, slow_two_error, slow_one_result, slow_two_result))
finish(server, "observability")
stopped = load_json(status_path)
expected = {"accepted": 43, "admitted": 42, "rejected": 1, "completed": 42}
if stopped["phase"] != "stopped" or stopped["ready"] or stopped["active_requests"] != 0 or stopped["worker_processes"] != 0:
    raise AssertionError(stopped)
if any(stopped["counters"][key] != value for key, value in expected.items()):
    raise AssertionError(stopped["counters"])
if stopped["counters"]["worker_recycles"] < 4 or stopped["counters"]["worker_starts"] < 6:
    raise AssertionError(stopped["counters"])
if os.path.getsize(events_path) > 4096:
    raise AssertionError("event log exceeded configured bound")
with open(events_path, encoding="utf-8") as source:
    events = [json.loads(line) for line in source if line.strip()]
if not any(event["event"] == "overload" and event["status"] == 503 for event in events):
    raise AssertionError(events)
if not any(event["event"] == "request" and event["worker_id"] is not None for event in events):
    raise AssertionError(events)


# One signal drains an active request within the deadline and stops admission.
port = free_port()
status_path = os.path.join(WORK, "drain-status.json")
config = base(port, '"shutdown_grace_ms":1000,"status_path":"drain-status.json"')
server, helper = start_server(
    "drain", port, config,
    'http.get(app, "/slow", (request) => { marker := ofstream("drain.started"); marker.write("yes"); close(marker); run("sh", "-c", "sleep 0.3"); return http.text("slow") })',
)
slow, slow_result, slow_error = threaded(lambda: request(port, "/slow"))
wait_for(lambda: os.path.exists(os.path.join(WORK, "drain.started")), "drain handler did not start")
started = time.monotonic()
os.kill(helper, signal.SIGTERM)
slow.join(3)
finish(server, "drain")
if slow_error or slow_result[0] != (200, b"slow") or time.monotonic() - started >= 1:
    raise AssertionError((slow_error, slow_result))
if load_json(status_path)["phase"] != "stopped":
    raise AssertionError("graceful drain did not publish stopped")


# An over-deadline request is forcefully cancelled and reaped.
port = free_port()
status_path = os.path.join(WORK, "forced-status.json")
config = base(port, '"shutdown_grace_ms":100,"status_path":"forced-status.json","worker_timeout_ms":30000')
server, helper = start_server(
    "forced", port, config,
    'http.get(app, "/hang", (request) => { marker := ofstream("forced.started"); marker.write("yes"); close(marker); while(true) {} return http.text("never") })',
)
hang, _hang_result, _hang_error = threaded(lambda: request(port, "/hang", timeout=35))
wait_for(lambda: os.path.exists(os.path.join(WORK, "forced.started")), "forced handler did not start")
worker_pids = children(helper)
started = time.monotonic()
os.kill(helper, signal.SIGTERM)
hang.join(3)
finish(server, "forced")
if hang.is_alive() or time.monotonic() - started >= 2:
    raise AssertionError("forced shutdown exceeded its bound")
if any(os.path.exists(f"/proc/{pid}") for pid in worker_pids):
    raise AssertionError("worker survived forced shutdown")
forced = load_json(status_path)
if forced["phase"] != "stopped" or forced["active_requests"] != 0 or forced["worker_processes"] != 0:
    raise AssertionError(forced)
if any(name.startswith("nift-http-") for name in os.listdir(os.path.join(WORK, "tmp"))):
    raise AssertionError("temporary root survived CP12 lifecycle tests")


# A second signal bypasses a long drain deadline and terminates a one-shot
# worker immediately rather than waiting for worker_timeout_ms.
port = free_port()
status_path = os.path.join(WORK, "second-status.json")
config = (
    f'"host":"127.0.0.1","port":{port},"max_concurrency":1,'
    '"worker_mode":"oneshot","shutdown_grace_ms":10000,"worker_timeout_ms":30000,'
    '"status_path":"second-status.json"'
)
server, helper = start_server(
    "second-signal", port, config,
    'http.get(app, "/hang", (request) => { marker := ofstream("second.started"); marker.write("yes"); close(marker); while(true) {} return http.text("never") })',
)
hang, _hang_result, _hang_error = threaded(lambda: request(port, "/hang", timeout=35))
wait_for(lambda: os.path.exists(os.path.join(WORK, "second.started")), "one-shot handler did not start")
worker_pids = children(helper)
started = time.monotonic()
os.kill(helper, signal.SIGTERM)
wait_for(
    lambda: (value := load_json(status_path)) and value["phase"] == "draining",
    "first signal did not begin drain",
)
os.kill(helper, signal.SIGTERM)
hang.join(3)
finish(server, "second-signal")
if hang.is_alive() or time.monotonic() - started >= 2:
    raise AssertionError("second signal did not force prompt shutdown")
if any(os.path.exists(f"/proc/{pid}") for pid in worker_pids):
    raise AssertionError("one-shot worker survived second signal")


# Parent death during persistent startup cancels readiness work and owns the
# partially started worker instead of publishing a listener.
port = free_port()
status_path = os.path.join(WORK, "startup-parent-status.json")
startup_source = f'''@import("http")
if(getenv("NIFT_HTTP_WORKER") == "persistent") {{ run("sh", "-c", "sleep 5") }}
app := http.server({{"host":"127.0.0.1","port":{port},"max_concurrency":1,"worker_mode":"persistent","worker_pool_size":1,"status_path":"startup-parent-status.json"}})
http.get(app, "/", (request) => http.text("never"))
http.listen(app)
'''
with open(os.path.join(WORK, "startup-parent.f"), "w", encoding="utf-8") as output:
    output.write(startup_source)
server = subprocess.Popen(
    [NIFT, "startup-parent.f"], cwd=WORK, env=ENV,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
helper = wait_for(lambda: helper_child(server.pid), "startup helper did not appear")
startup_workers = wait_for(lambda: children(helper), "startup worker did not appear")
server.kill()
server.communicate(timeout=5)
wait_for(lambda: not os.path.exists(f"/proc/{helper}"), "helper survived startup parent death")
for pid in startup_workers:
    if os.path.exists(f"/proc/{pid}"):
        raise AssertionError("worker survived startup parent death")
startup_status = wait_for(lambda: load_json(status_path), "startup status was not retained")
if startup_status["phase"] != "stopped" or startup_status["worker_processes"] != 0:
    raise AssertionError(startup_status)


# A worker that fails before readiness was never counted as a live pool member.
port = free_port()
status_path = os.path.join(WORK, "startup-failure-status.json")
failure_source = f'''@import("http")
if(getenv("NIFT_HTTP_WORKER") == "persistent") {{ broken := missing.value }}
app := http.server({{"host":"127.0.0.1","port":{port},"max_concurrency":1,"worker_mode":"persistent","worker_pool_size":1,"status_path":"startup-failure-status.json"}})
http.get(app, "/", (request) => http.text("never"))
http.listen(app)
'''
with open(os.path.join(WORK, "startup-failure.f"), "w", encoding="utf-8") as output:
    output.write(failure_source)
failure = subprocess.run(
    [NIFT, "startup-failure.f"], cwd=WORK, env=ENV,
    capture_output=True, text=True, timeout=10,
)
failure_status = load_json(status_path)
if failure.returncode != 0 or failure_status["phase"] != "stopped" or failure_status["worker_processes"] != 0:
    raise AssertionError((failure.returncode, failure_status, failure.stderr))
if failure_status["counters"]["worker_starts"] != 0:
    raise AssertionError(failure_status)


# Oversized request metadata is omitted rather than violating the event-file
# byte limit; every retained line remains independently valid NDJSON.
port = free_port()
events_path = os.path.join(WORK, "oversized-events.ndjson")
config = (
    f'"host":"127.0.0.1","port":{port},"max_requests":1,"max_concurrency":1,'
    '"event_log_path":"oversized-events.ndjson","max_event_log_bytes":4096'
)
server, helper = start_server(
    "oversized-event", port, config,
    'http.get(app, "/ok", (request) => http.text("ok"))',
)
oversized = request(port, "/" + "x" * 5000)
if oversized[0] != 404:
    raise AssertionError(oversized)
finish(server, "oversized-event")
if os.path.getsize(events_path) > 4096:
    raise AssertionError("one event exceeded its configured log bound")
with open(events_path, encoding="utf-8") as source:
    oversized_events = [json.loads(line) for line in source if line.strip()]
if len(oversized_events) != 1 or not oversized_events[0].get("truncated"):
    raise AssertionError(oversized_events)

print("PASS http CP12 operations and observability")
