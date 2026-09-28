#!/usr/bin/env python3
"""Strict, dependency-free HTTP/1.x helper for the Nift http package."""

import argparse
from email.message import Message
from email.utils import format_datetime, parsedate_to_datetime
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qsl


TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HEX = frozenset("0123456789abcdefABCDEF")
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
}
REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 206: "Partial Content",
    400: "Bad Request",
    404: "Not Found", 414: "URI Too Long", 415: "Unsupported Media Type",
    405: "Method Not Allowed", 408: "Request Timeout",
    413: "Payload Too Large", 431: "Request Header Fields Too Large",
    500: "Internal Server Error", 501: "Not Implemented",
    503: "Service Unavailable", 504: "Gateway Timeout",
    416: "Range Not Satisfiable",
}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def strict_unquote(value):
    output = bytearray()
    raw = value.encode("ascii")
    i = 0
    while i < len(raw):
        if raw[i] == 37:
            if i + 2 >= len(raw) or chr(raw[i + 1]) not in HEX or chr(raw[i + 2]) not in HEX:
                raise HttpError(400, "invalid percent escape")
            output.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            output.append(raw[i])
            i += 1
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HttpError(400, "request target is not UTF-8") from exc


def parse_content_type(values):
    if not values:
        return "", {}
    if len(values) != 1:
        raise HttpError(400, "duplicate content-type is not accepted")
    message = Message()
    message["content-type"] = values[0]
    media_type = message.get_content_type().lower()
    parameters = {}
    for key, value in message.get_params()[1:]:
        key = key.lower()
        if key in parameters or value is None:
            raise HttpError(400, "invalid content-type parameters")
        parameters[key] = value
    return media_type, parameters


def add_repeated(mapping, key, value):
    if key in mapping:
        if not isinstance(mapping[key], list):
            mapping[key] = [mapping[key]]
        mapping[key].append(value)
    else:
        mapping[key] = value


