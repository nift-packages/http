# http

Backend-neutral HTTP server package for Nift. Version 0.1.0 currently provides
a process backend with facade-managed routing.

```nift
@import("http")

app := http.server({
    "host": "127.0.0.1",
    "port": 8080
})

http.get(app, "/", (request) => http.text("Hello from Nift"))
http.get(app, "/users/:id", (request) => http.json({
    "id": request.params.id
}))
http.post(app, "/echo", (request) => http.text(request.body.text))

result := http.listen(app)
```

The server handle is package data and all stateful behavior remains on the
`http` facade. `listen()` blocks until shutdown. For deterministic tests,
`max_requests` may stop the helper after a finite number of accepted requests.
`http.server_backend(app)` reports the concrete backend pinned to that server.

Backend inspection follows the package convention:

```nift
http.backends()
http.backend()
http.use_backend("process")
http.capabilities()
```

`auto` selects `process` when Python 3 and the bundled helper are usable.
Creating the first server freezes selection and pins the concrete backend into
that server handle. `ffi` and `native` are reserved but are not advertised.
If no process backend is usable, `http.server()` returns an error object with
`ok: false` and `error_code: "backend_unavailable"`; no server resource is
registered. The failed first-use attempt still freezes package selection.

The process helper owns HTTP parsing and serialization. The default `oneshot`
mode uses one fresh Nift application worker per request. Optional persistent
workers still handle at most one request at a time. Worker stdout is not
protocol framing, so application `print()` calls cannot corrupt responses. See
[PROTOCOL.md](PROTOCOL.md).

In one-shot mode each request reruns the application script, so top-level route
registration must be deterministic and other top-level side effects run once
per request. In persistent mode top-level setup runs once per worker instead.

Routes support GET, POST, PUT, PATCH, DELETE and HEAD. `http.route()` accepts an
additional method. Static segments and `:name` parameters are matched in Nift;
HEAD falls back to a matching GET route while the helper suppresses its body.
Request objects provide `method`, `target`, decoded `path`, `params`, `query`,
lowercase array-valued `headers`, `body`, parsed `json` and `remote_addr`.

Response options support custom status and headers:

```nift
return http.json({"created": true}, {
    "status": 201,
    "headers": {"x-resource": "42"}
})
```

URL-encoded forms are parsed only for
`application/x-www-form-urlencoded`. `request.form` contains strings for
single fields and arrays for repeated names; blank values are retained. UTF-8
is the only accepted form charset. Malformed escapes/encoding and configured
field limits fail before the worker starts.

`request.cookies` uses the same single-string/repeated-array convention. Cookie
syntax is parsed conservatively and values are not percent-decoded. Response
cookies use structured descriptors so repeated `Set-Cookie` remains separate:

```nift
return http.text("ok", {"cookies": [
    http.cookie("theme", "dark", {
        "path": "/",
        "max_age": 3600,
        "secure": true,
        "http_only": true,
        "same_site": "Lax"
    })
]})
```

Supported attributes are Path, Domain, Max-Age, Expires, Secure, HttpOnly and
SameSite. Sessions, authentication and CSRF policy remain application concerns.

Server options can lower the finite defaults for `max_request_line`,
`max_header_bytes`, `max_headers`, `max_body_bytes`, `client_timeout_ms`,
`worker_timeout_ms` and `backlog`. HTTP/1.1 requires exactly one Host header;
transfer encoding and duplicate Content-Length are rejected. JSON request
bodies are decoded by the helper into `request.json`; malformed JSON receives
400 without invoking application code.
Form-specific finite options are `max_form_fields`, `max_form_name_bytes` and
`max_form_value_bytes`; `max_cookie_pairs` bounds parsed request cookies.

Multipart requests populate `request.form`, `request.uploads` and
`request.files`. A single file field is an upload object; repeated file fields
become arrays. Upload objects contain only logical identity plus `name`,
`filename`, `content_type` and `size`; client filenames are metadata and are
never used as spool paths.

