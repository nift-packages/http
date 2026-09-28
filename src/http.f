/*
    Backend-neutral HTTP server package for Nift. v0.1.0 process bootstrap.
*/

http_backend_requested := "auto"
http_backend_locked := false
http_backend_selected := ""

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
    app := map()
    app.set("kind", "http_server")
    app.set("backend", backend)
    app.set("config", config)
    app.set("routes", 0)
    return app
}

fn(http_is_server(app)) {
    return type(app) == "collection" && app.contains("kind") && app.get("kind") == "http_server"
}

fn(http_write_bootstrap_response()) {
    request := inject(getenv("NIFT_HTTP_REQUEST"))
    response := {
        "protocol":1,
        "request_id":request.request_id,
        "status":501,
        "headers":{"content-type":["text/plain; charset=utf-8"]},
        "body":{"kind":"text","text":"HTTP routing is not installed yet"}
    }
    output := ofstream(getenv("NIFT_HTTP_RESPONSE"))
    output.write_val(response)
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
    if(getenv("NIFT_HTTP_WORKER") == "1") { return http_write_bootstrap_response() }
    backend := app.get("backend")
    if(backend != "process" || !http_available_now()) {
        return {"ok":false,"error":"http process backend is unavailable","error_code":"backend_unavailable","backend":backend,"exit_code":127}
    }
    nift := http_nift_path()
    if(nift == null) {
        return {"ok":false,"error":"cannot locate the Nift executable","error_code":"worker_unavailable","backend":"process","exit_code":127}
    }
    config := app.get("config")
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
        "json":false,
        "binary_files":false,
        "streaming":false,
        "websockets":false,
        "tls":false,
        "persistent_workers":false
    } }
    server := (config) => http_server(config)
    listen := (app) => http_listen(app)
}

http := http_api()
export(http)
