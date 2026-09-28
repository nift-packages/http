# HTTP helper protocol

The process backend has two long-lived roles and one short-lived role:

```text
client -> Python HTTP helper -> one Nift application worker -> helper -> client
```

The helper owns the listening socket, HTTP parsing, finite wire limits, worker
lifetime and response serialization. The Nift application owns routes and
application behavior. A fresh invocation of the application script handles
each request in v0.1.0.

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
  "remote_addr": "127.0.0.1"
}
```

Header names are lowercase and values are arrays. Query values are strings;
repeated keys are arrays. Path parameters are added by the Nift router rather
than the helper.

`response.json` is a protocol-1 object:

```json
{
  "protocol": 1,
  "request_id": "1",
  "status": 200,
  "headers": {"content-type": ["text/plain; charset=utf-8"]},
  "body": {"kind": "text", "text": "hello"}
}
```

The initial implementation supports `text`, `json` and `empty` response body
kinds. The envelope deliberately leaves room for later package-owned `file`
and `stream` descriptors without changing route or status/header semantics.
Request bytes that are not UTF-8 are represented as a private `file` body; CP06
does not expose a binary reader yet.

## Channel ownership

Protocol framing never uses worker stdout or stderr. They are redirected to
bounded-lifetime files inside the request directory and removed with that
directory. Application `print()` output therefore cannot corrupt a response.
The helper itself is silent during normal operation; startup and fatal
diagnostics belong to its stderr.

## Limits and failures

Defaults are an 8 KiB request line, 32 KiB total header section, 100 headers,
1 MiB body and 30 second worker deadline. The helper accepts HTTP/1.0 and 1.1,
one request per connection, and always closes the connection. It rejects
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

This protocol is one-shot in CP04-CP06. A future persistent worker can reuse the
same request/response envelope and request IDs over a separate control channel;
that is deliberately deferred.
