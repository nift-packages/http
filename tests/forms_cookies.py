#!/usr/bin/env python3
"""CP07 URL-encoded form and cookie contract tests."""

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time


NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, ".dogfood-work")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, ".nift"), exist_ok=True)
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


port = free_port()
source = f'''@import("http")
app := http.server({{
    "host":"127.0.0.1","port":{port},"max_requests":13,
    "max_form_fields":4,"max_form_name_bytes":8,"max_form_value_bytes":16,
    "max_cookie_pairs":4
}})
http.post(app, "/form", (request) => http.json(request.form))
http.post(app, "/plain", (request) => http.json(request.form))
http.get(app, "/cookies", (request) => http.json(request.cookies))
http.get(app, "/set", (request) => http.text("set", {{"cookies":[
    http.cookie("theme", "dark", {{"path":"/","max_age":60,"http_only":true,"same_site":"Lax"}}),
    http.cookie("__Secure-id", "abc", {{"secure":true,"expires":"Wed, 21 Oct 2015 07:28:00 GMT"}})
]}}))
http.get(app, "/bad-cookie", (request) => http.text("bad", {{"cookies":[
    http.cookie("__Host-id", "abc", {{"path":"/"}})
]}}))
http.listen(app)
'''
with open(os.path.join(WORK, "app.f"), "w", encoding="utf-8") as output:
    output.write(source)

server = subprocess.Popen(
    [NIFT, "app.f"], cwd=WORK,
    env={**os.environ, "NIFT_HTTP_NIFT": NIFT},
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)


def request(method, path, body=None, headers=None):
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = response.status, response.getheaders(), response.read()
            connection.close()
            return result
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def raw(payload):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
        client.sendall(payload)
        output = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            output.extend(chunk)
    status = int(bytes(output).split(b" ", 2)[1])
    return status, bytes(output)


try:
    form = request(
        "POST", "/form", b"a=one&a=two&blank=&utf=%E2%9C%93",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    if form[0] != 200 or json.loads(form[2]) != {"a": ["one", "two"], "blank": "", "utf": "✓"}:
        raise AssertionError(form)
    empty = request(
        "POST", "/form", b"",
        {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    if empty[0] != 200 or json.loads(empty[2]) != {}:
        raise AssertionError(empty)
    plain = request("POST", "/plain", b"a=one", {"Content-Type": "text/plain"})
    if plain[0] != 200 or json.loads(plain[2]) != {}:
        raise AssertionError(plain)
    for malformed in (b"a=%ZZ", b"a=%FF"):
        result = request("POST", "/form", malformed, {"Content-Type": "application/x-www-form-urlencoded"})
        if result[0] != 400:
            raise AssertionError(result)
    too_many = request("POST", "/form", b"a=1&b=2&c=3&d=4&e=5", {"Content-Type": "application/x-www-form-urlencoded"})
    if too_many[0] != 400:
        raise AssertionError(too_many)
    long_name = request("POST", "/form", b"123456789=x", {"Content-Type": "application/x-www-form-urlencoded"})
    if long_name[0] != 400:
        raise AssertionError(long_name)
    long_value = request("POST", "/form", b"a=12345678901234567", {"Content-Type": "application/x-www-form-urlencoded"})
    if long_value[0] != 400:
        raise AssertionError(long_value)
    cookies = request("GET", "/cookies", headers={"Cookie": "a=1; a=2; empty="})
    if cookies[0] != 200 or json.loads(cookies[2]) != {"a": ["1", "2"], "empty": ""}:
        raise AssertionError(cookies)
    malformed_cookie = request("GET", "/cookies", headers={"Cookie": "good=1; broken"})
    if malformed_cookie[0] != 400:
        raise AssertionError(malformed_cookie)
    response_cookies = request("GET", "/set")
    set_cookies = [value for key, value in response_cookies[1] if key.lower() == "set-cookie"]
    if response_cookies[0] != 200 or set_cookies != [
        "theme=dark; Path=/; Max-Age=60; SameSite=Lax; HttpOnly",
        "__Secure-id=abc; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Secure",
    ]:
        raise AssertionError(response_cookies)
    invalid_response = request("GET", "/bad-cookie")
    if invalid_response[0] != 500:
        raise AssertionError(invalid_response)
    duplicate_type = raw(
        b"POST /form HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n"
        b"Content-Type: text/plain\r\nContent-Length: 3\r\n\r\na=1"
    )
    if duplicate_type[0] != 400:
        raise AssertionError(duplicate_type)
finally:
    stdout, stderr = server.communicate(timeout=20)

if server.returncode != 0:
    raise SystemExit(f"FAIL CP07 server: {stdout!r} {stderr!r}")
print("PASS http CP07 forms and cookies")
