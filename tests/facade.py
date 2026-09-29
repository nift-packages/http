#!/usr/bin/env python3
"""Named-method HTTP facade and module-isolation coverage."""

import os
import re
import shutil
import subprocess
import sys


NIFT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "nift")
PKG = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, ".facade-work")
SOURCE = os.path.join(PKG, "src", "http.f")

with open(SOURCE, encoding="utf-8") as source_file:
    package_source = source_file.read()

if "\nstruct(http) {\n" not in package_source or "@struct(http)" in package_source:
    raise SystemExit("FAIL HTTP facade is not a bare struct")
public_methods = re.findall(r"^    fn\(([A-Za-z_][A-Za-z0-9_]*)\(", package_source, re.MULTILINE)
private_methods = re.findall(r"^    private fn\(([A-Za-z_][A-Za-z0-9_]*)\(", package_source, re.MULTILINE)
expected_signatures = {
    "available": "", "backends": "", "backend": "", "use_backend": "name",
    "capabilities": "", "server": "config", "server_backend": "app",
    "route": "app, method, path, handler", "get": "app, path, handler",
    "post": "app, path, handler", "put": "app, path, handler",
    "patch": "app, path, handler", "delete": "app, path, handler",
    "head": "app, path, handler", "text": "body, ...options",
    "json": "value, ...options", "cookie": "cookie_name, cookie_value, ...options",
    "save_upload": "upload, destination", "save_body": "body, destination",
    "file": "path, ...options", "file_from": "root, path, ...options",
    "stream": "producer, ...options", "listen": "app",
}
actual_signatures = dict(re.findall(
    r"^    fn\(([A-Za-z_][A-Za-z0-9_]*)\(([^)]*)\)\)", package_source, re.MULTILINE,
))
if actual_signatures != expected_signatures or len(public_methods) != 23:
    raise SystemExit(f"FAIL public method surface: {public_methods!r}")
if len(private_methods) != 20:
    raise SystemExit(f"FAIL private method count: {private_methods!r}")
if re.search(r"^    (?:private )?fn\(http_", package_source, re.MULTILINE):
    raise SystemExit("FAIL legacy module helper remains")
if package_source.count("=>") != 1 or "write_chunk := (chunk) =>" not in package_source:
    raise SystemExit("FAIL unexpected package closure inventory")

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, ".nift"), exist_ok=True)
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)

hostile_names = [
    "http_backend_requested", "http_backend_locked", "http_backend_selected",
    "http_next_server_id", "http_server_backends", "http_server_configs",
    "http_server_route_counts", "http_server_routes", "http_spool_paths",
    "http_worker_response_path", "http_worker_stream_path",
    "http_worker_stream_done_path", "http_worker_request_id",
    "http_worker_request_method", "http_worker_stream_published",
    *private_methods,
    "override", "python", "config", "backend", "id", "app", "method",
    "path", "handler", "method_char", "token_chars", "index", "key", "pattern",
    "segments", "parts", "params", "i", "part", "param_key", "options", "kind",
    "headers", "header_name", "header_value", "lower_header", "body",
    "option_values", "status", "response", "value", "cookie_name", "cookie_value",
    "cookie", "root", "producer", "public_response", "envelope", "stream_output",
    "write_chunk", "chunk", "allowed", "selected_index", "fallback_index",
    "selected_params", "fallback_params", "route", "request", "private_request",
    "request_path", "response_path", "stream_path", "stream_done_path", "destination",
    "upload", "temporary_path", "output", "exchange", "fallback", "nift", "host",
    "port", "result",
]
hostile_names = list(dict.fromkeys(hostile_names))
hostile_bindings = "\n".join(f'{name} := "consumer-{name}"' for name in hostile_names)

