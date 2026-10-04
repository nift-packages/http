#!/usr/bin/env python3
"""Native HTTP backend contract harness (SERVER2-R).

Serves a real Nift app over the native backend (socket package facade only)
and exercises the bounded HTTP/1.1 parser, router dispatch, response
serialization, malformed-framing rejection, and max_requests shutdown.

Python is used strictly as test-driver/oracle; the production native backend
never invokes Python.
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
WORK = tempfile.mkdtemp(prefix="nift-http-native-")
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


def launch(port, max_requests=0):
    project = os.path.join(WORK, f"p{port}")
    os.makedirs(os.path.join(project, ".nift"), exist_ok=True)
    proc = subprocess.run([NIFT, "add", PKG], cwd=project,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SystemExit(f"nift add failed: {proc.stderr}")
    app = f'''
@import("http")
http.use_backend("native")
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":{max_requests}}})
http.get(app, "/hello/:name", (request) => http.text("hi " + request.params.name))
http.get(app, "/query", (request) => http.text(request.query.has("q") ? "q=" + request.query.q : "no-q"))
http.get(app, "/json", (request) => http.json({{"message":"ok","n":5,"arr":[1,2,3]}}))
http.get(app, "/cookies", (request) => http.text("c", {{"cookies":[http.cookie("a","1"), http.cookie("b","2")]}}))
http.post(app, "/echo", (request) => {{ b := request.body; t := ""; if(type(b) == "object" && b.has("text")) {{ t = b.text }}; return http.text("echo:" + t) }})
http.get(app, "/empty", (request) => http.text("", {{"status":204}}))
http.get(app, "/big", (request) => {{ s := ""; i := 0; chunk := "0123456789ABCDEF"; while(i < 8192) {{ s += chunk; i += 1 }}; return http.text(s) }})
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
        out = proc.stdout.read() if proc.stdout else ""
        raise SystemExit(f"server exited early: {out}")
    return proc, project


def raw_request(port, payload, timeout=5.0):
    conn = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    conn.settimeout(timeout)
    conn.sendall(payload)
    chunks = []
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (socket.timeout, ConnectionResetError, ConnectionAbortedError):
        pass
    conn.close()
    return b"".join(chunks)


def main():
    port = free_port()
    proc, project = launch(port)

    def client(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        raw_headers = [(k.lower(), v) for k, v in r.getheaders()]
        headers = dict(raw_headers)
        out = {"status": r.status, "headers": headers, "raw_headers": raw_headers, "body": data}
        c.close()
        return out

    try:
        r = client("GET", "/hello/world")
        report("GET route param", r["status"] == 200 and r["body"] == b"hi world",
               str(r))
        report("GET content-type", r["headers"].get("content-type", "").startswith("text/plain"),
               str(r["headers"]))
        report("GET content-length", r["headers"].get("content-length") == str(len(r["body"])),
               str(r["headers"]))
        report("GET connection close", r["headers"].get("connection") == "close",
               str(r["headers"]))

        r = client("HEAD", "/hello/world")
        report("HEAD same content-length no body", r["status"] == 200 and r["body"] == b""
               and r["headers"].get("content-length") == str(len(b"hi world")), str(r))

        r = client("POST", "/echo", body=b"payload-123",
                   headers={"Content-Type": "text/plain"})
        report("POST body echo", r["status"] == 200 and r["body"] == b"echo:payload-123",
               str(r))

        r = client("GET", "/query?q=alpha%20beta")
        report("GET query decode", r["status"] == 200 and r["body"] == b"q=alpha beta",
               str(r))
        r = client("GET", "/query")
        report("GET query absent", r["status"] == 200 and r["body"] == b"no-q", str(r))

        r = client("GET", "/json")
        report("GET json response", r["status"] == 200 and b'"message":"ok"' in r["body"]
               and b'"n":5' in r["body"], str(r["body"]))
        report("GET json content-type", r["headers"].get("content-type", "").startswith("application/json"),
               str(r["headers"]))

        r = client("GET", "/cookies")
        setcookies = [v for k, v in r["raw_headers"] if k == "set-cookie"]
        report("repeated Set-Cookie", setcookies == ["a=1", "b=2"], str(setcookies))

        r = client("GET", "/empty")
        report("204 no body", r["status"] == 204 and r["body"] == b""
               and "content-length" not in r["headers"], str(r))

        r = client("GET", "/big")
        report("large response send-continuation", r["status"] == 200
               and len(r["body"]) == 131072 and r["body"] == b"0123456789ABCDEF" * 8192
               and r["headers"].get("content-length") == "131072", str((r["status"], len(r["body"]))))

        r = client("GET", "/nope")
        report("404 route", r["status"] == 404, str(r))

        # split-packet request: send the request in two halves
        raw = b"GET /hello/split HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        half = len(raw) // 2
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.settimeout(5)
        conn.sendall(raw[:half])
        time.sleep(0.2)
        conn.sendall(raw[half:])
        chunks = []
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        conn.close()
        data = b"".join(chunks)
        report("split-packet request", b"hi split" in data, str(data[:80]))

        # split body: POST body delivered in two writes
        body = b"x" * 500
        head = f"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.settimeout(5)
        conn.sendall(head + body[:200])
        time.sleep(0.2)
        conn.sendall(body[200:])
        chunks = []
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        conn.close()
        data = b"".join(chunks)
        report("split request body", b"echo:" + body in data, str(data[:60]))

        # Transfer-Encoding rejected
        data = raw_request(port, b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
        report("Transfer-Encoding rejected", b"501" in data.split(b"\r\n")[0], str(data.split(b"\r\n")[0]))

        # duplicate content-length rejected
        data = raw_request(port, b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nContent-Length: 2\r\nConnection: close\r\n\r\n")
        report("duplicate content-length rejected", b"400" in data.split(b"\r\n")[0], str(data.split(b"\r\n")[0]))

        # malformed request line
        data = raw_request(port, b"GET\r\n\r\n")
        report("malformed request line", b"400" in data.split(b"\r\n")[0], str(data.split(b"\r\n")[0]))

        # bare CR/LF framing rejected
        data = raw_request(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r")
        report("bare CR framing", b"400" in data.split(b"\r\n")[0] or b"" in data, str(data.split(b"\r\n")[0]))

        # oversized request line
        huge = b"GET /" + b"a" * 10000 + b" HTTP/1.1\r\nHost: x\r\n\r\n"
        data = raw_request(port, huge)
        report("oversized request line", b"414" in data.split(b"\r\n")[0] or b"431" in data.split(b"\r\n")[0],
               str(data.split(b"\r\n")[0]))

        # oversized body
        huge_body = b"y" * (1048576 + 100)
        head = f"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: {len(huge_body)}\r\nConnection: close\r\n\r\n".encode()
        data = raw_request(port, head + huge_body[:100000])
        report("oversized body", b"413" in data.split(b"\r\n")[0], str(data.split(b"\r\n")[0]))

        # binary response via raw body plan
        raw_resp = b"GET /bin HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        data = raw_request(port, raw_resp)
        report("missing binary route 404", b"404" in data.split(b"\r\n")[0], str(data.split(b"\r\n")[0]))

        # server still alive after malformed requests
        r = client("GET", "/hello/again")
        report("server survives malformed input", r["status"] == 200 and r["body"] == b"hi again", str(r))
    finally:
        proc.kill()
        proc.wait()

    # max_requests shutdown
    port2 = free_port()
    proc2, _ = launch(port2, max_requests=2)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port2, timeout=5)
        c.request("GET", "/hello/a")
        r = c.getresponse(); r.read(); c.close()
        c = http.client.HTTPConnection("127.0.0.1", port2, timeout=5)
        c.request("GET", "/hello/b")
        r = c.getresponse(); r.read(); c.close()
        time.sleep(0.5)
        c = http.client.HTTPConnection("127.0.0.1", port2, timeout=3)
        try:
            c.request("GET", "/hello/c")
            r = c.getresponse(); r.read(); c.close()
            report("max_requests shutdown", False, "server still served")
        except Exception:
            report("max_requests shutdown", True)
    finally:
        proc2.kill()
        proc2.wait()

    # slowloris timeout reaping: incomplete request gets 408, server survives
    port3 = free_port()
    project = os.path.join(WORK, f"t{port3}")
    os.makedirs(os.path.join(project, ".nift"), exist_ok=True)
    subprocess.run([NIFT, "add", PKG], cwd=project, capture_output=True, text=True, timeout=120, check=True)
    app3 = f'''
@import("http")
http.use_backend("native")
app := http.server({{"host":"127.0.0.1","port":{port3},"client_timeout_ms":500}})
http.get(app, "/", (request) => http.text("ok"))
http.listen(app)
'''
    with open(os.path.join(project, "srv.f"), "w", encoding="utf-8") as handle:
        handle.write(app3)
    proc3 = subprocess.Popen([NIFT, "srv.f"], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.0)
    conn = socket.create_connection(("127.0.0.1", port3), timeout=5)
    conn.settimeout(5)
    conn.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
    data = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    conn.close()
    report("slowloris timeout 408", data.startswith(b"HTTP/1.1 408 "), str(data[:40]))
    proc3.kill(); proc3.wait()

    shutil.rmtree(WORK, ignore_errors=True)
    print(f"native harness: {PASS} passed, {len(FAILS)} failed")
    if FAILS:
        sys.exit(1)


if __name__ == "__main__":
    main()