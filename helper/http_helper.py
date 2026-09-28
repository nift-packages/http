#!/usr/bin/env python3
"""Strict, dependency-free HTTP/1.x helper for the Nift http package."""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl


TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HEX = frozenset("0123456789abcdefABCDEF")
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
}
REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request",
    404: "Not Found", 414: "URI Too Long",
    405: "Method Not Allowed", 408: "Request Timeout",
    413: "Payload Too Large", 431: "Request Header Fields Too Large",
    500: "Internal Server Error", 501: "Not Implemented",
    504: "Gateway Timeout",
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


def recv_headers(conn, limit):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(min(4096, limit + 1 - len(data)))
        if not chunk:
            raise HttpError(400, "incomplete request headers")
        data.extend(chunk)
        if len(data) > limit:
            raise HttpError(431, "request headers are too large")
    marker = data.index(b"\r\n\r\n")
    return bytes(data[:marker]), bytes(data[marker + 4:])


def read_request(conn, address, args, request_id, request_dir):
    header_block, remainder = recv_headers(conn, args.max_header_bytes)
    lines = header_block.split(b"\r\n")
    if not lines or len(lines[0]) > args.max_request_line:
        raise HttpError(400 if not lines else 414, "invalid request line")
    try:
        request_line = lines[0].decode("ascii")
        method, target, version = request_line.split(" ")
    except (UnicodeDecodeError, ValueError) as exc:
        raise HttpError(400, "malformed request line") from exc
    if not TOKEN.fullmatch(method.encode("ascii")) or version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HttpError(400, "unsupported request syntax")
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
        if not content_lengths[0].isdigit():
            raise HttpError(400, "invalid content-length")
        length = int(content_lengths[0])
    if length > args.max_body_bytes:
        raise HttpError(413, "request body is too large")
    body = bytearray(remainder[:length])
    while len(body) < length:
        chunk = conn.recv(min(65536, length - len(body)))
        if not chunk:
            raise HttpError(400, "incomplete request body")
        body.extend(chunk)

    raw_path, separator, raw_query = target.partition("?")
    try:
        path = strict_unquote(raw_path)
        query_pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=False,
                                encoding="utf-8", errors="strict") if separator else []
    except (UnicodeDecodeError, ValueError) as exc:
        raise HttpError(400, "invalid query encoding") from exc
    query = {}
    for key, value in query_pairs:
        if key in query:
            if not isinstance(query[key], list):
                query[key] = [query[key]]
            query[key].append(value)
        else:
            query[key] = value

    body_path = os.path.join(request_dir, "request.body")
    with open(body_path, "wb") as output:
        output.write(body)
    try:
        body_text = body.decode("utf-8")
        body_value = {"kind": "text", "text": body_text}
    except UnicodeDecodeError:
        body_text = None
        body_value = {"kind": "file", "path": body_path}

    json_value = None
    content_type = headers.get("content-type", [""])[-1].split(";", 1)[0].strip().lower()
    if body and (content_type == "application/json" or content_type.endswith("+json")):
        if body_text is None:
            raise HttpError(400, "JSON body is not UTF-8")
        try:
            json_value = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise HttpError(400, "malformed JSON body") from exc

    request = {
        "protocol": 1,
        "request_id": str(request_id),
        "method": method.upper(),
        "target": target,
        "path": path,
        "segments": [strict_unquote(part) for part in raw_path.split("/")[1:] if part != ""],
        "query": query,
        "headers": headers,
        "body": body_value,
        "json": json_value,
        "remote_addr": address[0],
    }
    request_path = os.path.join(request_dir, "request.json")
    with open(request_path, "w", encoding="utf-8") as output:
        json.dump(request, output, ensure_ascii=False, separators=(",", ":"))
    return request, request_path