def parse_urlencoded(body, parameters, args):
    charset = parameters.get("charset", "utf-8").lower()
    if charset not in ("utf-8", "utf8"):
        raise HttpError(415, "URL-encoded forms require UTF-8")
    try:
        encoded = body.decode("ascii")
        strict_unquote(encoded.replace("+", " "))
        pairs = parse_qsl(
            encoded, keep_blank_values=True, strict_parsing=False,
            encoding="utf-8", errors="strict", max_num_fields=args.max_form_fields,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HttpError(400, "malformed URL-encoded form") from exc
    form = {}
    for key, value in pairs:
        if len(key.encode("utf-8")) > args.max_form_name_bytes:
            raise HttpError(400, "form field name is too large")
        if len(value.encode("utf-8")) > args.max_form_value_bytes:
            raise HttpError(400, "form field value is too large")
        add_repeated(form, key, value)
    return form


def parse_cookies(values, max_pairs):
    cookies = {}
    count = 0
    for header in values:
        if any(ord(char) < 32 or ord(char) == 127 for char in header):
            raise HttpError(400, "invalid Cookie header")
        for segment in header.split(";"):
            segment = segment.strip()
            if not segment or "=" not in segment:
                raise HttpError(400, "malformed Cookie header")
            name, value = segment.split("=", 1)
            name = name.strip()
            value = value.strip()
            try:
                encoded_name = name.encode("ascii")
                encoded_value = value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise HttpError(400, "Cookie header must be ASCII") from exc
            if not TOKEN.fullmatch(encoded_name) or any(
                byte < 0x21 or byte > 0x7E or byte in (0x22, 0x2C, 0x3B, 0x5C)
                for byte in encoded_value
            ):
                raise HttpError(400, "malformed Cookie header")
            count += 1
            if count > max_pairs:
                raise HttpError(400, "too many cookies")
            add_repeated(cookies, name, value)
    return cookies


def serialize_cookie(cookie):
    if not isinstance(cookie, dict):
        raise HttpError(500, "invalid response cookie")
    name = cookie.get("name")
    value = cookie.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        raise HttpError(500, "invalid response cookie")
    try:
        encoded_name = name.encode("ascii")
        encoded_value = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HttpError(500, "response cookies must be ASCII") from exc
    if not TOKEN.fullmatch(encoded_name) or any(
        byte < 0x21 or byte > 0x7E or byte in (0x22, 0x2C, 0x3B, 0x5C)
        for byte in encoded_value
    ):
        raise HttpError(500, "invalid response cookie")
    parts = [f"{name}={value}"]
    for key, label in (("path", "Path"), ("domain", "Domain")):
        attribute = cookie.get(key)
        if attribute is not None:
            if not isinstance(attribute, str) or not attribute or any(
                ord(char) < 0x20 or ord(char) > 0x7E or char == ";" for char in attribute
            ):
                raise HttpError(500, f"invalid cookie {label}")
            parts.append(f"{label}={attribute}")
    if cookie.get("max_age") is not None:
        max_age = cookie["max_age"]
        if not isinstance(max_age, int) or isinstance(max_age, bool):
            raise HttpError(500, "invalid cookie Max-Age")
        parts.append(f"Max-Age={max_age}")
    if cookie.get("expires") is not None:
        expires = cookie["expires"]
        if not isinstance(expires, str):
            raise HttpError(500, "invalid cookie Expires")
        try:
            parsed = parsedate_to_datetime(expires)
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            expires = format_datetime(parsed, usegmt=True)
        except (TypeError, ValueError) as exc:
            raise HttpError(500, "invalid cookie Expires") from exc
        parts.append(f"Expires={expires}")
    same_site = cookie.get("same_site")
    if same_site is not None:
        if not isinstance(same_site, str) or same_site.lower() not in ("strict", "lax", "none"):
            raise HttpError(500, "invalid cookie SameSite")
        parts.append(f"SameSite={same_site.title()}")
    secure = cookie.get("secure", False)
    http_only = cookie.get("http_only", False)
    if not isinstance(secure, bool) or not isinstance(http_only, bool):
        raise HttpError(500, "invalid cookie flag")
    if secure:
        parts.append("Secure")
    if http_only:
        parts.append("HttpOnly")
    if name.startswith("__Secure-") and not secure:
        raise HttpError(500, "__Secure- cookies require Secure")
    if name.startswith("__Host-") and (not secure or cookie.get("path") != "/" or cookie.get("domain") is not None):
        raise HttpError(500, "__Host- cookies require Secure, Path=/ and no Domain")
    return "; ".join(parts)


def parse_part_headers(block, args):
    if len(block) > args.max_part_header_bytes:
        raise HttpError(400, "multipart part headers are too large")
    lines = block.split(b"\r\n") if block else []
    if len(lines) > args.max_part_headers:
        raise HttpError(400, "too many multipart part headers")
    headers = {}
    for line in lines:
        if not line or line[:1] in (b" ", b"\t") or b":" not in line:
            raise HttpError(400, "malformed multipart part header")
        name, value = line.split(b":", 1)
        if not TOKEN.fullmatch(name) or any(byte < 32 and byte != 9 for byte in value) or 127 in value:
            raise HttpError(400, "invalid multipart part header")
        key = name.decode("ascii").lower()
        if key in headers:
            raise HttpError(400, "duplicate multipart part header")
        headers[key] = value.strip(b" \t").decode("latin-1")
    if "content-transfer-encoding" in headers:
        raise HttpError(400, "multipart transfer encoding is not supported")
    return headers


def next_multipart_boundary(body, marker, start):
    position = start
    while True:
        position = body.find(marker, position)
        if position == -1:
            return -1
        suffix = position + len(marker)
        if body[suffix:suffix + 2] in (b"\r\n", b"--"):
            return position
        position += len(marker)


def parse_disposition(value):
    message = Message()
    message["content-disposition"] = value
    if message.get_content_disposition() != "form-data":
        raise HttpError(400, "multipart part requires form-data disposition")
    parameters = {}
    for key, parameter in message.get_params(header="content-disposition")[1:]:
        key = key.lower()
        if key in parameters or parameter is None:
            raise HttpError(400, "invalid multipart disposition parameters")
        parameters[key] = parameter
    field_name = parameters.get("name")
    if not isinstance(field_name, str) or not field_name or any(ord(char) < 32 for char in field_name):
        raise HttpError(400, "multipart part requires a valid name")
    return field_name, parameters.get("filename"), "filename" in parameters


def parse_multipart(body, parameters, request_dir, args):
    boundary = parameters.get("boundary")
    if not isinstance(boundary, str):
        raise HttpError(400, "multipart boundary is required")
    try:
        encoded_boundary = boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HttpError(400, "invalid multipart boundary") from exc
    if not re.fullmatch(rb"[0-9A-Za-z'()+_,./:=?-]{1,70}", encoded_boundary):
        raise HttpError(400, "invalid multipart boundary")
    delimiter = b"--" + encoded_boundary
    marker = b"\r\n" + delimiter
    if not body.startswith(delimiter):
        raise HttpError(400, "multipart body does not start with its boundary")
    cursor = len(delimiter)
    form = {}
    uploads = []
    part_count = 0
    file_count = 0
    spooled_bytes = len(body)
    while True:
        if body[cursor:cursor + 2] == b"--":
            trailer = body[cursor + 2:]
            if trailer not in (b"", b"\r\n"):
                raise HttpError(400, "invalid multipart closing boundary")
            break
        if body[cursor:cursor + 2] != b"\r\n":
            raise HttpError(400, "malformed multipart boundary")
        header_start = cursor + 2
        header_end = body.find(b"\r\n\r\n", header_start)
        if header_end == -1:
            raise HttpError(400, "multipart part headers are incomplete")
        headers = parse_part_headers(body[header_start:header_end], args)
        content_start = header_end + 4
        boundary_position = next_multipart_boundary(body, marker, content_start)
        if boundary_position == -1:
            raise HttpError(400, "multipart closing boundary is missing")
        content = body[content_start:boundary_position]
        cursor = boundary_position + len(marker)
        part_count += 1
        if part_count > args.max_multipart_parts:
            raise HttpError(413, "too many multipart parts")
        if "content-disposition" not in headers:
            raise HttpError(400, "multipart part has no content disposition")
        if headers.get("content-type", "").lower().startswith("multipart/"):
            raise HttpError(400, "nested multipart is not supported")
        field_name, filename, has_filename = parse_disposition(headers["content-disposition"])
        if len(field_name.encode("utf-8")) > args.max_form_name_bytes:
            raise HttpError(400, "multipart field name is too large")
        if has_filename:
            file_count += 1
            if file_count > args.max_multipart_files:
                raise HttpError(413, "too many multipart files")
            if not isinstance(filename, str) or any(char in filename for char in ("\x00", "\r", "\n")):
                raise HttpError(400, "invalid multipart filename")
            if len(filename.encode("utf-8")) > args.max_filename_bytes:
                raise HttpError(400, "multipart filename is too large")
            if len(content) > args.max_file_bytes:
                raise HttpError(413, "multipart file is too large")
            spooled_bytes += len(content)
            if spooled_bytes > args.max_temp_bytes:
                raise HttpError(413, "request temporary storage limit exceeded")
            upload_id = str(file_count)
            upload_path = os.path.join(request_dir, f"upload-{upload_id}.bin")
            with open(upload_path, "wb") as output:
                output.write(content)
            uploads.append({
                "id": upload_id,
                "field": field_name,
                "filename": filename,
                "content_type": headers.get("content-type", "application/octet-stream"),
                "size": len(content),
                "path": upload_path,
            })
        else:
            if len(content) > args.max_form_value_bytes:
                raise HttpError(413, "multipart field is too large")
            try:
                value = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HttpError(400, "multipart text field is not UTF-8") from exc
            add_repeated(form, field_name, value)
    return form, uploads


def set_remaining_timeout(conn, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout()
    conn.settimeout(remaining)


def recv_headers(conn, limit, deadline):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        set_remaining_timeout(conn, deadline)
        chunk = conn.recv(4096)
        if not chunk:
            raise HttpError(400, "incomplete request headers")
        data.extend(chunk)
        if b"\r\n\r\n" not in data and len(data) > limit:
            raise HttpError(431, "request headers are too large")
    marker = data.index(b"\r\n\r\n")
    if marker > limit:
        raise HttpError(431, "request headers are too large")
    return bytes(data[:marker]), bytes(data[marker + 4:])


def read_request(conn, address, args, request_id, request_dir, request_state):
    deadline = time.monotonic() + args.client_timeout_ms / 1000.0
    header_block, remainder = recv_headers(conn, args.max_header_bytes, deadline)
    lines = header_block.split(b"\r\n")
    if not lines or len(lines[0]) > args.max_request_line:
        raise HttpError(400 if not lines else 414, "invalid request line")
    try:
        request_line = lines[0].decode("ascii")
        method, target, version = request_line.split(" ")
    except (UnicodeDecodeError, ValueError) as exc:
        raise HttpError(400, "malformed request line") from exc
    if not TOKEN.fullmatch(method.encode("ascii")) or version != "HTTP/1.1":
        raise HttpError(400, "unsupported request syntax")
    request_state["method"] = method
    if not target.startswith("/") or "#" in target:
        raise HttpError(400, "only origin-form request targets are supported")
    if len(lines) - 1 > args.max_headers:
        raise HttpError(431, "too many request headers")

    headers = {}
    content_lengths = []
    for line in lines[1:]:
        if not line or line[:1] in (b" ", b"\t") or b":" not in line:
            raise HttpError(400, "malformed request header")
        name, value = line.split(b":", 1)
        if not TOKEN.fullmatch(name) or any(byte < 32 and byte != 9 for byte in value) or 127 in value:
            raise HttpError(400, "invalid request header")
        try:
            key = name.decode("ascii").lower()
            text = value.strip(b" \t").decode("latin-1")
        except UnicodeDecodeError as exc:
            raise HttpError(400, "invalid request header encoding") from exc
        headers.setdefault(key, []).append(text)
        if key == "content-length":
            content_lengths.append(text)

    if "transfer-encoding" in headers:
        raise HttpError(501, "transfer encoding is not supported")
    if version == "HTTP/1.1" and (len(headers.get("host", [])) != 1 or not headers["host"][0]):
        raise HttpError(400, "HTTP/1.1 requires exactly one Host header")
    if len(content_lengths) > 1:
        raise HttpError(400, "duplicate content-length is not accepted")
    length = 0
    if content_lengths:
        if not re.fullmatch(r"[0-9]+", content_lengths[0]):
            raise HttpError(400, "invalid content-length")
        if len(content_lengths[0]) > 20:
            raise HttpError(413, "request body is too large")
        try:
            length = int(content_lengths[0])
        except ValueError as exc:
            raise HttpError(400, "invalid content-length") from exc
    if length > args.max_body_bytes:
        raise HttpError(413, "request body is too large")
    body = bytearray(remainder[:length])
    while len(body) < length:
        set_remaining_timeout(conn, deadline)
        chunk = conn.recv(min(65536, length - len(body)))
        if not chunk:
            raise HttpError(400, "incomplete request body")
        body.extend(chunk)

    raw_path, separator, raw_query = target.partition("?")
    try:
        path = strict_unquote(raw_path)
        if separator:
            # Validate escapes before parse_qsl applies form-query decoding.
            strict_unquote(raw_query.replace("+", " "))
        query_pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=False,
                                encoding="utf-8", errors="strict") if separator else []
    except (UnicodeDecodeError, ValueError) as exc:
        raise HttpError(400, "invalid query encoding") from exc
    query = {}
    for key, value in query_pairs:
        add_repeated(query, key, value)

    body_path = os.path.join(request_dir, "request.body")
    with open(body_path, "wb") as output:
        output.write(body)
    if len(body) > args.max_temp_bytes:
        raise HttpError(413, "request temporary storage limit exceeded")

    content_type, content_parameters = parse_content_type(headers.get("content-type", []))
    form = {}
    uploads = []
    json_value = None
    private_body_path = None
    if content_type == "multipart/form-data":
        form, uploads = parse_multipart(bytes(body), content_parameters, request_dir, args)
        body_value = {"kind": "multipart", "size": len(body)}
    elif content_type == "application/x-www-form-urlencoded":
        form = parse_urlencoded(body, content_parameters, args)
        try:
            body_text = body.decode("ascii")
        except UnicodeDecodeError as exc:
            raise HttpError(400, "malformed URL-encoded form") from exc
        body_value = {"kind": "text", "text": body_text}
    else:
        try:
            body_text = body.decode("utf-8")
            body_value = {"kind": "text", "text": body_text}
        except UnicodeDecodeError:
            body_text = None
            body_value = {"kind": "spooled", "size": len(body)}
            private_body_path = body_path
    if content_type == "application/json" or content_type.endswith("+json"):
        charset = content_parameters.get("charset", "utf-8").lower()
        if charset not in ("utf-8", "utf8"):
            raise HttpError(415, "JSON bodies require UTF-8")
        if body_text is None:
            raise HttpError(400, "JSON body is not UTF-8")
        try:
            json_value = json.loads(
                body_text,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise HttpError(400, "malformed JSON body") from exc

    request = {
        "protocol": 1,
        "request_id": str(request_id),
        "method": method,
        "target": target,
        "path": path,
        "segments": [strict_unquote(part) for part in raw_path.split("/")[1:] if part != ""],
        "query": query,
        "headers": headers,
        "body": body_value,
        "json": json_value,
        "form": form,
        "cookies": parse_cookies(headers.get("cookie", []), args.max_cookie_pairs),
        "remote_addr": address[0],
    }
    if private_body_path is not None:
        request["_body_path"] = private_body_path
    if uploads:
        request["_uploads"] = uploads
    request_state["path"] = path
    request_path = os.path.join(request_dir, "request.json")
    with open(request_path, "w", encoding="utf-8") as output:
        json.dump(request, output, ensure_ascii=False, separators=(",", ":"))
    return request, request_path


def terminate_process(process):
    if process is None:
        return
    if os.name == "posix":
        group_signaled = False
        try:
            os.killpg(process.pid, signal.SIGTERM)
            group_signaled = True
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        if group_signaled:
            # The group may outlive its leader if application code detached a child.
            time.sleep(0.02)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    elif process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def start_diagnostic_drain(process, name):
    diagnostic = bytearray()

    def drain_output():
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    return
                if len(diagnostic) < 8192:
                    diagnostic.extend(chunk[:8192 - len(diagnostic)])
        except (OSError, ValueError):
            pass

    drain = threading.Thread(target=drain_output, name=name)
    try:
        drain.start()
    except Exception as exc:
        terminate_process(process)
        process.stdout.close()
        raise OSError(f"cannot start worker diagnostic drain: {exc}") from exc
    return diagnostic, drain


def load_worker_response(response_path, request):
    try:
        with open(response_path, encoding="utf-8") as source:
            response = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise HttpError(500, "application worker produced no valid response") from exc
    if not isinstance(response, dict) or response.get("protocol") != 1:
        raise HttpError(500, "invalid application response protocol")
    if response.get("request_id") != request["request_id"]:
        raise HttpError(500, "application response request ID mismatch")
    return response


def run_oneshot_worker(args, request, request_path, request_dir, state, request_id):
    response_path = os.path.join(request_dir, "response.json")
    environment = dict(os.environ)
    environment.update({
        "NIFT_HTTP_WORKER": "1",
        "NIFT_HTTP_REQUEST": request_path,
        "NIFT_HTTP_RESPONSE": response_path,
    })
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            [args.nift, args.app], cwd=args.cwd, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix", creationflags=creationflags,
        )
    except OSError as exc:
        raise HttpError(500, f"worker launch failed: {exc}") from exc
    state.worker_started()
    try:
        diagnostic, drain = start_diagnostic_drain(process, f"http-worker-log-{request_id}")
    except OSError:
        state.worker_finished()
        raise
    if not state.register_worker(request_id, process, request_id):
        terminate_process(process)
        drain.join(timeout=1)
        process.stdout.close()
        state.worker_finished()
        raise HttpError(500, "server is shutting down")
    try:
        process.wait(timeout=args.worker_timeout_ms / 1000.0)
    except subprocess.TimeoutExpired as exc:
        terminate_process(process)
        raise HttpError(504, "application worker timed out") from exc
    finally:
        terminate_process(process)
        state.unregister_worker(request_id, process)
        drain.join(timeout=1)
        process.stdout.close()
        state.worker_finished()
    if process.returncode != 0:
        diagnostic_text = diagnostic.decode("utf-8", "replace").strip()
        if diagnostic_text:
            print(f"http helper: worker failed: {diagnostic_text}", file=sys.stderr)
        raise HttpError(500, "application worker failed")
    return load_worker_response(response_path, request), str(request_id)


class PersistentWorker:
    def __init__(self, worker_id, process, diagnostic, drain):
        self.worker_id = worker_id
        self.process = process
        self.diagnostic = diagnostic
        self.drain = drain
        self.requests = 0
        self.finished = False
        self.counted = False
        self.finish_lock = threading.Lock()


class PersistentWorkerStartupTimeout(OSError):
    pass


class PersistentWorkerPool:
    def __init__(self, args, state):
        self.args = args
        self.state = state
        self.condition = threading.Condition()
        self.workers = {}
        self.available = []
        self.next_worker_id = 1
        self.stopping = False
        self.replacing = 0
        self.maintenance_threads = set()

    def start(self):
        try:
            for _index in range(self.args.worker_pool_size):
                worker = self.launch_worker(cancel_on_drain=True)
                with self.condition:
                    if self.stopping:
                        self.finish_worker(worker)
                        raise OSError("persistent pool startup cancelled")
                    self.workers[worker.worker_id] = worker
                    self.available.append(worker)
        except Exception:
            self.shutdown()
            raise

    def launch_worker(self, deadline=None, cancel_on_drain=False):
        with self.condition:
            worker_id = self.next_worker_id
            self.next_worker_id += 1
        environment = dict(os.environ)
        environment["NIFT_HTTP_WORKER"] = "persistent"
        environment.pop("NIFT_HTTP_REQUEST", None)
        environment.pop("NIFT_HTTP_RESPONSE", None)
        ready_path = os.path.join(self.args.temp_root, f"persistent-{worker_id}.ready")
        environment["NIFT_HTTP_READY"] = ready_path
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            [self.args.nift, self.args.app], cwd=self.args.cwd, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix", creationflags=creationflags,
        )
        diagnostic, drain = start_diagnostic_drain(
            process, f"http-persistent-log-{worker_id}",
        )
        worker = PersistentWorker(worker_id, process, diagnostic, drain)
        if deadline is None:
            deadline = time.monotonic() + self.args.worker_timeout_ms / 1000.0
        while not os.path.exists(ready_path):
            if self.stopping or (cancel_on_drain and self.state.shutdown_requested()):
                self.finish_worker(worker)
                raise OSError("persistent worker startup cancelled")
            if os.getppid() != self.state.parent_pid:
                self.state.force_requested.set()
                self.finish_worker(worker)
                raise OSError("server parent exited during worker startup")
            if process.poll() is not None:
                diagnostic_text = diagnostic.decode("utf-8", "replace").strip()
                self.finish_worker(worker)
                raise OSError(f"persistent worker exited during startup: {diagnostic_text}")
            if time.monotonic() >= deadline:
                self.finish_worker(worker)
                raise PersistentWorkerStartupTimeout("persistent worker startup timed out")
            time.sleep(0.002)
        try:
            os.remove(ready_path)
        except FileNotFoundError:
            pass
        except OSError:
            self.finish_worker(worker)
            raise
        self.state.worker_started()
        worker.counted = True
        return worker

    def finish_worker(self, worker, graceful=False):
        with worker.finish_lock:
            if worker.finished:
                return
            worker.finished = True
        if graceful and worker.process.stdin is not None:
            try:
                worker.process.stdin.close()
                worker.process.wait(timeout=0.2)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
        terminate_process(worker.process)
        if worker.process.stdin is not None and not worker.process.stdin.closed:
            try:
                worker.process.stdin.close()
            except OSError:
                pass
        worker.drain.join(timeout=1)
        if worker.process.stdout is not None:
            worker.process.stdout.close()
        if worker.counted:
            worker.counted = False
            self.state.worker_finished()

    def acquire(self, state, deadline):
        while True:
            dead = None
            launch = False
            with self.condition:
                if self.stopping or state.aborting.is_set():
                    raise HttpError(500, "server is shutting down")
                if time.monotonic() >= deadline:
                    raise HttpError(504, "application worker timed out")
                for candidate in self.available:
                    if candidate.process.poll() is not None:
                        self.available.remove(candidate)
                        self.workers.pop(candidate.worker_id, None)
                        dead = candidate
                        break
                if dead is None and len(self.workers) + self.replacing < self.args.worker_pool_size:
                    self.replacing += 1
                    launch = True
                elif dead is None and self.available:
                    return self.available.pop(0)
                if dead is None:
                    if not launch:
                        self.condition.wait(timeout=max(0, min(0.05, deadline - time.monotonic())))
                        continue
            if dead is not None:
                self.state.increment("worker_restarts")
                self.finish_worker(dead)
                continue
            try:
                worker = self.launch_worker(deadline)
            except PersistentWorkerStartupTimeout as exc:
                with self.condition:
                    self.replacing -= 1
                    self.condition.notify_all()
                raise HttpError(504, "application worker timed out") from exc
            except OSError as exc:
                with self.condition:
                    self.replacing -= 1
                    self.condition.notify_all()
                raise HttpError(500, f"persistent worker launch failed: {exc}") from exc
            with self.condition:
                self.replacing -= 1
                if self.stopping or state.aborting.is_set():
                    close_worker = True
                else:
                    self.workers[worker.worker_id] = worker
                    close_worker = False
            if close_worker:
                self.finish_worker(worker)
                raise HttpError(500, "server is shutting down")
            return worker

    def release(self, worker, replace=False, reason=None):
        if not replace and worker.process.poll() is None:
            with self.condition:
                if not self.stopping:
                    self.available.append(worker)
                    self.condition.notify()
                    return
                replace = True
        with self.condition:
            self.workers.pop(worker.worker_id, None)
            stopping = self.stopping or self.state.aborting.is_set() or reason == "shutdown"
            if not stopping:
                self.replacing += 1
            self.condition.notify_all()
        if stopping:
            self.finish_worker(worker, graceful=not replace)
            return
        if reason == "recycle":
            self.state.increment("worker_recycles")
        else:
            self.state.increment("worker_restarts")
        maintenance = threading.Thread(
            target=self.replace_worker,
            args=(worker, replace),
            name=f"http-worker-replace-{worker.worker_id}",
        )
        with self.condition:
            self.maintenance_threads.add(maintenance)
        try:
            maintenance.start()
        except Exception as exc:
            with self.condition:
                self.maintenance_threads.discard(maintenance)
                self.replacing -= 1
                self.condition.notify_all()
            self.finish_worker(worker, graceful=not replace)
            print(f"http helper: cannot start worker replacement: {exc}", file=sys.stderr)

    def replace_worker(self, old_worker, force):
        replacement = None
        try:
            self.finish_worker(old_worker, graceful=not force)
            if not self.stopping:
                replacement = self.launch_worker()
            with self.condition:
                self.replacing -= 1
                if replacement is not None and not self.stopping:
                    self.workers[replacement.worker_id] = replacement
                    self.available.append(replacement)
                    replacement = None
                self.condition.notify_all()
        except OSError as exc:
            if not self.stopping:
                print(f"http helper: persistent worker replacement failed: {exc}", file=sys.stderr)
            with self.condition:
                self.replacing -= 1
                self.condition.notify_all()
        finally:
            if replacement is not None:
                self.finish_worker(replacement)
            with self.condition:
                self.maintenance_threads.discard(threading.current_thread())
                self.condition.notify_all()

    def shutdown(self):
        with self.condition:
            if self.stopping and not self.workers and not self.maintenance_threads:
                return
            self.stopping = True
            self.available.clear()
            maintenance = list(self.maintenance_threads)
            self.condition.notify_all()
        for thread in maintenance:
            thread.join(timeout=self.args.worker_timeout_ms / 1000.0 + 1)
        with self.condition:
            workers = list(self.workers.values())
        for worker in workers:
            if worker.process.stdin is not None:
                try:
                    worker.process.stdin.close()
                except OSError:
                    pass
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and any(
            worker.process.poll() is None for worker in workers
        ):
            time.sleep(0.01)
        for worker in workers:
            try:
                if os.name == "posix":
                    os.killpg(worker.process.pid, signal.SIGTERM)
                elif worker.process.poll() is None:
                    worker.process.terminate()
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and any(
            worker.process.poll() is None for worker in workers
        ):
            time.sleep(0.01)
        for worker in workers:
            try:
                if os.name == "posix":
                    os.killpg(worker.process.pid, signal.SIGKILL)
                elif worker.process.poll() is None:
                    worker.process.kill()
            except ProcessLookupError:
                pass
            self.finish_worker(worker)
        with self.condition:
            self.workers.clear()

    def begin_shutdown(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()


def run_persistent_worker(args, request, request_dir, state, request_id):
    response_path = os.path.join(request_dir, "response.json")
    deadline = time.monotonic() + args.worker_timeout_ms / 1000.0
    worker = state.pool.acquire(state, deadline)
    replace = False
    replacement_reason = None
    if not state.register_worker(request_id, worker.process, worker.worker_id):
        state.pool.release(worker, replace=True, reason="shutdown")
        raise HttpError(500, "server is shutting down")
    try:
        try:
            worker.process.stdin.write((request_dir + "\n").encode("utf-8"))
            worker.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            replace = True
            replacement_reason = "failure"
            raise HttpError(500, "persistent worker control channel failed") from exc
        worker.requests += 1
        while not os.path.exists(response_path):
            if worker.process.poll() is not None:
                replace = True
                replacement_reason = "failure"
                diagnostic = worker.diagnostic.decode("utf-8", "replace").strip()
                print(
                    f"http helper: persistent worker exited {worker.process.returncode}"
                    + (f": {diagnostic}" if diagnostic else ""),
                    file=sys.stderr,
                )
                raise HttpError(500, "persistent application worker failed")
            if state.aborting.is_set():
                replace = True
                replacement_reason = "failure"
                raise HttpError(500, "server is shutting down")
            if time.monotonic() >= deadline:
                replace = True
                replacement_reason = "failure"
                terminate_process(worker.process)
                raise HttpError(504, "application worker timed out")
            time.sleep(0.002)
        try:
            response = load_worker_response(response_path, request)
        except HttpError:
            replace = True
            replacement_reason = "failure"
            raise
        if worker.requests >= args.worker_max_requests:
            replace = True
            replacement_reason = "recycle"
        return response, str(worker.worker_id)
    finally:
        state.unregister_worker(request_id, worker.process)
        state.pool.release(worker, replace=replace, reason=replacement_reason)


def run_worker(args, request, request_path, request_dir, state, request_id):
    if args.worker_mode == "persistent":
        return run_persistent_worker(args, request, request_dir, state, request_id)
    return run_oneshot_worker(args, request, request_path, request_dir, state, request_id)


def open_root_file(root, relative, cwd):
    if not isinstance(root, str) or not isinstance(relative, str) or not root or not relative:
        raise HttpError(500, "invalid rooted file response")
    if os.path.isabs(relative) or "\\" in relative or "\x00" in relative:
        raise HttpError(404, "file not found")
    components = relative.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise HttpError(404, "file not found")
    root_path = root if os.path.isabs(root) else os.path.join(cwd, root)
    if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors = []
        final_fd = None
        try:
            current = os.open(root_path, directory_flags)
            descriptors.append(current)
            for component in components[:-1]:
                current = os.open(component, directory_flags, dir_fd=current)
                descriptors.append(current)
            final_fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
            metadata = os.fstat(final_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise HttpError(404, "file not found")
            source = os.fdopen(final_fd, "rb")
            final_fd = None
            return source, metadata.st_size
        except (OSError, ValueError) as exc:
            raise HttpError(404, "file not found") from exc
        finally:
            if final_fd is not None:
                os.close(final_fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    root_real = os.path.realpath(root_path)
    candidate = os.path.realpath(os.path.join(root_real, *components))
    source = None
    try:
        if os.path.commonpath((root_real, candidate)) != root_real:
            raise HttpError(404, "file not found")
        source = open(candidate, "rb")
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise HttpError(404, "file not found")
        return source, metadata.st_size
    except HttpError:
        if source is not None:
            source.close()
        raise
    except (OSError, ValueError) as exc:
        if source is not None:
            source.close()
        raise HttpError(404, "file not found") from exc


def open_response_file(body, args):
    kind = body.get("kind")
    if kind == "file":
        path = body.get("path")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise HttpError(500, "invalid file response path")
        resolved = path if os.path.isabs(path) else os.path.join(args.cwd, path)
        source = None
        try:
            source = open(resolved, "rb")
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise HttpError(404, "file not found")
            size = metadata.st_size
        except HttpError:
            if source is not None:
                source.close()
            raise
        except OSError as exc:
            if source is not None:
                source.close()
            raise HttpError(404, "file not found") from exc
    elif kind == "root_file":
        source, size = open_root_file(body.get("root"), body.get("path"), args.cwd)
    else:
        raise HttpError(500, "unsupported file response body kind")
    if size > args.max_file_response_bytes:
        source.close()
        raise HttpError(500, "file response exceeds configured limit")
    return source, size


def parse_range(value, size):
    if not isinstance(value, str) or len(value) > 256 or not value.startswith("bytes="):
        return None
    specification = value[6:]
    if "," in specification or specification.count("-") != 1:
        return None
    start_text, end_text = specification.split("-", 1)
    if len(start_text) > 20 or len(end_text) > 20:
        return None
    if start_text:
        if not start_text.isascii() or not start_text.isdigit():
            return None
        start = int(start_text)
        if end_text:
            if not end_text.isascii() or not end_text.isdigit():
                return None
            end = int(end_text)
            if end < start:
                return None
        else:
            end = size - 1
        if start >= size:
            return None
        end = min(end, size - 1)
    else:
        if not end_text or not end_text.isascii() or not end_text.isdigit():
            return None
        suffix = int(end_text)
        if suffix <= 0 or size == 0:
            return None
        start = max(0, size - suffix)
        end = size - 1
    return start, end


def response_parts(response, request=None, args=None):
    status = response.get("status")
    if not isinstance(status, int) or isinstance(status, bool) or status < 200 or status > 599:
        raise HttpError(500, "invalid application response status")
    raw_headers = response.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise HttpError(500, "invalid application response headers")
    headers = []
    for name, values in raw_headers.items():
        if not isinstance(name, str):
            raise HttpError(500, "invalid application response header name")
        try:
            encoded_name = name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise HttpError(500, "invalid application response header name") from exc
        if not TOKEN.fullmatch(encoded_name):
            raise HttpError(500, "invalid application response header name")
        key = name.lower()
        if key in HOP_BY_HOP:
            raise HttpError(500, "application supplied a hop-by-hop response header")
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if not isinstance(value, str) or any(
                ord(char) == 127 or (ord(char) < 32 and char != "\t") or ord(char) > 255
                for char in value
            ):
                raise HttpError(500, "invalid application response header value")
            headers.append((key, value))
    raw_cookies = response.get("cookies", [])
    if not isinstance(raw_cookies, list):
        raise HttpError(500, "invalid response cookies")
    if raw_cookies and any(name == "set-cookie" for name, _value in headers):
        raise HttpError(500, "structured cookies cannot be combined with raw Set-Cookie")
    for cookie in raw_cookies:
        headers.append(("set-cookie", serialize_cookie(cookie)))
    body = response.get("body", {"kind": "empty"})
    if not isinstance(body, dict):
        raise HttpError(500, "invalid application response body")
    kind = body.get("kind", "empty")
    if kind == "empty":
        payload = b""
    elif kind == "text":
        if not isinstance(body.get("text"), str):
            raise HttpError(500, "invalid text response body")
        payload = body["text"].encode("utf-8")
    elif kind == "json":
        try:
            payload = json.dumps(
                body.get("value"), ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HttpError(500, "invalid JSON response body") from exc
    elif kind in ("file", "root_file"):
        if any(name in ("content-length", "content-range", "accept-ranges") for name, _value in headers):
            raise HttpError(500, "application supplied reserved file response headers")
        source, size = open_response_file(body, args)
        download_name = body.get("download_name")
        if download_name is not None:
            if not isinstance(download_name, str) or not download_name or any(
                ord(char) < 0x20 or ord(char) > 0x7E or char in ('"', "\\", "/")
                for char in download_name
            ):
                source.close()
                raise HttpError(500, "invalid download filename")
            headers.append(("content-disposition", f'attachment; filename="{download_name}"'))
        headers.append(("accept-ranges", "bytes"))
        offset = 0
        length = size
        range_values = request["headers"].get("range", [])
        if range_values and request["method"] in ("GET", "HEAD") and status == 200:
            selected = parse_range(range_values[0], size) if len(range_values) == 1 else None
            if selected is None:
                source.close()
                headers.append(("content-range", f"bytes */{size}"))
                return 416, headers, {"kind": "bytes", "data": b""}
            offset, end = selected
            length = end - offset + 1
            status = 206
            headers.append(("content-range", f"bytes {offset}-{end}/{size}"))
        return status, headers, {
            "kind": "file", "source": source, "offset": offset, "length": length,
        }
    else:
        raise HttpError(500, "unsupported application response body kind")
    return status, headers, {"kind": "bytes", "data": payload}


def send_response(conn, status, body, headers=(), method="GET"):
    reason = REASONS.get(status, "Response")
    if isinstance(body, str):
        body = {"kind": "bytes", "data": body.encode("utf-8")}
    elif isinstance(body, bytes):
        body = {"kind": "bytes", "data": body}
    if not isinstance(body, dict) or body.get("kind") not in ("bytes", "file"):
        raise HttpError(500, "invalid response body plan")
    length = len(body["data"]) if body["kind"] == "bytes" else body["length"]
    lines = [f"HTTP/1.1 {status} {reason}\r\n"]
    seen_type = False
    for name, value in headers:
        if name.lower() == "content-type":
            seen_type = True
        lines.append(f"{name}: {value}\r\n")
    if not seen_type:
        lines.append("Content-Type: text/plain; charset=utf-8\r\n")
    no_body_status = status in (204, 304)
    if not no_body_status:
        lines.append(f"Content-Length: {length}\r\n")
    lines.append("Connection: close\r\n\r\n")
    conn.sendall("".join(lines).encode("latin-1"))
    if method == "HEAD" or no_body_status:
        return
    if body["kind"] == "bytes":
        conn.sendall(body["data"])
        return
    source = body["source"]
    source.seek(body["offset"])
    remaining = body["length"]
    if remaining == 0:
        return
    try:
        conn.sendfile(source, offset=body["offset"], count=remaining)
    except (AttributeError, NotImplementedError):
        source.seek(body["offset"])
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                raise OSError("file response ended before its advertised length")
            conn.sendall(chunk)
            remaining -= len(chunk)


def safe_send_response(conn, status, body, headers=(), method="GET"):
    try:
        send_response(conn, status, body, headers, method)
    except (BrokenPipeError, ConnectionResetError, socket.timeout, UnicodeEncodeError, OSError):
        pass
    finally:
        if isinstance(body, dict) and body.get("kind") == "file":
            body["source"].close()


class ServerState:
    def __init__(self, parent_pid):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.draining = threading.Event()
        self.aborting = threading.Event()
        self.force_requested = threading.Event()
        self.signal_count = 0
        self.parent_pid = parent_pid
        self.server = None
        self.connections = {}
        self.workers = {}
        self.request_worker_ids = {}
        self.threads = {}
        self.pool = None
        self.phase = "starting"
        self.admission_waiting = 0
        self.worker_processes = 0
        self.metrics = {
            "accepted": 0, "admitted": 0, "rejected": 0, "completed": 0,
            "errors": 0, "worker_starts": 0, "worker_restarts": 0,
            "worker_recycles": 0,
        }
        self.status_path = None
        self.event_log_path = None
        self.max_event_log_bytes = 1048576
        self.admission_timeout_ms = 20
        self.mode = "oneshot"
        self.max_concurrency = 1

    def configure(self, args):
        self.mode = args.worker_mode
        self.max_concurrency = args.max_concurrency
        self.status_path = self.resolve_output_path(args.status_path, args.cwd)
        self.event_log_path = self.resolve_output_path(args.event_log_path, args.cwd)
        self.max_event_log_bytes = args.max_event_log_bytes
        self.admission_timeout_ms = args.admission_timeout_ms
        if self.paths_alias(self.status_path, self.event_log_path):
            raise OSError("status and event log paths must not alias")
        if self.paths_alias(
            self.status_path, self.status_path + ".tmp" if self.status_path else None,
        ):
            raise OSError("status path conflicts with its temporary path")
        if self.paths_alias(self.status_path + ".tmp" if self.status_path else None, self.event_log_path):
            raise OSError("event log conflicts with status temporary path")
        if self.event_log_path:
            try:
                mode = "w" if os.path.getsize(self.event_log_path) > self.max_event_log_bytes else "a"
            except OSError:
                mode = "a"
            with open(self.event_log_path, mode, encoding="utf-8"):
                pass
        with self.lock:
            self.write_status_locked(strict=True)

    @staticmethod
    def resolve_output_path(path, cwd):
        if not path:
            return None
        resolved = path if os.path.isabs(path) else os.path.join(cwd, path)
        return os.path.realpath(os.path.abspath(resolved))

    @staticmethod
    def paths_alias(first, second):
        if not first or not second:
            return False
        if first == second:
            return True
        try:
            return os.path.samefile(first, second)
        except OSError:
            return False

    def shutdown_requested(self):
        return self.signal_count > 0 or self.force_requested.is_set()

    def forced_shutdown_requested(self):
        return self.signal_count > 1 or self.force_requested.is_set()

    def status_value_locked(self):
        return {
            "protocol": 1,
            "phase": self.phase,
            "ready": self.phase == "ready",
            "worker_mode": self.mode,
            "max_concurrency": self.max_concurrency,
            "active_requests": len(self.connections),
            "active_workers": len(self.workers),
            "worker_processes": self.worker_processes,
            "queue_depth": self.admission_waiting,
            "counters": dict(self.metrics),
        }

    def write_status_locked(self, strict=False):
        if not self.status_path:
            return
        temporary = self.status_path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as output:
                json.dump(self.status_value_locked(), output, separators=(",", ":"))
            os.replace(temporary, self.status_path)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            if strict:
                raise
            print(f"http helper: cannot write status: {exc}", file=sys.stderr)

    def set_phase(self, phase):
        with self.lock:
            self.phase = phase
            self.write_status_locked()

    def increment(self, name, amount=1):
        with self.lock:
            self.metrics[name] += amount
            self.write_status_locked()

    def worker_started(self):
        with self.lock:
            self.metrics["worker_starts"] += 1
            self.worker_processes += 1
            self.write_status_locked()

    def worker_finished(self):
        with self.lock:
            self.worker_processes -= 1
            self.write_status_locked()

    def record_event(self, event):
        if not self.event_log_path:
            return
        event = {"time": round(time.time(), 6), **event}
        encoded = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        if len(encoded.encode("utf-8")) > self.max_event_log_bytes:
            event = {
                key: value for key, value in event.items()
                if key in ("time", "event", "request_id", "worker_id", "status", "duration_ms")
            }
            event["truncated"] = True
            encoded = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        if len(encoded.encode("utf-8")) > self.max_event_log_bytes:
            return
        with self.lock:
            try:
                size = os.path.getsize(self.event_log_path)
            except OSError:
                size = 0
            mode = "w" if size + len(encoded.encode("utf-8")) > self.max_event_log_bytes else "a"
            try:
                with open(self.event_log_path, mode, encoding="utf-8") as output:
                    output.write(encoded)
            except OSError as exc:
                print(f"http helper: cannot write event log: {exc}", file=sys.stderr)

    def set_server(self, server):
        with self.lock:
            self.server = server

    def set_pool(self, pool):
        self.pool = pool

    def admit(self, request_id, conn):
        deadline = time.monotonic() + self.admission_timeout_ms / 1000.0
        with self.condition:
            waiting = False
            while not self.shutdown_requested() and len(self.connections) >= self.max_concurrency:
                if not waiting:
                    waiting = True
                    self.admission_waiting += 1
                    self.write_status_locked()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.admission_waiting -= 1
                    self.write_status_locked()
                    return False
                if os.getppid() != self.parent_pid:
                    self.force_requested.set()
                    break
                self.condition.wait(min(0.05, remaining))
            if waiting:
                self.admission_waiting -= 1
            if self.shutdown_requested() or self.draining.is_set():
                self.write_status_locked()
                return False
            self.connections[request_id] = conn
            self.metrics["admitted"] += 1
            self.write_status_locked()
            return True

    def register_thread(self, request_id, thread):
        with self.lock:
            self.threads[request_id] = thread

    def register_worker(self, request_id, process, worker_id):
        with self.lock:
            if self.aborting.is_set():
                return False
            self.workers[request_id] = process
            self.request_worker_ids[request_id] = str(worker_id)
            self.write_status_locked()
            return True

    def unregister_worker(self, request_id, process):
        with self.lock:
            if self.workers.get(request_id) is process:
                del self.workers[request_id]
                self.write_status_locked()

    def complete(self, request_id):
        with self.condition:
            self.connections.pop(request_id, None)
            self.threads.pop(request_id, None)
            self.request_worker_ids.pop(request_id, None)
            self.metrics["completed"] += 1
            self.write_status_locked()
            self.condition.notify_all()

    def request_worker_id(self, request_id):
        with self.lock:
            return self.request_worker_ids.get(request_id)

    def abort(self):
        self.aborting.set()
        self.set_phase("aborting")
        self.begin_drain()
        if self.pool is not None:
            self.pool.begin_shutdown()
        with self.condition:
            server = self.server
            connections = list(self.connections.values())
            self.condition.notify_all()
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def begin_drain(self):
        self.draining.set()
        if not self.aborting.is_set():
            self.set_phase("draining")
        with self.condition:
            server = self.server
            self.condition.notify_all()
        if server is not None:
            try:
                server.close()
            except OSError:
                pass

    def terminate_workers(self):
        with self.lock:
            processes = list(self.workers.values())
        if os.name == "posix":
            for process in processes:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and any(process.poll() is None for process in processes):
                time.sleep(0.01)
            for process in processes:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            for process in processes:
                if process.poll() is None:
                    process.terminate()

    def join_threads(self, timeout=None, interruptible=True):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self.lock:
                threads = list(self.threads.values())
            if not threads:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            if interruptible and (
                self.forced_shutdown_requested() or os.getppid() != self.parent_pid
            ):
                self.force_requested.set()
                return False
            for thread in threads:
                if interruptible and (
                    self.forced_shutdown_requested() or os.getppid() != self.parent_pid
                ):
                    self.force_requested.set()
                    return False
                remaining = 0.02 if deadline is None else max(0, min(0.02, deadline - time.monotonic()))
                thread.join(timeout=remaining)


def handle_connection(args, state, conn, address, request_id, request_dir):
    request_state = {"method": "GET", "path": ""}
    payload = None
    worker_id = None
    status = 499
    started = time.perf_counter()
    try:
        with conn:
            conn.settimeout(args.client_timeout_ms / 1000.0)
            try:
                request, request_path = read_request(
                    conn, address, args, request_id, request_dir, request_state,
                )
                response, worker_id = run_worker(
                    args, request, request_path, request_dir, state, request_id,
                )
                status, headers, payload = response_parts(response, request, args)
                conn.settimeout(args.response_timeout_ms / 1000.0)
                safe_send_response(conn, status, payload, headers, request_state["method"])
            except socket.timeout:
                status = 408
                safe_send_response(conn, 408, "request timeout", method=request_state["method"])
            except HttpError as exc:
                status = exc.status
                safe_send_response(conn, exc.status, exc.message, method=request_state["method"])
            except (BrokenPipeError, ConnectionResetError, UnicodeEncodeError, OSError):
                pass
            except Exception as exc:
                status = 500
                print(f"http helper: request failed: {exc}", file=sys.stderr)
                safe_send_response(conn, 500, "internal server error", method=request_state["method"])
    finally:
        if isinstance(payload, dict) and payload.get("kind") == "file":
            payload["source"].close()
        shutil.rmtree(request_dir, ignore_errors=True)
        if status >= 500:
            state.increment("errors")
        state.record_event({
            "event": "request",
            "request_id": str(request_id),
            "worker_id": worker_id or state.request_worker_id(request_id),
            "method": request_state["method"],
            "path": request_state["path"],
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        state.complete(request_id)


def reject_overload(state, conn, request_id):
    with conn:
        try:
            conn.settimeout(0.1)
            send_response(
                conn, 503, b"", (("retry-after", "1"),), method="HEAD",
            )
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass
    state.increment("rejected")
    state.record_event({
        "event": "overload", "request_id": str(request_id), "status": 503,
    })


def serve(args):
    parent_pid = os.getppid()
    state = ServerState(parent_pid)
    state.configure(args)

    def stop(_signum, _frame):
        state.signal_count += 1

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop)
    temp_root = tempfile.mkdtemp(prefix="nift-http-")
    server = None
    handled = 0
    try:
        if args.worker_mode == "persistent":
            if "\n" in temp_root or "\r" in temp_root:
                raise OSError("persistent worker temporary path contains a line break")
            args.temp_root = temp_root
            pool = PersistentWorkerPool(args, state)
            state.set_pool(pool)
            pool.start()
        if state.forced_shutdown_requested() or os.getppid() != parent_pid:
            state.abort()
            raise OSError("server startup cancelled")
        if state.shutdown_requested():
            state.begin_drain()
            raise OSError("server startup drained")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        state.set_server(server)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(args.backlog)
        server.settimeout(0.2)
        state.set_phase("ready")
        while not state.shutdown_requested() and not state.draining.is_set() and (
            args.max_requests == 0 or handled < args.max_requests
        ):
            if os.getppid() != parent_pid:
                state.force_requested.set()
                break
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if state.draining.is_set():
                    break
                raise
            handled += 1
            state.increment("accepted")
            request_id = handled
            if not state.admit(request_id, conn):
                reject_overload(state, conn, request_id)
                continue
            request_dir = os.path.join(temp_root, str(handled))
            try:
                os.mkdir(request_dir, 0o700)
                thread = threading.Thread(
                    target=handle_connection,
                    args=(args, state, conn, address, request_id, request_dir),
                    name=f"http-request-{request_id}",
                )
                state.register_thread(request_id, thread)
                thread.start()
            except Exception:
                shutil.rmtree(request_dir, ignore_errors=True)
                state.complete(request_id)
                conn.close()
                raise
        if state.forced_shutdown_requested() or os.getppid() != parent_pid:
            state.abort()
            state.terminate_workers()
        else:
            state.begin_drain()
            if not state.join_threads(args.shutdown_grace_ms / 1000.0):
                state.abort()
                state.terminate_workers()
        state.join_threads(interruptible=False)
        if state.pool is not None:
            state.pool.shutdown()
    finally:
        state.begin_drain()
        if not state.join_threads(0):
            state.abort()
            state.terminate_workers()
        state.join_threads(interruptible=False)
        if state.pool is not None:
            state.pool.shutdown()
        if server is not None:
            server.close()
        shutil.rmtree(temp_root, ignore_errors=True)
        state.set_phase("stopped")
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--nift", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--worker-timeout-ms", type=int, default=30000)
    parser.add_argument("--client-timeout-ms", type=int, default=10000)
    parser.add_argument("--response-timeout-ms", type=int, default=30000)
    parser.add_argument("--max-request-line", type=int, default=8192)
    parser.add_argument("--max-header-bytes", type=int, default=32768)
    parser.add_argument("--max-headers", type=int, default=100)
    parser.add_argument("--max-body-bytes", type=int, default=1048576)
    parser.add_argument("--max-form-fields", type=int, default=256)
    parser.add_argument("--max-form-name-bytes", type=int, default=256)
    parser.add_argument("--max-form-value-bytes", type=int, default=65536)
    parser.add_argument("--max-cookie-pairs", type=int, default=128)
    parser.add_argument("--max-multipart-parts", type=int, default=128)
    parser.add_argument("--max-multipart-files", type=int, default=32)
    parser.add_argument("--max-part-header-bytes", type=int, default=8192)
    parser.add_argument("--max-part-headers", type=int, default=32)
    parser.add_argument("--max-file-bytes", type=int, default=1048576)
    parser.add_argument("--max-filename-bytes", type=int, default=255)
    parser.add_argument("--max-temp-bytes", type=int, default=2097152)
    parser.add_argument("--max-file-response-bytes", type=int, default=67108864)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--worker-mode", choices=("oneshot", "persistent"), default="oneshot")
    parser.add_argument("--worker-pool-size", type=int, default=1)
    parser.add_argument("--worker-max-requests", type=int, default=1000)
    parser.add_argument("--shutdown-grace-ms", type=int, default=5000)
    parser.add_argument("--admission-timeout-ms", type=int, default=20)
    parser.add_argument("--status-path", default="")
    parser.add_argument("--event-log-path", default="")
    parser.add_argument("--max-event-log-bytes", type=int, default=1048576)
    parser.add_argument("--backlog", type=int, default=16)
    args = parser.parse_args(argv)
    if not (0 <= args.port <= 65535):
        parser.error("port must be between 0 and 65535")
    for name in ("max_requests", "worker_timeout_ms", "client_timeout_ms", "response_timeout_ms",
                  "max_request_line", "max_header_bytes", "max_headers",
                  "max_body_bytes", "max_form_fields", "max_form_name_bytes",
                  "max_form_value_bytes", "max_cookie_pairs", "max_multipart_parts",
                  "max_multipart_files", "max_part_header_bytes", "max_part_headers",
                  "max_file_bytes", "max_filename_bytes", "max_temp_bytes",
                  "max_file_response_bytes", "max_concurrency", "worker_pool_size",
                  "worker_max_requests", "shutdown_grace_ms", "admission_timeout_ms",
                  "backlog"):
        if getattr(args, name) < 0:
            parser.error(f"{name.replace('_', '-')} must not be negative")
    if args.max_concurrency == 0 or args.max_concurrency > 128:
        parser.error("max-concurrency must be between 1 and 128")
    if args.worker_pool_size == 0 or args.worker_pool_size > 128:
        parser.error("worker-pool-size must be between 1 and 128")
    if args.worker_mode == "persistent" and args.worker_pool_size != args.max_concurrency:
        parser.error("persistent worker-pool-size must equal max-concurrency")
    if args.worker_max_requests == 0:
        parser.error("worker-max-requests must be at least 1")
    if args.max_event_log_bytes < 4096:
        parser.error("max-event-log-bytes must be at least 4096")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(serve(parse_args(sys.argv[1:])))
    except OSError as exc:
        print(f"http helper: {exc}", file=sys.stderr)
        raise SystemExit(2)
