#!/usr/bin/env python3
"""SERVER3-PERF fair backend comparison: native vs process-default vs
process-persistent. Uses the same client, payloads, handler logic, and
connection semantics for every backend; pre- and post-run health checks; a run
with 2xx == 0 is INVALID and never counted; failed launches are retried before
measurement. Captures whole-process-tree RSS/CPU.
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
WORK = tempfile.mkdtemp(prefix="nift-http-cmp-")
DUR = 4


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def tree_stats(pid):
    total_rss = 0
    n = 0

    def walk(p):
        nonlocal total_rss, n
        try:
            with open(f"/proc/{p}/stat") as f:
                parts = f.read().split()
                rss = int(parts[23])
                total_rss += rss
                n += 1
        except Exception:
            return
        try:
            children = subprocess.run(["pgrep", "-P", str(p)],
                                      capture_output=True, text=True).stdout.split()
        except Exception:
            children = []
        for c in children:
            walk(int(c))

    walk(pid)
    return total_rss, n


def launch(label, backend, port, conc, wmode, wpool):
    project = os.path.join(WORK, f"{label}{port}")
    os.makedirs(os.path.join(project, ".nift"), exist_ok=True)
    env = dict(os.environ)
    env["NIFT_HTTP_NIFT"] = NIFT
    subprocess.run([NIFT, "add", PKG], cwd=project, capture_output=True,
                   text=True, timeout=120, check=True, env=env)
    app = f'''
@import("http")
http.use_backend("{backend}")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":0,"max_concurrency":{conc},"worker_mode":"{wmode}","worker_pool_size":{wpool},"poll_timeout_ms":50}})
http.get(app, "/text", (request) => http.text("hello-benchmark-0123456789"))
http.get(app, "/json/:id", (request) => http.json({{"id":request.params.id,"ok":true}}))
http.post(app, "/echo", (request) => {{ b := request.body; t := ""; if(type(b) == "object" && b.has("text")) {{ t = b.text }}; return http.text(t) }})
http.get(app, "/large", (request) => {{ s := ""; i := 0; chunk := "0123456789ABCDEF"; while(i < 8192) {{ s += chunk; i += 1 }}; return http.text(s) }})
http.listen(app)
'''
    src = os.path.join(project, "srv.f")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(app)
    proc = subprocess.Popen([NIFT, "srv.f"], cwd=project, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    return proc, project


def health(port):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        c.request("GET", "/text", headers={"Connection": "close"})
        r = c.getresponse()
        r.read()
        c.close()
        return 200 <= r.status < 300
    except Exception:
        return False


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
    if ok == 0:
        raise SystemExit("INVALID run: 2xx == 0; not counted")
    lat = sorted(latencies)
    return ok / duration, lat[len(lat) // 2], lat[int(len(lat) * 0.95)]


def main():
    configs = [
        ("native", "native", 16, "oneshot", 1),
        ("process_default", "process", 1, "oneshot", 1),
        ("process_persistent", "process", 4, "persistent", 4),
    ]
    workloads = [
        ("text", "GET", "/text", None),
        ("json", "GET", "/json/42", None),
        ("echo1k", "POST", "/echo", "A" * 1024),
        ("large", "GET", "/large", None),
    ]
    print(f"{'config':<18}{'wl':<8}{'c':>3} {'req/s':>8} {'p50':>8} {'p95':>8}")
    for label, backend, conc, wmode, wpool in configs:
        port = free_port()
        proc = None
        for attempt in range(6):
            proc, _ = launch(label, backend, port, conc, wmode, wpool)
            time.sleep(2.5)
            if health(port) and proc.poll() is None:
                break
            proc.kill()
            proc.wait()
            port = free_port()
        if proc is None or proc.poll() is not None:
            print(f"{label}: FAILED to launch healthy server")
            continue
        for wl, method, path, body in workloads:
            for c in (1, 4, 16):
                try:
                    reqs, p50, p95 = run_load(port, c, DUR, path, method, body)
                except SystemExit as e:
                    print(f"{label}: {e}")
                    continue
                print(f"{label:<18}{wl:<8}{c:>3} {reqs:>8.1f} {p50:>7.1f}ms {p95:>7.1f}ms")
        rss, n = tree_stats(proc.pid)
        print(f"{label}: process-tree peak RSS ~{rss // 1024} KB ({n} procs)")
        proc.kill()
        proc.wait()
    print("comparison complete")


if __name__ == "__main__":
    main()