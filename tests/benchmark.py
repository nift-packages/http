#!/usr/bin/env python3
"""SERVER3-PERF native HTTP benchmark harness.

Measures native backend throughput and latency under sustained concurrent
load. A run whose requests all fail (2xx == 0) is INVALID and is never counted
as a throughput/latency result; such a run aborts the harness so invalid data
cannot appear in performance results.
"""

import http.client
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
WORK = tempfile.mkdtemp(prefix="nift-http-bench-")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def launch(port, poll_timeout=10):
    project = os.path.join(WORK, f"p{port}")
    os.makedirs(os.path.join(project, ".nift"), exist_ok=True)
    subprocess.run([NIFT, "add", PKG], cwd=project, capture_output=True,
                   text=True, timeout=120, check=True)
    app = f'''
@import("http")
http.use_backend("native")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":0,"poll_timeout_ms":{poll_timeout}}})
http.get(app, "/text", (request) => http.text("hello-benchmark-0123456789"))
http.get(app, "/json/:id", (request) => http.json({{"id":request.params.id,"ok":true}}))
http.post(app, "/echo", (request) => {{ b := request.body; t := ""; if(type(b) == "object" && b.has("text")) {{ t = b.text }}; return http.text(t) }})
http.listen(app)
'''
    src = os.path.join(project, "srv.f")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(app)
    proc = subprocess.Popen([NIFT, "srv.f"], cwd=project,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    time.sleep(2.0)
    if proc.poll() is not None:
        raise SystemExit(f"server exited early: "
                         f"{proc.stdout.read() if proc.stdout else ''}")
    return proc


def run_load(port, concurrency, duration, path, method="GET", body=None):
    stop = time.time() + duration
    ok = 0
    latencies = []
    lock = threading.Lock()

    def worker():
        nonlocal ok
        while time.time() < stop:
            try:
                t0 = time.time()
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request(method, path, body=body,
                          headers={"Connection": "close"})
                r = c.getresponse()
                r.read()
                c.close()
                if 200 <= r.status < 300:
                    with lock:
                        ok += 1
                        latencies.append((time.time() - t0) * 1000)
            except Exception:
                pass

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = duration
    reqs = ok / elapsed
    if ok == 0:
        raise SystemExit("INVALID benchmark run: 2xx == 0 "
                         "(server not serving); never counted as a result")
    lat = sorted(latencies)
    p50 = lat[len(lat) // 2]
    p95 = lat[int(len(lat) * 0.95)]
    p99 = lat[int(len(lat) * 0.99)]
    return reqs, p50, p95, p99, ok


def main():
    port = free_port()
    proc = launch(port)
    try:
        print(f"{'concurrency':>12} {'req/s':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'ok':>6}")
        for c in (1, 4, 16, 32):
            reqs, p50, p95, p99, ok = run_load(port, c, 5, "/text")
            print(f"{c:>12} {reqs:>8.1f} {p50:>7.1f}ms {p95:>7.1f}ms {p99:>7.1f}ms {ok:>6}")
    finally:
        proc.kill()
        proc.wait()
    print("benchmark harness OK (no invalid 2xx==0 runs)")


if __name__ == "__main__":
    main()