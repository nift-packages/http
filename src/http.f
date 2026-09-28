/*
    Backend-neutral HTTP server package for Nift. v0.1.0 process bootstrap.
*/

http_backend_requested := "auto"
http_backend_locked := false
http_backend_selected := ""
http_next_server_id := 0
http_server_backends := map()
http_server_configs := map()
http_server_route_counts := map()
http_server_routes := map()

fn(http_helper_path()) {
    return pwd() + "/.nift/packages/http/helper/http_helper.py"
}

fn(http_python_path()) {
    override := getenv("NIFT_HTTP_PYTHON")
    if(override != null && override != "") { return override }
    return which("python3")
}

fn(http_nift_path()) {
    override := getenv("NIFT_HTTP_NIFT")
    if(override != null && override != "") { return override }
    return which("nift")
}

fn(http_available_now()) {
    if(getenv("NIFT_NO_PROCESS") != null) { return false }
    python := http_python_path()
    return python != null && exists(http_helper_path())
}

fn(http_backend_names()) {
    if(http_available_now()) { return ["process"] }
    return []
}

fn(http_resolved_backend()) {
    if(http_backend_locked) {
        if(http_backend_selected != "") { return http_backend_selected }
        return null
    }
    if(http_backend_requested == "process") {
        if(http_available_now()) { return "process" }
        return null
    }
    if(http_backend_requested == "auto" && http_available_now()) { return "process" }
    return null
}

fn(http_use_backend(name)) {
    if(http_backend_locked) {
        return {"ok":false,"error":"http backend is already selected","error_code":"backend_locked","backend":http_resolved_backend()}
    }
    if(name != "auto" && name != "process" && name != "ffi" && name != "native") {
        return {"ok":false,"error":"unknown http backend: " + name,"error_code":"unknown_backend","backend":http_resolved_backend()}
    }
    if(name != "auto" && name != "process") {
        return {"ok":false,"error":"http backend is not implemented: " + name,"error_code":"backend_unavailable","backend":http_resolved_backend()}
    }
    if(name == "process" && !http_available_now()) {
        return {"ok":false,"error":"http process backend is unavailable","error_code":"backend_unavailable","backend":http_resolved_backend()}
    }
    http_backend_requested = name
    return {"ok":true,"error":"","error_code":"","backend":http_resolved_backend()}
}

fn(http_server(config)) {
    backend := http_resolved_backend()
    if(backend != null) { http_backend_selected = backend }
    http_backend_locked = true
    http_next_server_id += 1
    id := http_next_server_id
    http_server_backends.set(id, backend)
    http_server_configs.set(id, config)
    http_server_route_counts.set(id, 0)
    return {"kind":"http_server","_server_id":id}
}

fn(http_is_server(app)) {
    if(type(app) != "object" || !app.has("kind") || !app.has("_server_id")) { return false }
    if(app.kind != "http_server") { return false }
    return http_server_backends.contains(app._server_id)
}

fn(http_add_route(app, method, path, handler)) {
    if(!http_is_server(app)) {
        return {"ok":false,"error":"invalid http server handle","error_code":"invalid_handle","backend":null}
    }
    if(type(path) != "string" || !path.starts_with("/") || path.contains("?") || path.contains("#")) {
        return {"ok":false,"error":"route path must be an origin path","error_code":"invalid_route","backend":http_server_backends.get(app._server_id)}
    }
    if(type(handler) != "function") {
        return {"ok":false,"error":"route handler must be callable","error_code":"invalid_handler","backend":http_server_backends.get(app._server_id)}
    }
    index := http_server_route_counts.get(app._server_id)
    key := app._server_id.to_string() + ":" + index.to_string()
    http_server_routes.set(key, {"method":method,"path":path,"handler":handler})
    http_server_route_counts.set(app._server_id, index + 1)
    return {"ok":true,"error":"","error_code":"","backend":http_server_backends.get(app._server_id)}
}

fn(http_route_parts(path)) {
    if(path == "/") { return [] }
    inner := path.substr(1)
    if(inner.ends_with("/")) { inner = inner.substr(0, inner.length() - 1) }
    if(inner == "") { return [] }
    return inner.split("/")
}

fn(http_match_route(pattern, segments)) {
    parts := http_route_parts(pattern)
    if(parts.size() != segments.size()) { return null }
    params := {}
    i := 0
    while(i < parts.size()) {
        part := parts[i]
        if(part.starts_with(":")) {
            if(part.length() == 1) { return null }
            param_key := part.substr(1)
            params[param_key] = segments[i]
        }
        else if(part != segments[i]) { return null }
        i += 1
    }
    return params
}

fn(http_options(options)) {
    if(options.size() > 0 && type(options[0]) == "object") { return options[0] }
    return {}
}

fn(http_response_headers(kind, options)) {
    headers := {}
    if(kind == "json") { headers["content-type"] = ["application/json; charset=utf-8"] }
    else { headers["content-type"] = ["text/plain; charset=utf-8"] }
    if(options.has("headers") && type(options.headers) == "object") {
        for((name, value) : options.headers) { headers[name] = value }
    }
    return headers
}

fn(http_text_response(body, option_values)) {
    options := http_options(option_values)
    status := 200
    if(options.has("status")) { status = options.status }
    return {"status":status,"headers":http_response_headers("text", options),"body":{"kind":"text","text":body}}
}