facade_source = f'''@import("http")
{hostile_bindings}
fn(expect(condition, label)) {{ if(!condition) {{ print("FAIL " + label); missing.value }} }}

expect(http.available(), "available")
expect(http.backends().stringify() == "[\\"process\\"]", "backends")
expect(http.backend() == "process", "backend")
expect(http.use_backend("auto").ok, "use_backend")
caps := http.capabilities()
expect(caps.buffered_text && caps.json && caps.forms && caps.cookies &&
       caps.binary_files && caps.spooled_bodies && caps.uploads && caps.multipart &&
       caps.persistent_workers && !caps.websockets && !caps.tls, "capabilities")
expect(caps.streaming == (os() != "windows"), "streaming capability")

direct := http()
shallow := copy(http)
deep := deepcopy(http)
server_a := direct.server({{"port":8101}})
server_b := shallow.server({{"port":8102}})
server_c := deep.server({{"port":8103}})
expect(server_a._server_id == 1 && server_b._server_id == 2 && server_c._server_id == 3, "shared unique IDs")
expect(http.server_backend(server_a) == "process" && direct.server_backend(server_b) == "process" &&
       deep.server_backend(server_c) == "process", "shared backend state")

secret := "consumer-secret"
callback := (request_value) => secret + ":" + request_value.params.id
expect(http.route(server_a, "OPTIONS", "/generic/:id", callback).ok, "route")
expect(http.get(server_a, "/get", callback).ok, "get")
expect(http.post(server_a, "/post", callback).ok, "post")
expect(http.put(server_a, "/put", callback).ok, "put")
expect(http.patch(server_a, "/patch", callback).ok, "patch")
expect(http.delete(server_a, "/delete", callback).ok, "delete")
expect(http.head(server_a, "/head", callback).ok, "head")
expect(callback({{"params":{{"id":"lexical"}}}}) == "consumer-secret:lexical", "callback lexical scope")

text_response := http.text("body", {{"status":201,"headers":{{"X-Test":"yes"}}}})
expect(text_response.status == 201 && text_response.body.text == "body" &&
       text_response.headers.get("x-test") == "yes", "text variadic")
json_response := http.json({{"ok":true}}, {{"status":202}})
expect(json_response.status == 202 && json_response.body.value.ok, "json variadic")
cookie_response := http.cookie("theme", "dark", {{"path":"/","http_only":true}})
expect(cookie_response.name == "theme" && cookie_response.path == "/" && cookie_response.http_only, "cookie variadic")
file_response := http.file("asset.bin", {{"status":206,"download_name":"out.bin"}})
expect(file_response.status == 206 && file_response.body.path == "asset.bin" &&
       file_response.body.download_name == "out.bin", "file variadic")
root_response := http.file_from("root", "asset.bin", {{"content_type":"application/test"}})
expect(root_response.body.root == "root" && root_response.body.path == "asset.bin" &&
       root_response.headers.get("content-type")[0] == "application/test", "file_from variadic")

captured := ""
stream_response := http.stream((write) => {{ write(secret) }}, {{"status":203}})
stream_response.body._producer((piece) => {{ captured = piece }})
expect(stream_response.status == 203 && captured == "consumer-secret", "stream callback lexical scope")
expect(http.save_upload({{}}, "unused").error_code == "invalid_upload", "save_upload")
expect(http.save_body({{}}, "unused").error_code == "invalid_body", "save_body")
expect(http.listen({{}}).error_code == "invalid_handle", "listen")
print("PASS http named-method facade")
'''
with open(os.path.join(WORK, "facade.f"), "w", encoding="utf-8") as output:
    output.write(facade_source)

environment = {**os.environ, "NIFT_HTTP_NIFT": NIFT}
facade = subprocess.run(
    [NIFT, "facade.f"], cwd=WORK, env=environment,
    capture_output=True, text=True, timeout=20,
)
if facade.returncode != 0 or facade.stdout.strip() != "PASS http named-method facade":
    raise SystemExit(f"FAIL facade execution: rc={facade.returncode} stdout={facade.stdout!r} stderr={facade.stderr!r}")

for name in private_methods:
    with open(os.path.join(WORK, "private.f"), "w", encoding="utf-8") as output:
        output.write(f'@import("http")\nhttp.{name}()\n')
    private = subprocess.run([NIFT, "private.f"], cwd=WORK, env=environment, capture_output=True, text=True)
    if private.returncode == 0 or f"private struct method: {name}" not in private.stderr:
        raise SystemExit(f"FAIL private method visible: {name}: {private.stdout!r} {private.stderr!r}")

for name in hostile_names[:15]:
    with open(os.path.join(WORK, "global.f"), "w", encoding="utf-8") as output:
        output.write(f'@import("http")\nprint({name})\n')
    private = subprocess.run([NIFT, "global.f"], cwd=WORK, env=environment, capture_output=True, text=True)
    if private.returncode == 0:
        raise SystemExit(f"FAIL module global visible: {name}")

with open(os.path.join(WORK, "extract.f"), "w", encoding="utf-8") as output:
    output.write('@import("http")\nmember := http.text\nprint(member("body"))\n')
extraction = subprocess.run([NIFT, "extract.f"], cwd=WORK, env=environment, capture_output=True, text=True)
if extraction.returncode == 0:
    raise SystemExit("FAIL named method remained extractable as a first-class member")

print("PASS http facade isolation")
