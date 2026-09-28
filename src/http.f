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
http_spool_paths := map()

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
    return python != null && http_nift_path() != null && exists(http_helper_path())
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
    if(backend == null) {
        return {"kind":"http_server_error","ok":false,"error":"http process backend is unavailable","error_code":"backend_unavailable","backend":null}
    }
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
    if(type(method) != "string" || method == "") {
        return {"ok":false,"error":"route method must be a non-empty string","error_code":"invalid_method","backend":http_server_backends.get(app._server_id)}
    }
    method = method.to_upper()
    token_chars := "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for(method_char : method.split("")) {
        if(!token_chars.contains(method_char)) {
            return {"ok":false,"error":"route method is not an HTTP token","error_code":"invalid_method","backend":http_server_backends.get(app._server_id)}
        }
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
    else if(kind == "file") { headers["content-type"] = ["application/octet-stream"] }
    else { headers["content-type"] = ["text/plain; charset=utf-8"] }
    if(options.has("content_type")) { headers["content-type"] = [options.content_type] }
    if(options.has("headers") && type(options.headers) == "object") {
        for((header_name, header_value) : options.headers) {
            lower_header := header_name.to_lower()
            headers[lower_header] = header_value
        }
    }
    return headers
}

fn(http_text_response(body, option_values)) {
    options := http_options(option_values)
    status := 200
    if(options.has("status")) { status = options.status }
    response := {"status":status,"headers":http_response_headers("text", options),"body":{"kind":"text","text":body}}
    if(options.has("cookies")) { response["cookies"] = options.cookies }
    return response
}

fn(http_json_response(value, option_values)) {
    options := http_options(option_values)
    status := 200
    if(options.has("status")) { status = options.status }
    response := {"status":status,"headers":http_response_headers("json", options),"body":{"kind":"json","value":value}}
    if(options.has("cookies")) { response["cookies"] = options.cookies }
    return response
}

fn(http_cookie(cookie_name, cookie_value, option_values)) {
    options := http_options(option_values)
    cookie := {"name":cookie_name,"value":cookie_value}
    if(options.has("path")) { cookie["path"] = options.path }
    if(options.has("domain")) { cookie["domain"] = options.domain }
    if(options.has("max_age")) { cookie["max_age"] = options.max_age }
    if(options.has("expires")) { cookie["expires"] = options.expires }
    if(options.has("secure")) { cookie["secure"] = options.secure }
    if(options.has("http_only")) { cookie["http_only"] = options.http_only }
    if(options.has("same_site")) { cookie["same_site"] = options.same_site }
    return cookie
}

fn(http_file_response(path, option_values)) {
    options := http_options(option_values)
    status := 200
    if(options.has("status")) { status = options.status }
    body := {"kind":"file","path":path}
    if(options.has("download_name")) { body["download_name"] = options.download_name }
    return {"status":status,"headers":http_response_headers("file", options),"body":body}
}

fn(http_root_file_response(root, path, option_values)) {
    options := http_options(option_values)
    status := 200
    if(options.has("status")) { status = options.status }
    body := {"kind":"root_file","root":root,"path":path}
    if(options.has("download_name")) { body["download_name"] = options.download_name }
    return {"status":status,"headers":http_response_headers("file", options),"body":body}
}