fn(http_json_response(value, option_values)) {
    options := http_options(option_values)
    status := 200
    if(options.has("status")) { status = options.status }
    return {"status":status,"headers":http_response_headers("json", options),"body":{"kind":"json","value":value}}
}

fn(http_dispatch(app, request)) {
    allowed := []
    selected_index := -1
    selected_params := {}
    i := 0
    while(i < http_server_route_counts.get(app._server_id)) {
        key := app._server_id.to_string() + ":" + i.to_string()
        route := http_server_routes.get(key)
        params := http_match_route(route.path, request.segments)
        if(params != null) {
            if(!allowed.contains(route.method)) { allowed.push(route.method) }
            if(route.method == request.method || (request.method == "HEAD" && route.method == "GET")) {
                selected_index = i
                selected_params = params
                break
            }
        }
        i += 1
    }
    if(selected_index == -1) {
        if(allowed.size() > 0) {
            result := {"status":405,"headers":{"allow":[allowed.join(", ")],"content-type":["text/plain; charset=utf-8"]},"body":{"kind":"text","text":"method not allowed"}}
            return result
        }
        result := {"status":404,"headers":{"content-type":["text/plain; charset=utf-8"]},"body":{"kind":"text","text":"not found"}}
        return result
    }
    selected_key := app._server_id.to_string() + ":" + selected_index.to_string()
    selected := http_server_routes.get(selected_key)
    request["params"] = selected_params
    handler := selected.handler
    response := handler(request)
    if(type(response) == "string") { return http_text_response(response, []) }
    return response
}

fn(http_write_worker_response(app)) {
    request := inject(getenv("NIFT_HTTP_REQUEST"))
    response := http_dispatch(app, request)
    envelope := {
        "protocol":1,
        "request_id":request.request_id,
        "status":response.status,
        "headers":response.headers,
        "body":response.body
    }
    output := ofstream(getenv("NIFT_HTTP_RESPONSE"))
    output.write_val(envelope)
    close(output)
    return {"ok":true,"error":"","error_code":"","backend":"process"}
}

fn(http_config_value(config, name, fallback)) {
    if(type(config) == "object" && config.has(name)) { return config.get(name) }
    return fallback
}

fn(http_listen(app)) {
    if(!http_is_server(app)) {
        return {"ok":false,"error":"invalid http server handle","error_code":"invalid_handle","backend":null,"exit_code":null}
    }
    if(getenv("NIFT_HTTP_WORKER") == "1") { return http_write_worker_response(app) }
    backend := http_server_backends.get(app._server_id)
    if(backend != "process" || !http_available_now()) {
        return {"ok":false,"error":"http process backend is unavailable","error_code":"backend_unavailable","backend":backend,"exit_code":127}
    }
    nift := http_nift_path()
    if(nift == null) {
        return {"ok":false,"error":"cannot locate the Nift executable","error_code":"worker_unavailable","backend":"process","exit_code":127}
    }
    config := http_server_configs.get(app._server_id)
    host := http_config_value(config, "host", "127.0.0.1")
    port := http_config_value(config, "port", 8080)
    max_requests := http_config_value(config, "max_requests", 0)
    worker_timeout := http_config_value(config, "worker_timeout_ms", 30000)
    result := run(
        http_python_path(), http_helper_path(),
        "--host", host,
        "--port", port.to_string(),
        "--nift", nift,
        "--app", cmd,
        "--cwd", pwd(),
        "--max-requests", max_requests.to_string(),
        "--worker-timeout-ms", worker_timeout.to_string()
    )
    if(!result.launched) {
        return {"ok":false,"error":"failed to launch HTTP helper","error_code":"helper_launch","backend":"process","exit_code":result.exit_code}
    }
    if(result.exit_code != 0) {
        return {"ok":false,"error":result.stderr,"error_code":"helper_failed","backend":"process","exit_code":result.exit_code}
    }
    return {"ok":true,"error":"","error_code":"","backend":"process","exit_code":0}
}

@struct(http_api) {
    available := () => http_available_now()
    backends := () => http_backend_names()
    backend := () => http_resolved_backend()
    use_backend := (name) => http_use_backend(name)
    capabilities := () => { return {
        "backend":"process",
        "buffered_text":true,
        "json":true,
        "binary_files":false,
        "streaming":false,
        "websockets":false,
        "tls":false,
        "persistent_workers":false
    } }
    server := (config) => http_server(config)
    route := (app, method, path, handler) => http_add_route(app, method.to_upper(), path, handler)
    get := (app, path, handler) => http_add_route(app, "GET", path, handler)
    post := (app, path, handler) => http_add_route(app, "POST", path, handler)
    put := (app, path, handler) => http_add_route(app, "PUT", path, handler)
    patch := (app, path, handler) => http_add_route(app, "PATCH", path, handler)
    delete := (app, path, handler) => http_add_route(app, "DELETE", path, handler)
    head := (app, path, handler) => http_add_route(app, "HEAD", path, handler)
    text := (body, ...options) => http_text_response(body, options)
    json := (value, ...options) => http_json_response(value, options)
    listen := (app) => http_listen(app)
}

http := http_api()
export(http)
