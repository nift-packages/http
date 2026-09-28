#!/usr/bin/env python3
"""CP09 realistic CRUD dogfood using deterministic file persistence."""

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
os.makedirs(os.path.join(WORK, "attachments"), exist_ok=True)
subprocess.run([NIFT, "add", PKG], cwd=WORK, check=True, capture_output=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def multipart(boundary, filename, content):
    return (
        b"--" + boundary + b"\r\n"
        b"Content-Disposition: form-data; name=\"attachment\"; filename=\"" + filename.encode() + b"\"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n" + content + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )


def app_source(port, max_requests):
    return f'''@import("http")
fn(crud_save_state(state)) {{
    state_file := file("items.json")
    state_file.open("w")
    state_file.write_val(state)
    state_file.save()
    state_file.close()
}}
fn(crud_load_state()) {{
    loaded := inject("items.json")
    return loaded
}}
fn(crud_not_found()) {{
    response := http.json({{"error":"not_found"}}, {{"status":404}})
    return response
}}
if(!exists("items.json")) {{ crud_save_state({{"next_id":1,"items":{{}}}}) }}
ephemeral_counter := 0
app := http.server({{"host":"127.0.0.1","port":{port},"max_requests":{max_requests},"max_body_bytes":1048576,"max_file_bytes":524288}})
http.get(app, "/items", (request) => http.json(crud_load_state().items))
http.post(app, "/items", (request) => {{
    state := crud_load_state()
    item_id := state.next_id.to_string()
    item := {{"id":item_id,"title":request.json.title,"attachment":false}}
    items := state.items
    items[item_id] = item
    state["items"] = items
    state["next_id"] = state.next_id + 1
    crud_save_state(state)
    return http.json(item, {{"status":201,"cookies":[http.cookie("last_item", item_id, {{"path":"/","http_only":true}})]}})
}})
http.post(app, "/items/form", (request) => {{
    state := crud_load_state()
    item_id := state.next_id.to_string()
    item := {{"id":item_id,"title":request.form.title,"attachment":false}}
    items := state.items
    items[item_id] = item
    state["items"] = items
    state["next_id"] = state.next_id + 1
    crud_save_state(state)
    return http.json(item, {{"status":201}})
}})
http.get(app, "/items/:id", (request) => {{
    state := crud_load_state()
    item_id := request.params.id
    if(!state.items.has(item_id)) {{ return crud_not_found() }}
    return http.json(state.items[item_id])
}})
http.patch(app, "/items/:id", (request) => {{
    state := crud_load_state()
    item_id := request.params.id
    if(!state.items.has(item_id)) {{ return crud_not_found() }}
    items := state.items
    item := items[item_id]
    item["title"] = request.json.title
    items[item_id] = item
    state["items"] = items
    crud_save_state(state)
    return http.json(item)
}})
http.delete(app, "/items/:id", (request) => {{
    state := crud_load_state()
    item_id := request.params.id
    if(!state.items.has(item_id)) {{ return crud_not_found() }}
    attachment_path := "attachments/" + item_id + ".bin"
    if(exists(attachment_path)) {{ remove(attachment_path) }}
    state["items"] = state.items.omit([item_id])
    crud_save_state(state)
    return http.json({{"deleted":item_id}})
}})
http.post(app, "/items/:id/attachment", (request) => {{
    state := crud_load_state()
    item_id := request.params.id
    if(!state.items.has(item_id)) {{ return crud_not_found() }}
    upload := request.files.attachment
    saved := http.save_upload(upload, "attachments/" + item_id + ".bin")
    items := state.items
    item := items[item_id]
    item["attachment"] = saved.ok
    item["attachment_type"] = upload.content_type
    items[item_id] = item
    state["items"] = items
    crud_save_state(state)
    return http.json({{"saved":saved.ok,"size":upload.size}})
}})
http.get(app, "/items/:id/attachment", (request) => {{
    state := crud_load_state()
    item_id := request.params.id
    if(!state.items.has(item_id) || !state.items[item_id].attachment) {{ return crud_not_found() }}
    return http.file_from("attachments", item_id + ".bin", {{"content_type":state.items[item_id].attachment_type,"download_name":"attachment.bin"}})
}})
http.get(app, "/worker-state", (request) => {{
    ephemeral_counter += 1
    return http.json({{"counter":ephemeral_counter}})
}})
http.listen(app)
'''


port = free_port()
with open(os.path.join(WORK, "app.f"), "w", encoding="utf-8") as output:
    output.write(app_source(port, 15))
env = {**os.environ, "NIFT_HTTP_NIFT": NIFT}
server = subprocess.Popen([NIFT, "app.f"], cwd=WORK, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def request(method, path, body=None, headers=None, active_port=None):
    selected_port = active_port or port
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", selected_port, timeout=5)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = response.status, response.getheaders(), response.read()
            connection.close()
            return result
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def json_request(method, path, value):
    return request(method, path, json.dumps(value).encode(), {"Content-Type": "application/json"})


try:
    if json.loads(request("GET", "/items")[2]) != {}:
        raise AssertionError("initial collection is not empty")
    created = json_request("POST", "/items", {"title": "First"})
    if created[0] != 201 or json.loads(created[2])["id"] != "1":
        raise AssertionError(created)
    if not any(key.lower() == "set-cookie" and value.startswith("last_item=1") for key, value in created[1]):
        raise AssertionError("create cookie missing")
    if json.loads(request("GET", "/items/1")[2])["title"] != "First":
        raise AssertionError("GET item failed")
    patched = json_request("PATCH", "/items/1", {"title": "Updated"})
    if patched[0] != 200 or json.loads(patched[2])["title"] != "Updated":
        raise AssertionError(patched)
    formed = request(
        "POST", "/items/form", b"title=From+Form",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    if formed[0] != 201 or json.loads(formed[2]) != {"id": "2", "title": "From Form", "attachment": False}:
        raise AssertionError(formed)
    collection = json.loads(request("GET", "/items")[2])
    if sorted(collection) != ["1", "2"]:
        raise AssertionError(collection)
    attachment = b"\x00attachment-bytes\xff"
    boundary = b"crud-boundary"
    uploaded = request(
        "POST", "/items/1/attachment", multipart(boundary, "../client.bin", attachment),
        {"Content-Type": "multipart/form-data; boundary=crud-boundary"},
    )
    if uploaded[0] != 200 or json.loads(uploaded[2]) != {"saved": True, "size": len(attachment)}:
        raise AssertionError(uploaded)
    downloaded = request("GET", "/items/1/attachment")
    if downloaded[0] != 200 or downloaded[2] != attachment:
        raise AssertionError(downloaded)
    ranged = request("GET", "/items/1/attachment", headers={"Range": "bytes=1-4"})
    if ranged[0] != 206 or ranged[2] != attachment[1:5]:
        raise AssertionError(ranged)
    deleted = request("DELETE", "/items/1")
    if deleted[0] != 200 or json.loads(deleted[2]) != {"deleted": "1"}:
        raise AssertionError(deleted)
    if request("GET", "/items/1")[0] != 404 or request("GET", "/items/1/attachment")[0] != 404:
        raise AssertionError("deleted resources remain reachable")
    if json.loads(request("GET", "/worker-state")[2]) != {"counter": 1}:
        raise AssertionError("unexpected first worker state")
    if json.loads(request("GET", "/worker-state")[2]) != {"counter": 1}:
        raise AssertionError("top-level state persisted across workers")
    if json_request("POST", "/items", {})[0] != 500:
        raise AssertionError("application error was not contained")
finally:
    stdout, stderr = server.communicate(timeout=30)

if server.returncode != 0:
    raise SystemExit(f"FAIL CRUD server: {stdout!r} {stderr!r}")
if exists := os.path.exists(os.path.join(WORK, "attachments", "1.bin")):
    raise SystemExit(f"FAIL deleted attachment remains: {exists}")

# Restart proves persistence is external to process-local route/application state.
restart_port = free_port()
with open(os.path.join(WORK, "restart.f"), "w", encoding="utf-8") as output:
    output.write(app_source(restart_port, 1))
restart = subprocess.Popen([NIFT, "restart.f"], cwd=WORK, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
persisted = request("GET", "/items/2", active_port=restart_port)
restart_stdout, restart_stderr = restart.communicate(timeout=15)
if restart.returncode != 0 or persisted[0] != 200 or json.loads(persisted[2])["title"] != "From Form":
    raise SystemExit(f"FAIL persisted restart: {persisted!r} {restart_stdout!r} {restart_stderr!r}")

with open(os.path.join(WORK, "items.json"), encoding="utf-8") as source:
    state = json.load(source)
if state["next_id"] != 3 or sorted(state["items"]) != ["2"]:
    raise SystemExit(f"FAIL persisted state: {state!r}")

print("PASS http CP09 CRUD dogfood")
