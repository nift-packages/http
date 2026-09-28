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


def run_worker(args, request, request_path, request_dir, state, request_id):
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
    diagnostic = bytearray()

    def drain_output():
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                return
            if len(diagnostic) < 8192:
                diagnostic.extend(chunk[:8192 - len(diagnostic)])

    drain = threading.Thread(target=drain_output, name=f"http-worker-log-{request_id}")
    drain.start()
    if not state.register_worker(request_id, process):
        terminate_process(process)
        drain.join(timeout=1)
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
    if process.returncode != 0:
        diagnostic_text = diagnostic.decode("utf-8", "replace").strip()
        if diagnostic_text:
            print(f"http helper: worker failed: {diagnostic_text}", file=sys.stderr)
        raise HttpError(500, "application worker failed")
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
    def __init__(self):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.aborting = threading.Event()
        self.server = None
        self.connections = {}
        self.workers = {}
        self.threads = {}

    def set_server(self, server):
        with self.lock:
            self.server = server

    def admit(self, request_id, conn):
        deadline = time.monotonic() + 0.02
        with self.condition:
            while not self.aborting.is_set() and len(self.connections) >= self.max_concurrency:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            if self.aborting.is_set():
                return False
            self.connections[request_id] = conn
            return True

    def register_thread(self, request_id, thread):
        with self.lock:
            self.threads[request_id] = thread

    def register_worker(self, request_id, process):
        with self.lock:
            if self.aborting.is_set():
                return False
            self.workers[request_id] = process
            return True

    def unregister_worker(self, request_id, process):
        with self.lock:
            if self.workers.get(request_id) is process:
                del self.workers[request_id]

    def complete(self, request_id):
        with self.condition:
            self.connections.pop(request_id, None)
            self.threads.pop(request_id, None)
            self.condition.notify_all()

    def abort(self):
        self.aborting.set()
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

    def join_threads(self):
        while True:
            with self.lock:
                threads = list(self.threads.values())
            if not threads:
                return
            for thread in threads:
                thread.join(timeout=0.1)


def handle_connection(args, state, conn, address, request_id, request_dir):
    request_state = {"method": "GET"}
    payload = None
    try:
        with conn:
            conn.settimeout(args.client_timeout_ms / 1000.0)
            try:
                request, request_path = read_request(
                    conn, address, args, request_id, request_dir, request_state,
                )
                response = run_worker(
                    args, request, request_path, request_dir, state, request_id,
                )
                status, headers, payload = response_parts(response, request, args)
                conn.settimeout(args.response_timeout_ms / 1000.0)
                safe_send_response(conn, status, payload, headers, request_state["method"])
            except socket.timeout:
                safe_send_response(conn, 408, "request timeout", method=request_state["method"])
            except HttpError as exc:
                safe_send_response(conn, exc.status, exc.message, method=request_state["method"])
            except (BrokenPipeError, ConnectionResetError, UnicodeEncodeError, OSError):
                pass
            except Exception as exc:
                print(f"http helper: request failed: {exc}", file=sys.stderr)
                safe_send_response(conn, 500, "internal server error", method=request_state["method"])
    finally:
        if isinstance(payload, dict) and payload.get("kind") == "file":
            payload["source"].close()
        shutil.rmtree(request_dir, ignore_errors=True)
        state.complete(request_id)


def reject_overload(conn):
    with conn:
        try:
            conn.settimeout(0.1)
            send_response(
                conn, 503, b"", (("retry-after", "1"),), method="HEAD",
            )
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass


def serve(args):
    state = ServerState()
    state.max_concurrency = args.max_concurrency
    parent_pid = os.getppid()

    def stop(_signum, _frame):
        state.abort()

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop)
    temp_root = tempfile.mkdtemp(prefix="nift-http-")
    server = None
    handled = 0
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        state.set_server(server)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(args.backlog)
        server.settimeout(0.2)
        while not state.aborting.is_set() and (args.max_requests == 0 or handled < args.max_requests):
            if os.getppid() != parent_pid:
                state.abort()
                break
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if state.aborting.is_set():
                    break
                raise
            handled += 1
            request_id = handled
            if not state.admit(request_id, conn):
                reject_overload(conn)
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
        if state.aborting.is_set():
            state.abort()
            state.terminate_workers()
        state.join_threads()
    finally:
        if state.aborting.is_set():
            state.abort()
            state.terminate_workers()
        state.join_threads()
        if server is not None:
            server.close()
        shutil.rmtree(temp_root, ignore_errors=True)
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
                  "max_file_response_bytes", "max_concurrency", "backlog"):
        if getattr(args, name) < 0:
            parser.error(f"{name.replace('_', '-')} must not be negative")
    if args.max_concurrency == 0 or args.max_concurrency > 128:
        parser.error("max-concurrency must be between 1 and 128")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(serve(parse_args(sys.argv[1:])))
    except OSError as exc:
        print(f"http helper: {exc}", file=sys.stderr)
        raise SystemExit(2)
