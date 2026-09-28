#!/usr/bin/env python3
"""CP10 bounded concurrent one-shot worker tests."""

import http.client
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time


if not sys.platform.startswith("linux"):
    raise SystemExit("concurrency.py currently verifies Linux process groups")

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


def start_server(name, source):
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


def run_thread(target):
    result = []
    error = []

    def invoke():
        try:
            result.append(target())
        except Exception as exc:
            error.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    return thread, result, error


def finish_server(server, label, timeout=20):
    stdout, stderr = server.communicate(timeout=timeout)
    if server.returncode != 0:
        raise AssertionError(f"{label} failed: {stdout!r} {stderr!r}")


def multipart(boundary, content):
    return (
        b"--" + boundary + b"\r\n"
        b"Content-Disposition: form-data; name=\"attachment\"; filename=\"client.bin\"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n" + content + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )


# A fast request must overtake a slow request, while a full server rejects
# overload without starting an unbounded third worker.
port = free_port()
server, helper = start_server("overlap", f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":4,"max_concurrency":2}})
http.get(app, "/slow", (request) => {{ run("sh", "-c", "sleep 1"); return http.text("slow") }})
http.get(app, "/fast", (request) => http.text("fast"))
http.listen(app)
''')
slow_one, slow_one_result, slow_one_error = run_thread(lambda: request(port, path="/slow"))


def first_slow_started():
    if slow_one_error or slow_one_result:
        raise AssertionError((slow_one_error, slow_one_result))
    return len(children(helper)) >= 1


wait_for(first_slow_started, "first slow worker did not start")
fast_started = time.perf_counter()
fast = request(port, path="/fast")
fast_elapsed = time.perf_counter() - fast_started
if fast[0] != 200 or fast[2] != b"fast" or not slow_one.is_alive() or fast_elapsed >= 0.5:
    raise AssertionError((fast, fast_elapsed, slow_one.is_alive()))
slow_two, slow_two_result, slow_two_error = run_thread(lambda: request(port, path="/slow"))


def second_slow_started():
    if slow_two_error or slow_two_result:
        raise AssertionError((slow_two_error, slow_two_result))
    return len(children(helper)) >= 2


wait_for(second_slow_started, "second slow worker did not start")
cap_samples = {"workers": 0, "directories": 0}
sampling = threading.Event()
sampling.set()


def sample_cap():
    while sampling.is_set():
        cap_samples["workers"] = max(cap_samples["workers"], len(children(helper)))
        roots = [
            os.path.join(WORK, "tmp", name) for name in os.listdir(os.path.join(WORK, "tmp"))
            if name.startswith("nift-http-")
        ]
        cap_samples["directories"] = max(
            cap_samples["directories"],
            sum(len(os.listdir(root)) for root in roots if os.path.isdir(root)),
        )
        time.sleep(0.001)


cap_sampler = threading.Thread(target=sample_cap)
cap_sampler.start()
overload_started = time.perf_counter()
overload = request(port, path="/fast")
overload_elapsed = time.perf_counter() - overload_started
time.sleep(0.05)
sampling.clear()
cap_sampler.join()
if overload[0] != 503 or overload[2] != b"" or overload_elapsed >= 0.25:
    raise AssertionError((overload, overload_elapsed))
if cap_samples != {"workers": 2, "directories": 2}:
    raise AssertionError(("concurrency cap exceeded", cap_samples))
slow_one.join(5)
slow_two.join(5)
if slow_one_error or slow_two_error or slow_one_result[0][0] != 200 or slow_two_result[0][0] != 200:
    raise AssertionError((slow_one_error, slow_two_error, slow_one_result, slow_two_result))
finish_server(server, "overlap")


# A crash, timeout, and disconnected slow client must not block an independent
# fast request in another slot.
port = free_port()
server, helper = start_server("isolation", f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":6,"max_concurrency":4,"worker_timeout_ms":250}})
http.get(app, "/fast", (request) => http.text("fast"))
http.get(app, "/crash", (request) => {{ run("sh", "-c", "sleep 0.15"); return request.missing.value }})
http.get(app, "/timeout", (request) => {{ while(true) {{}} return http.text("never") }})
http.get(app, "/slow", (request) => {{ run("sh", "-c", "sleep 0.4"); return http.text("slow") }})
http.listen(app)
''')
crash_thread, crash_result, crash_error = run_thread(lambda: request(port, path="/crash"))
wait_for(lambda: len(children(helper)) >= 1, "crashing worker did not start")
if request(port, path="/fast")[0] != 200:
    raise AssertionError("fast request failed beside crash")
crash_thread.join(5)
if crash_error or crash_result[0][0] != 500:
    raise AssertionError((crash_error, crash_result))
timeout_thread, timeout_result, timeout_error = run_thread(lambda: request(port, path="/timeout"))
wait_for(lambda: len(children(helper)) >= 1, "timeout worker did not start")
if request(port, path="/fast")[0] != 200:
    raise AssertionError("fast request failed beside timeout")
timeout_thread.join(5)
if timeout_error or timeout_result[0][0] != 504:
    raise AssertionError((timeout_error, timeout_result))
disconnected = socket.create_connection(("127.0.0.1", port), timeout=3)
disconnected.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
wait_for(lambda: len(children(helper)) >= 1, "disconnected worker did not start")
disconnected.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
disconnected.close()
if request(port, path="/fast")[0] != 200:
    raise AssertionError("fast request failed beside disconnected client")
