# http

Backend-neutral HTTP server package for Nift. Version 0.1.0 currently provides
a process backend and a helper/worker bootstrap; routing is added in the next
checkpoint.

```nift
@import("http")

app := http.server({
    "host": "127.0.0.1",
    "port": 8080
})

result := http.listen(app)
```

The server handle is package data and all stateful behavior remains on the
`http` facade. `listen()` blocks until shutdown. For deterministic tests,
`max_requests` may stop the helper after a finite number of accepted requests.

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

Current scope is plain HTTP/1.0 and HTTP/1.1 with `Connection: close`. TLS,
streaming, multipart, WebSockets, persistent workers, FFI and native modules are
deferred. CP04 intentionally returns 501 from the bootstrap worker until routes
are introduced in CP05.
