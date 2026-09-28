# HTTP helper protocol

The process backend has two long-lived server roles and bounded worker roles:

```text
client -> Python HTTP helper -> one Nift application worker -> helper -> client
```

The helper owns the listening socket, HTTP parsing, finite wire limits, worker
lifetime and response serialization. The Nift application owns routes and
application behavior. One-shot mode starts a fresh application process for each
admitted request. Persistent mode starts a fixed pool and reuses each process,
but a worker still owns at most one request at a time.

## Exchange directory

The helper creates one private temporary root per server and one numbered
directory per request. It sets these variables for the worker:

```text
NIFT_HTTP_WORKER=1
NIFT_HTTP_REQUEST=<request.json>
NIFT_HTTP_RESPONSE=<response.json>
```

Persistent workers instead receive `NIFT_HTTP_WORKER=persistent` and a private
startup-ready path. After route registration they block on newline-delimited,
helper-generated exchange-directory paths from stdin. Each directory contains
the same `request.json`; the worker writes `response.tmp`, closes it and renames
it to `response.json` as the atomic completion signal. EOF requests graceful
worker exit. Application code must not read worker stdin in persistent mode.

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

The implementation supports `text`, `json`, `empty`, `file`, `root_file` and
`stream` response body kinds. A stream envelope contains no pipe, spool or
worker path. Arbitrary binary request bodies use an opaque package-owned spool
rather than exposing its private path or requiring bytes to be UTF-8.

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

Dynamic streams use a helper-created request-owned POSIX FIFO plus a separate
completion marker. Both paths are private request metadata removed before the
handler runs. The Nift `http.stream()` call atomically publishes the ordinary
status/header envelope, opens the FIFO and synchronously invokes its producer.
Producer writes are raw bytes; the helper reads at most
`max_stream_chunk_bytes`, applies `max_stream_response_bytes`, and owns HTTP/1.1
chunked framing. The FIFO and socket buffers provide bounded flow control: a
producer blocks when the client/helper cannot consume more data. The completion
marker is created only after the producer and outer route handler both return.
This keeps one-shot and persistent worker leases active for the full request.

Worker stdout remains diagnostics and is never a stream channel. A stream that
fails after headers or bytes were committed ends without the terminal HTTP
chunk; no second 500 response is appended. HEAD skips the producer but waits for
outer handler completion. Streamed 204, 205 and 304 bodies are invalid. Stream
paths and completion state never enter application-visible request values or the
response envelope.

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

TCP request-side half-close is unsupported. EOF is client cancellation even if
the peer keeps its read side open; this is part of the current one-request,
`Connection: close` process-backend contract.

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
`admission_timeout_ms` configures the bounded admission wait. The coordinator
does not allocate one queued thread per waiting client; additional connections
remain bounded by the kernel listen backlog.

The first termination signal closes the listener and enters graceful drain.
Active request sockets/workers remain valid until completion or the finite
`shutdown_grace_ms` deadline. Deadline expiry, parent death or a second signal
closes active sockets, terminates all registered worker groups and then removes
request directories. The optional atomic status snapshot exposes `starting`,
`ready`, `draining`, `aborting` and `stopped` phases. Optional NDJSON events are
size-bounded and remain separate from HTTP and worker framing.

Persistent workers reuse the same protocol-1 request/response envelopes and
request IDs as one-shot workers. Binary bodies and uploads remain in private
spools rather than crossing the stdin control channel or JSON framing. A crash,
timeout, invalid envelope or request-count recycle replaces that worker and
never retries the uncertain request.