fn(http_dispatch(app, request)) {
    allowed := []
    selected_index := -1
    fallback_index := -1
    selected_params := {}
    fallback_params := {}
    i := 0
    while(i < http_server_route_counts.get(app._server_id)) {
        key := app._server_id.to_string() + ":" + i.to_string()
        route := http_server_routes.get(key)
        params := http_match_route(route.path, request.segments)
        if(params != null) {
            if(!allowed.contains(route.method)) { allowed.push(route.method) }
            if(route.method == "GET" && !allowed.contains("HEAD")) { allowed.push("HEAD") }
            if(route.method == request.method) {
                selected_index = i
                selected_params = params
                break
            }
            if(request.method == "HEAD" && route.method == "GET" && fallback_index == -1) {
                fallback_index = i
                fallback_params = params
            }
        }
        i += 1
    }
    if(selected_index == -1 && fallback_index != -1) {
        selected_index = fallback_index
        selected_params = fallback_params
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

fn(http_prepare_request(request)) {
    if(request.has("_body_path")) {
        body_token := request.request_id + ":body"
        http_spool_paths.set(body_token, request._body_path)
        request["body"] = {"kind":"spooled","size":request.body.size,"_body_id":body_token}
    }
    public_uploads := []
    files := {}
    if(request.has("_uploads")) {
        for(raw_upload : request._uploads) {
            upload_token := request.request_id + ":upload:" + raw_upload.id
            http_spool_paths.set(upload_token, raw_upload.path)
            upload := {
                "kind":"upload",
                "_upload_id":upload_token,
                "name":raw_upload.field,
                "filename":raw_upload.filename,
                "content_type":raw_upload.content_type,
                "size":raw_upload.size
            }
            public_uploads.push(upload)
            field_key := raw_upload.field
            if(files.has(field_key)) {
                existing := files[field_key]
                if(type(existing) == "array") {
                    repeated := copy(existing)
                    repeated.push(upload)
                    files[field_key] = repeated
                }
                else { files[field_key] = [existing, upload] }
            }
            else { files[field_key] = upload }
        }
    }
    request["uploads"] = public_uploads
    request["files"] = files
    return request.omit(["_body_path", "_uploads"])
}

fn(http_save_upload(upload, destination)) {
    if(type(upload) != "object" || !upload.has("kind") || upload.kind != "upload" ||
       !upload.has("_upload_id") || !http_spool_paths.contains(upload._upload_id)) {
        return {"ok":false,"error":"invalid or expired upload handle","error_code":"invalid_upload","backend":"process"}
    }
    copy(http_spool_paths.get(upload._upload_id), destination)
    return {"ok":true,"error":"","error_code":"","backend":"process"}
}

fn(http_save_body(body, destination)) {
    if(type(body) != "object" || !body.has("kind") || body.kind != "spooled" ||
       !body.has("_body_id") || !http_spool_paths.contains(body._body_id)) {
        return {"ok":false,"error":"invalid or expired body handle","error_code":"invalid_body","backend":"process"}
    }
    copy(http_spool_paths.get(body._body_id), destination)
    return {"ok":true,"error":"","error_code":"","backend":"process"}
}

fn(http_write_worker_response(app)) {
    request := inject(getenv("NIFT_HTTP_REQUEST"))
    request = http_prepare_request(request)
    response := http_dispatch(app, request)
    envelope := {
        "protocol":1,
        "request_id":request.request_id,
        "status":response.status,
        "headers":response.headers,
        "body":response.body
    }
    if(response.has("cookies")) { envelope["cookies"] = response.cookies }
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
    if(type(app) == "object" && app.has("kind") && app.kind == "http_server_error") {
        return {"ok":false,"error":app.error,"error_code":app.error_code,"backend":app.backend,"exit_code":127}
    }
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
    client_timeout := http_config_value(config, "client_timeout_ms", 10000)
    response_timeout := http_config_value(config, "response_timeout_ms", 30000)
    max_request_line := http_config_value(config, "max_request_line", 8192)
    max_header_bytes := http_config_value(config, "max_header_bytes", 32768)
    max_headers := http_config_value(config, "max_headers", 100)
    max_body_bytes := http_config_value(config, "max_body_bytes", 1048576)
    max_form_fields := http_config_value(config, "max_form_fields", 256)
    max_form_name_bytes := http_config_value(config, "max_form_name_bytes", 256)
    max_form_value_bytes := http_config_value(config, "max_form_value_bytes", 65536)
    max_cookie_pairs := http_config_value(config, "max_cookie_pairs", 128)
    max_multipart_parts := http_config_value(config, "max_multipart_parts", 128)
    max_multipart_files := http_config_value(config, "max_multipart_files", 32)
    max_part_header_bytes := http_config_value(config, "max_part_header_bytes", 8192)
    max_part_headers := http_config_value(config, "max_part_headers", 32)
    max_file_bytes := http_config_value(config, "max_file_bytes", 1048576)
    max_filename_bytes := http_config_value(config, "max_filename_bytes", 255)
    max_temp_bytes := http_config_value(config, "max_temp_bytes", 2097152)
    max_file_response_bytes := http_config_value(config, "max_file_response_bytes", 67108864)
    backlog := http_config_value(config, "backlog", 16)
    result := run(
        http_python_path(), http_helper_path(),
        "--host", host,
        "--port", port.to_string(),
        "--nift", nift,
        "--app", cmd,
        "--cwd", pwd(),
        "--max-requests", max_requests.to_string(),
        "--worker-timeout-ms", worker_timeout.to_string(),
        "--client-timeout-ms", client_timeout.to_string(),
        "--response-timeout-ms", response_timeout.to_string(),
        "--max-request-line", max_request_line.to_string(),
        "--max-header-bytes", max_header_bytes.to_string(),
        "--max-headers", max_headers.to_string(),
        "--max-body-bytes", max_body_bytes.to_string(),
        "--max-form-fields", max_form_fields.to_string(),
        "--max-form-name-bytes", max_form_name_bytes.to_string(),
        "--max-form-value-bytes", max_form_value_bytes.to_string(),
        "--max-cookie-pairs", max_cookie_pairs.to_string(),
        "--max-multipart-parts", max_multipart_parts.to_string(),
        "--max-multipart-files", max_multipart_files.to_string(),
        "--max-part-header-bytes", max_part_header_bytes.to_string(),
        "--max-part-headers", max_part_headers.to_string(),
        "--max-file-bytes", max_file_bytes.to_string(),
        "--max-filename-bytes", max_filename_bytes.to_string(),
        "--max-temp-bytes", max_temp_bytes.to_string(),
        "--max-file-response-bytes", max_file_response_bytes.to_string(),
        "--backlog", backlog.to_string()
    )
    if(!result.launched) {
        return {"ok":false,"error":"failed to launch HTTP helper","error_code":"helper_launch","backend":"process","exit_code":result.exit_code}
    }
    if(result.exit_code != 0) {
        return {"ok":false,"error":result.stderr,"error_code":"helper_failed","backend":"process","exit_code":result.exit_code}
    }
    return {"ok":true,"error":"","error_code":"","backend":"process","exit_code":0}
}

fn(http_server_backend(app)) {
    if(!http_is_server(app)) { return null }
    return http_server_backends.get(app._server_id)
}

@struct(http_api) {
    available := () => http_available_now()
    backends := () => http_backend_names()
    backend := () => http_resolved_backend()
    use_backend := (name) => http_use_backend(name)
    capabilities := () => { return {
        "buffered_text":true,
        "json":true,
        "forms":true,
        "cookies":true,
        "binary_files":true,
        "spooled_bodies":true,
        "uploads":true,
        "multipart":true,
        "streaming":false,
        "websockets":false,
        "tls":false,
        "persistent_workers":false
    } }
    server := (config) => http_server(config)
    server_backend := (app) => http_server_backend(app)
    route := (app, method, path, handler) => http_add_route(app, method, path, handler)
    get := (app, path, handler) => http_add_route(app, "GET", path, handler)
    post := (app, path, handler) => http_add_route(app, "POST", path, handler)
    put := (app, path, handler) => http_add_route(app, "PUT", path, handler)
    patch := (app, path, handler) => http_add_route(app, "PATCH", path, handler)
    delete := (app, path, handler) => http_add_route(app, "DELETE", path, handler)
    head := (app, path, handler) => http_add_route(app, "HEAD", path, handler)
    text := (body, ...options) => http_text_response(body, options)
    json := (value, ...options) => http_json_response(value, options)
    cookie := (cookie_name, cookie_value, ...options) => http_cookie(cookie_name, cookie_value, options)
    save_upload := (upload, destination) => http_save_upload(upload, destination)
    save_body := (body, destination) => http_save_body(body, destination)
    file := (path, ...options) => http_file_response(path, options)
    file_from := (root, path, ...options) => http_root_file_response(root, path, options)
    listen := (app) => http_listen(app)
}

http := http_api()
export(http)
