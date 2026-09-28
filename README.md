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

The process helper owns HTTP parsing and serialization. A fresh Nift application
worker owns one request. Worker stdout is not protocol framing, so application
`print()` calls cannot corrupt responses. See [PROTOCOL.md](PROTOCOL.md).

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

Server options can lower the finite defaults for `max_request_line`,
`max_header_bytes`, `max_headers`, `max_body_bytes`, `client_timeout_ms`,
`worker_timeout_ms` and `backlog`. HTTP/1.1 requires exactly one Host header;
transfer encoding and duplicate Content-Length are rejected. JSON request
bodies are decoded by the helper into `request.json`; malformed JSON receives
400 without invoking application code.

Current scope is plain HTTP/1.0 and HTTP/1.1 with `Connection: close`. TLS,
streaming, multipart, WebSockets, persistent workers, FFI and native modules are
deferred.
