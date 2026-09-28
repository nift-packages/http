# HTTP helper protocol

The process backend has two long-lived roles and bounded short-lived roles:

```text
client -> Python HTTP helper -> one fresh Nift application worker -> helper -> client
```

The helper owns the listening socket, HTTP parsing, finite wire limits, worker
lifetime and response serialization. The Nift application owns routes and
application behavior. A fresh invocation of the application script handles
each admitted request in v0.1.0. Multiple one-shot workers may run concurrently,
but each worker still owns exactly one request.

## Exchange directory

The helper creates one private temporary root per server and one numbered
directory per request. It sets these variables for the worker:

```text
NIFT_HTTP_WORKER=1
NIFT_HTTP_REQUEST=<request.json>
NIFT_HTTP_RESPONSE=<response.json>
```

`request.json` is a protocol-1 object:

```json
{
  "protocol": 1,
  "request_id": "1",
  "method": "POST",
  "target": "/items?q=a",
  "path": "/items",
  "segments": ["items"],
  "query": {"q": "a"},
  "headers": {"content-type": ["application/json"]},
  "body": {"kind": "text", "text": "{\"name\":\"A\"}"},
  "json": {"name": "A"},
  "form": {},
  "cookies": {"theme": "dark"},
  "remote_addr": "127.0.0.1"
}
```

Header names are lowercase and values are arrays. Query values are strings;
repeated keys are arrays. Path parameters are added by the Nift router rather
than the helper.

`application/x-www-form-urlencoded` populates `form`; request Cookie headers
populate `cookies`. A single name maps to a string and repeated names map to an
ordered array. Malformed form/cookie syntax is rejected before worker launch.

`response.json` is a protocol-1 object:

```json
{
  "protocol": 1,
  "request_id": "1",
  "status": 200,
  "headers": {"content-type": ["text/plain; charset=utf-8"]},
  "body": {"kind": "text", "text": "hello"},
  "cookies": [{"name":"theme","value":"dark","path":"/"}]
}
```

The implementation supports `text`, `json`, `empty`, `file` and `root_file`
response body kinds. The envelope deliberately leaves room for later stream
descriptors without changing route or status/header semantics. Arbitrary binary
request bodies use an opaque package-owned spool rather than exposing its
private path or requiring bytes to be UTF-8.

Structured response cookies are validated and serialized by the helper into
one `Set-Cookie` field per descriptor. Raw `set-cookie` headers and structured
cookies cannot be combined.

Multipart request metadata is private until the package facade sanitizes it.
The helper writes parts under generated request-owned names and supplies
internal IDs/paths to the facade. Before a handler runs, the facade replaces
them with upload descriptors:

```json
{
  "kind": "upload",
  "_upload_id": "request-logical-token",
  "name": "attachment",
  "filename": "client-name.bin",
  "content_type": "application/octet-stream",
  "size": 42
}
```

No helper path reaches ordinary handler values. The token is valid only in that
worker and is consumed through `http.save_upload()`. Generic binary request
bodies similarly become a `spooled` body consumed by `http.save_body()`.
Request cleanup always owns the original spool; saving copies it to the explicit
application destination.

File responses use either an application-authorized `file` path or a
`root_file` pair of explicit root plus untrusted relative path. The helper opens
and validates one regular file descriptor, derives framing/range metadata from
that descriptor and transfers it in file chunks/`sendfile` where available.
The worker never reads file bytes. Range support is one `bytes` range only;
invalid, unsatisfiable and multiple ranges return 416.

## Channel ownership

Protocol framing never uses worker stdout or stderr. The helper continuously
drains them through a private pipe, retains at most 8 KiB for failure diagnosis
and discards excess output. Application `print()` output therefore cannot block
or corrupt a response and cannot grow a worker log without bound.
The helper itself is silent during normal operation; startup and fatal
diagnostics belong to its stderr.

## Limits and failures

Defaults are an 8 KiB request line, 32 KiB total header section, 100 headers,
1 MiB body and 30 second worker deadline. The helper accepts HTTP/1.1, one
request per connection, and always closes the connection. It rejects
transfer encoding, duplicate `Content-Length`, folded headers, control bytes,
invalid percent escapes and malformed request syntax. HTTP/1.1 requires exactly
one non-empty `Host` header.

Malformed wire input receives a bounded 4xx/501 response without starting a
worker. Worker timeout receives 504. Worker launch, nonzero exit, missing or
invalid response metadata receives 500. The helper generates framing headers
and rejects application hop-by-hop headers.

The helper owns and removes every exchange directory. It monitors its Nift
parent and exits if that parent disappears. On POSIX each worker is placed in a
new process group; shutdown and timeout terminate the group before cleanup.
Windows uses a new process group but does not yet have Job Object coverage, so
Windows support remains unverified.

`max_concurrency` bounds each admitted socket from request receive through
response send and cleanup. The helper allocates no unbounded executor queue.
When all slots remain occupied after a short admission grace period, an accepted
connection receives an empty 503 without a request directory or worker launch.
Finite `max_requests` still counts accepted connections, including overloads.

This protocol is one-shot in v0.1. A future persistent worker can reuse the
same request/response envelope and request IDs over a separate control channel;
that is deliberately deferred.