```nift
upload := request.files.attachment
saved := http.save_upload(upload, "data/attachment.bin")
```

`save_upload` copies bytes to an application-chosen destination while the
request is active. Arbitrary non-UTF-8 non-multipart bodies use an opaque
`request.body` with `kind: "spooled"`; `http.save_body()` copies it. Handles
expire when the one-shot worker exits, and helper-owned request files are then
removed. Small UTF-8 text and JSON bodies retain their convenient buffered
forms.

Multipart limits include `max_multipart_parts`, `max_multipart_files`,
`max_part_header_bytes`, `max_part_headers`, `max_file_bytes`,
`max_filename_bytes` and `max_temp_bytes`. The aggregate `max_body_bytes` limit
remains authoritative. The current helper parser is bounded but internally
buffers the aggregate body before creating per-file spools; the public API does
not depend on that implementation and remains compatible with later streaming.

File responses are helper-streamed and never loaded into a Nift string:

```nift
return http.file("data/report.pdf", {
    "content_type": "application/pdf",
    "download_name": "report.pdf"
})
```

`http.file()` uses an application-authorized path. Do not pass untrusted route
input to it. `http.file_from(root, relative)` is the untrusted relative-path
facility: it rejects absolute/empty/dot/backslash components and, on POSIX,
opens each component without following symlinks. Single byte ranges produce
206/416 with generated Content-Range/Length and HEAD parity. Multiple ranges
are deliberately unsupported. `max_file_response_bytes` and
`response_timeout_ms` are finite server options.

`max_concurrency` bounds the complete active connection lifecycle and defaults
to 1. Independent accepted requests run in fresh Nift workers concurrently up
to that limit. When all slots remain occupied, the helper returns an empty 503
after a short admission grace period; it does not create a request directory or
an unbounded queued thread. One-shot workers still reconstruct top-level state,
and application persistence must provide its own synchronization when
concurrency is greater than 1.

Persistent mode is explicit and uses a fixed pool:

```nift
app := http.server({
    "host": "127.0.0.1",
    "port": 8080,
    "max_concurrency": 4,
    "worker_mode": "persistent",
    "worker_pool_size": 4,
    "worker_max_requests": 1000
})
```

`worker_pool_size` must equal `max_concurrency`, so an admitted request never
waits outside its configured worker deadline. Workers register routes once,
then handle one request at a time through private exchange files.
Crashes, timeouts and the finite `worker_max_requests` threshold replace a
worker without replaying its request. Worker stdin is reserved for package
control in this mode.

Mutable top-level state is strictly worker-local. With four workers, four
simultaneous `counter += 1` requests can all return 1; it is not coherent shared
application state. Sessions, records and other shared data require synchronized
filesystem/database/service storage. Request upload/body handles are explicitly
expired after every dispatch even though the worker remains alive.

Operational options are also finite. `admission_timeout_ms` controls how long
one accepted connection may wait for a slot before 503 (default 20 ms).
`shutdown_grace_ms` bounds graceful drain (default 5 seconds): the first
SIGTERM/SIGINT stops admission and drains active requests; a second signal or
deadline expiry closes sockets and terminates worker groups.

Optional observability writes no protocol data to stdout:

```nift
app := http.server({
    "status_path": "run/http-status.json",
    "event_log_path": "run/http-events.ndjson",
    "max_event_log_bytes": 1048576
})
```

The status file is atomically replaced and reports readiness/phase, active
requests/workers, queue depth, worker process count and accepted/admitted/
rejected/completed/error/start/restart/recycle counters. The event file contains
bounded NDJSON request/overload records with request and worker IDs where
available; it truncates before exceeding `max_event_log_bytes`.

Current scope is plain HTTP/1.1 with `Connection: close`. TLS,
incremental handler streams, WebSockets, FFI and native modules are
deferred.