finish_server(server, "isolation")


# Eight concurrent uploads and downloads prove request/spool identity and file
# response isolation at the configured bound.
os.makedirs(os.path.join(WORK, "saved"), exist_ok=True)
asset = bytes(range(256)) * 1024
with open(os.path.join(WORK, "asset.bin"), "wb") as output:
    output.write(asset)
port = free_port()
server, helper = start_server("resources", f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":16,"max_concurrency":8,"max_body_bytes":262144,"max_file_bytes":131072,"max_temp_bytes":393216}})
http.post(app, "/upload", (request) => {{
    saved := http.save_upload(request.files.attachment, "saved/" + request.request_id + ".bin")
    run("sh", "-c", "sleep 0.15")
    return http.json({{"id":request.request_id,"ok":saved.ok}})
}})
http.get(app, "/file", (request) => {{ run("sh", "-c", "sleep 0.15"); return http.file("asset.bin") }})
http.listen(app)
''')
boundary = b"cp10-boundary"
payloads = [bytes([index]) * 65536 for index in range(8)]
barrier = threading.Barrier(9)
upload_results = []
upload_errors = []


def upload(index):
    try:
        body = multipart(boundary, payloads[index])
        barrier.wait()
        upload_results.append((index, request(
            port, "POST", "/upload", body,
            {"Content-Type": "multipart/form-data; boundary=cp10-boundary"},
        )))
    except Exception as exc:
        upload_errors.append(exc)


upload_threads = [threading.Thread(target=upload, args=(index,)) for index in range(8)]
for thread in upload_threads:
    thread.start()
barrier.wait()
peak_workers = 0
peak_directories = 0
while any(thread.is_alive() for thread in upload_threads):
    peak_workers = max(peak_workers, len(children(helper)))
    roots = [
        os.path.join(WORK, "tmp", name) for name in os.listdir(os.path.join(WORK, "tmp"))
        if name.startswith("nift-http-")
    ]
    peak_directories = max(
        peak_directories,
        sum(len(os.listdir(root)) for root in roots if os.path.isdir(root)),
    )
    time.sleep(0.002)
for thread in upload_threads:
    thread.join()
if upload_errors or len(upload_results) != 8 or peak_workers != 8 or peak_directories != 8:
    raise AssertionError((upload_errors, len(upload_results), peak_workers, peak_directories))
seen_ids = set()
for index, result in upload_results:
    if result[0] != 200:
        raise AssertionError(result)
    value = json.loads(result[2])
    if not value["ok"] or value["id"] in seen_ids:
        raise AssertionError(value)
    seen_ids.add(value["id"])
    with open(os.path.join(WORK, "saved", value["id"] + ".bin"), "rb") as source:
        if source.read() != payloads[index]:
            raise AssertionError("concurrent upload bytes crossed request directories")

barrier = threading.Barrier(9)
download_results = []


def download():
    barrier.wait()
    download_results.append(request(port, path="/file"))


download_threads = [threading.Thread(target=download) for _ in range(8)]
for thread in download_threads:
    thread.start()
barrier.wait()
download_peak_workers = 0
while any(thread.is_alive() for thread in download_threads):
    download_peak_workers = max(download_peak_workers, len(children(helper)))
    time.sleep(0.002)
for thread in download_threads:
    thread.join()
if download_peak_workers != 8 or len(download_results) != 8 or any(
    result[0] != 200 or result[2] != asset for result in download_results
):
    raise AssertionError("concurrent file responses failed")
finish_server(server, "resources", timeout=30)


# Abortive shutdown terminates every active worker process group and removes the
# shared temporary root only after all request handlers have unwound.
descendant_file = os.path.join(WORK, "descendants.txt")
port = free_port()
server, helper = start_server("shutdown", f'''@import("http")
app := http.server({{"host":"127.0.0.1","port":{port},"max_concurrency":4,"worker_timeout_ms":30000}})
http.get(app, "/hang", (request) => {{
    run("sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $! >> descendants.txt")
    while(true) {{}}
    return http.text("never")
}})
http.listen(app)
''')
shutdown_barrier = threading.Barrier(5)
shutdown_errors = []


def hanging_client():
    try:
        shutdown_barrier.wait()
        request(port, path="/hang", timeout=35)
    except Exception as exc:
        shutdown_errors.append(exc)


clients = [threading.Thread(target=hanging_client) for _ in range(4)]
for client in clients:
    client.start()
shutdown_barrier.wait()
worker_pids = wait_for(
    lambda: children(helper) if len(children(helper)) == 4 else None,
    "four shutdown workers did not start",
)


def descendant_pids():
    try:
        with open(descendant_file, encoding="ascii") as source:
            values = [int(line) for line in source.read().split()]
        return values if len(values) == 4 else None
    except OSError:
        return None


spawned_descendants = wait_for(descendant_pids, "worker descendants did not start")
os.kill(helper, signal.SIGTERM)
for client in clients:
    client.join(5)
if any(client.is_alive() for client in clients):
    raise AssertionError("clients survived helper shutdown")
finish_server(server, "shutdown")
for pid in worker_pids + spawned_descendants:
    if os.path.exists(f"/proc/{pid}"):
        raise AssertionError(f"worker process survived shutdown: {pid}")
if any(name.startswith("nift-http-") for name in os.listdir(os.path.join(WORK, "tmp"))):
    raise AssertionError("temporary root survived shutdown")

print("PASS http CP10 bounded concurrent one-shot workers")