def terminate_process(process):
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=0.5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def run_worker(args, request, request_path, request_dir, state):
    response_path = os.path.join(request_dir, "response.json")
    log_path = os.path.join(request_dir, "worker.log")
    environment = dict(os.environ)
    environment.update({
        "NIFT_HTTP_WORKER": "1",
        "NIFT_HTTP_REQUEST": request_path,
        "NIFT_HTTP_RESPONSE": response_path,
    })
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with open(log_path, "wb") as log:
        try:
            process = subprocess.Popen(
                [args.nift, args.app], cwd=args.cwd, env=environment,
                stdout=log, stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix", creationflags=creationflags,
            )
        except OSError as exc:
            raise HttpError(500, f"worker launch failed: {exc}") from exc
        state["worker"] = process
        try:
            process.wait(timeout=args.worker_timeout_ms / 1000.0)
        except subprocess.TimeoutExpired as exc:
            terminate_process(process)
            raise HttpError(504, "application worker timed out") from exc
        finally:
            state["worker"] = None
    if process.returncode != 0:
        try:
            with open(log_path, "rb") as source:
                diagnostic = source.read(8192).decode("utf-8", "replace").strip()
            if diagnostic:
                print(f"http helper: worker failed: {diagnostic}", file=sys.stderr)
        except OSError:
            pass
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


def response_parts(response):
    status = response.get("status")
    if not isinstance(status, int) or isinstance(status, bool) or status < 100 or status > 599:
        raise HttpError(500, "invalid application response status")
    raw_headers = response.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise HttpError(500, "invalid application response headers")
    headers = []
    for name, values in raw_headers.items():
        if not isinstance(name, str) or not TOKEN.fullmatch(name.encode("ascii", "ignore")):
            raise HttpError(500, "invalid application response header name")
        key = name.lower()
        if key in HOP_BY_HOP:
            raise HttpError(500, "application supplied a hop-by-hop response header")
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HttpError(500, "invalid application response header value")
            headers.append((key, value))
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
        payload = json.dumps(body.get("value"), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        raise HttpError(500, "unsupported application response body kind")
    return status, headers, payload


def send_response(conn, status, body, headers=(), method="GET"):
    reason = REASONS.get(status, "Response")
    payload = body.encode("utf-8") if isinstance(body, str) else body
    lines = [f"HTTP/1.1 {status} {reason}\r\n"]
    seen_type = False
    for name, value in headers:
        if name.lower() == "content-type":
            seen_type = True
        lines.append(f"{name}: {value}\r\n")
    if not seen_type:
        lines.append("Content-Type: text/plain; charset=utf-8\r\n")
    lines.append(f"Content-Length: {len(payload)}\r\n")
    lines.append("Connection: close\r\n\r\n")
    conn.sendall("".join(lines).encode("latin-1") + (b"" if method == "HEAD" else payload))


def serve(args):
    state = {"stop": False, "worker": None}
    parent_pid = os.getppid()

    def stop(_signum, _frame):
        state["stop"] = True
        terminate_process(state["worker"])

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop)
    temp_root = tempfile.mkdtemp(prefix="nift-http-")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(args.backlog)
    server.settimeout(0.2)
    handled = 0
    try:
        while not state["stop"] and (args.max_requests == 0 or handled < args.max_requests):
            if os.getppid() != parent_pid:
                break
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            handled += 1
            request_dir = os.path.join(temp_root, str(handled))
            os.mkdir(request_dir, 0o700)
            method = "GET"
            with conn:
                conn.settimeout(args.client_timeout_ms / 1000.0)
                try:
                    request, request_path = read_request(conn, address, args, handled, request_dir)
                    method = request["method"]
                    response = run_worker(args, request, request_path, request_dir, state)
                    status, headers, payload = response_parts(response)
                    send_response(conn, status, payload, headers, method)
                except socket.timeout:
                    send_response(conn, 408, "request timeout", method=method)
                except HttpError as exc:
                    send_response(conn, exc.status, exc.message, method=method)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    shutil.rmtree(request_dir, ignore_errors=True)
    finally:
        terminate_process(state["worker"])
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
    parser.add_argument("--max-request-line", type=int, default=8192)
    parser.add_argument("--max-header-bytes", type=int, default=32768)
    parser.add_argument("--max-headers", type=int, default=100)
    parser.add_argument("--max-body-bytes", type=int, default=1048576)
    parser.add_argument("--backlog", type=int, default=16)
    args = parser.parse_args(argv)
    if not (0 <= args.port <= 65535):
        parser.error("port must be between 0 and 65535")
    for name in ("max_requests", "worker_timeout_ms", "client_timeout_ms",
                 "max_request_line", "max_header_bytes", "max_headers",
                 "max_body_bytes", "backlog"):
        if getattr(args, name) < 0:
            parser.error(f"{name.replace('_', '-')} must not be negative")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(serve(parse_args(sys.argv[1:])))
    except OSError as exc:
        print(f"http helper: {exc}", file=sys.stderr)
        raise SystemExit(2)
