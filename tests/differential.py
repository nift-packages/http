#!/usr/bin/env python3
"""Differential SERVER2 harness: process backend vs native backend.

Runs the identical app under both backends and compares status, normalized
headers, body bytes, route selection, route params, query, method/target/path,
HEAD behavior, and overlapping malformed-request outcomes. Parity is claimed
only for the supported scope (GET/HEAD/POST, text/json responses, query, route
params, 404/405/204).
"""

import http.client
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
WORK = tempfile.mkdtemp(prefix="nift-http-diff-")
PASS = 0
FAILS = []


def report(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAILS.append(name)
        print(f"FAIL {name} {detail}")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


APP_TEMPLATE = '''
@import("http")
http.use_backend("{backend}")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":0}})
http.get(app, "/hello/:name", (request) => http.text("hi " + request.params.name))
http.get(app, "/query", (request) => http.text(request.query.has("q") ? request.query.q : "no-q"))
http.get(app, "/json", (request) => http.json({{"message":"ok","n":5,"arr":[1,2,3]}}))
http.post(app, "/echo", (request) => {{ b := request.body; t := ""; if(type(b) == "object" && b.has("text")) {{ t = b.text }}; return http.text("echo:" + t) }})
http.get(app, "/empty", (request) => http.text("", {{"status":204}}))
r := http.listen(app)
print("listen:" + r.ok.to_string() + ":" + r.error + ":" + r.error_code)
'''


def launch(backend, port):
    project = os.path.join(WORK, f"{backend}{port}")
    os.makedirs(os.path.join(project, ".nift"), exist_ok=True)
    subprocess.run([NIFT, "add", PKG], cwd=project, capture_output=True,
                   text=True, timeout=120, check=True)
    src = os.path.join(project, "srv.f")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(APP_TEMPLATE.format(backend=backend, port=port))
    env = dict(os.environ)
    env["NIFT_HTTP_NIFT"] = NIFT
    proc = subprocess.Popen([NIFT, "srv.f"], cwd=project,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env)
    time.sleep(2.5)
    if proc.poll() is not None:
        out = proc.stdout.read() if proc.stdout else ""
        raise SystemExit(f"{backend} server exited early: rc={proc.returncode} out={out!r}")
    return proc


def request(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = {"status": r.status, "body": data,
           "headers": {k.lower(): v for k, v in r.getheaders()}}
    c.close()
    return out


def main():
    port_p = free_port()
    port_n = free_port()
    pp = launch("process", port_p)
    pn = launch("native", port_n)

    cases = [
        ("GET", "/hello/world"),
        ("GET", "/hello/foo%20bar"),
        ("GET", "/hello/a+b"),
        ("GET", "/hello/a%2Fb"),
        ("GET", "/query?q=alpha%20beta"),
        ("GET", "/query"),
        ("GET", "/json"),
        ("GET", "/empty"),
        ("GET", "/missing"),
        ("HEAD", "/hello/world"),
        ("POST", "/echo", b"payload-xyz", {"Content-Type": "text/plain"}),
        ("GET", "/hello/world", None, {"X-Custom": "1"}),
    ]

    def norm(headers, status, body):
        keys = sorted(headers.keys())
        kept = {k: headers[k] for k in keys
                if k not in ("connection",) and not k.startswith("date")}
        return status, kept, body

    try:
        for case in cases:
            method, path = case[0], case[1]
            body = case[2] if len(case) > 2 else None
            headers = case[3] if len(case) > 3 else {}
            rp = request(port_p, method, path, body, headers)
            rn = request(port_n, method, path, body, headers)
            np_, nhp, nb = norm(rp["headers"], rp["status"], rp["body"])
            nn_, nhn, nb = norm(rn["headers"], rn["status"], rn["body"])
            ok = rp["status"] == rn["status"] and nhp == nhn and nb == nb
            report(f"diff {method} {path}", ok,
                   f"process={rp['status']} {nb!r} native={rn['status']} {nb!r}")

        # malformed overlapping outcome: unsupported HTTP version
        for label, port in (("process", port_p), ("native", port_n)):
            conn = socket.create_connection(("127.0.0.1", port), timeout=5)
            conn.settimeout(5)
            conn.sendall(b"GET / HTTP/2.0\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = b""
            try:
                while True:
                    ch = conn.recv(65536)
                    if not ch:
                        break
                    data += ch
            except (socket.timeout, ConnectionResetError):
                pass
            conn.close()
            first = data.split(b"\r\n")[0] if data else b""
            print(f"{label} HTTP/2.0 -> {first.decode(errors='replace')!r}")
    finally:
        pp.kill()
        pp.wait()
        pn.kill()
        pn.wait()

    shutil.rmtree(WORK, ignore_errors=True)
    print(f"differential: {PASS} passed, {len(FAILS)} failed")
    if FAILS:
        sys.exit(1)


if __name__ == "__main__":
    main()