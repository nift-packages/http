#include <stdint.h>
#include <string.h>

/* SERVER3-PERF native HTTP request-line/header parser.
 * Parses a raw request head (request line + headers, up to CRLFCRLF) into a
 * length-prefixed structure that Nift decodes.
 *
 * Output layout (all little-endian):
 *   i32 status            (200 ok; 400/414/431/501 on rejection)
 *   i32 method_len, method bytes
 *   i32 target_len, target bytes
 *   i32 version_len, version bytes
 *   i32 header_count
 *   for each header: i32 name_len, name bytes, i32 value_len, value bytes
 *   i64 content_length
 *
 * Returns bytes written, or -1 if out_size is too small.
 */

static int is_token_char(unsigned char c) {
    if (c >= 'a' && c <= 'z') return 1;
    if (c >= 'A' && c <= 'Z') return 1;
    if (c >= '0' && c <= '9') return 1;
    switch (c) {
        case '!': case '#': case '$': case '%': case '&': case '\'':
        case '*': case '+': case '-': case '.': case '^': case '_':
        case '`': case '|': case '~':
            return 1;
        default:
            return 0;
    }
}

static int valid_utf8(const unsigned char* s, int n) {
    int i = 0;
    while (i < n) {
        unsigned char c = s[i];
        if (c < 0x80) { i += 1; }
        else if (c >= 0xC2 && c <= 0xDF) {
            if (i + 1 >= n || (s[i+1] & 0xC0) != 0x80) return 0;
            i += 2;
        } else if (c >= 0xE0 && c <= 0xEF) {
            if (i + 2 >= n || (s[i+1] & 0xC0) != 0x80 || (s[i+2] & 0xC0) != 0x80) return 0;
            i += 3;
        } else if (c >= 0xF0 && c <= 0xF4) {
            if (i + 3 >= n || (s[i+1] & 0xC0) != 0x80 || (s[i+2] & 0xC0) != 0x80 || (s[i+3] & 0xC0) != 0x80) return 0;
            i += 4;
        } else return 0;
    }
    return 1;
}

static void put_u32(unsigned char* out, int* o, uint32_t v) {
    out[(*o)++] = v & 0xFF; out[(*o)++] = (v >> 8) & 0xFF;
    out[(*o)++] = (v >> 16) & 0xFF; out[(*o)++] = (v >> 24) & 0xFF;
}

static void put_u64(unsigned char* out, int* o, uint64_t v) {
    for (int k = 0; k < 8; k++) { out[(*o)++] = (v >> (8*k)) & 0xFF; }
}

static int fail(unsigned char* out, int* o, int status) {
    *o = 0;
    put_u32(out, o, (uint32_t)status);
    put_u32(out, o, 0);
    put_u32(out, o, 0);
    put_u32(out, o, 0);
    put_u32(out, o, 0);
    put_u64(out, o, 0);
    return *o;
}

static int ensure(unsigned char* out, int* o, int out_size, int need) {
    if (*o + need > out_size) { return fail(out, o, 431); }
    return 0;
}

int http_parse(const unsigned char* data, int len, unsigned char* out, int out_size) {
    int o = 0;
    /* find CRLFCRLF terminator */
    int head_end = -1;
    for (int i = 0; i + 3 < len; i++) {
        if (data[i] == 13 && data[i+1] == 10 && data[i+2] == 13 && data[i+3] == 10) {
            head_end = i;
            break;
        }
    }
    if (head_end < 0) { return fail(out, &o, 400); }
    if (!valid_utf8(data, head_end)) { return fail(out, &o, 400); }

    /* request line ends at the first CRLF */
    int rl_end = -1;
    for (int i = 0; i < head_end; i++) {
        if (data[i] == 13 && i + 1 < head_end && data[i+1] == 10) { rl_end = i; break; }
    }
    if (rl_end <= 0) { return fail(out, &o, 400); }
    int sp1 = -1, sp2 = -1;
    for (int i = 0; i < rl_end; i++) {
        if (data[i] == 32) { sp1 = i; break; }
    }
    if (sp1 <= 0) { return fail(out, &o, 400); }
    for (int i = sp1 + 1; i < rl_end; i++) {
        if (data[i] == 32) { sp2 = i; break; }
    }
    if (sp2 <= sp1 + 1) { return fail(out, &o, 400); }
    /* no extra spaces after sp2 */
    for (int i = sp2 + 1; i < rl_end; i++) {
        if (data[i] == 32) { return fail(out, &o, 400); }
    }
    int method_len = sp1;
    int target_len = sp2 - sp1 - 1;
    int version_len = rl_end - sp2 - 1;
    for (int i = 0; i < method_len; i++) if (!is_token_char(data[i])) { return fail(out, &o, 400); }
    if (version_len != 8 || memcmp(data + sp2 + 1, "HTTP/1.1", 8) != 0) { return fail(out, &o, 400); }
    if (data[sp1+1] != '/') { return fail(out, &o, 400); }
    for (int i = sp1 + 1; i < sp2; i++) {
        unsigned char c = data[i];
        if (c < 32 || c == 127) { return fail(out, &o, 400); }
    }
    for (int i = sp1 + 1; i < sp2; i++) if (data[i] == '#') { return fail(out, &o, 400); }

    if (ensure(out, &o, out_size, 12 + method_len + target_len + version_len)) return o;
    put_u32(out, &o, 200);
    put_u32(out, &o, method_len); memcpy(out + o, data, method_len); o += method_len;
    put_u32(out, &o, target_len); memcpy(out + o, data + sp1 + 1, target_len); o += target_len;
    put_u32(out, &o, version_len); memcpy(out + o, data + sp2 + 1, version_len); o += version_len;

    /* headers: from rl_end+2 (past request-line CRLF) up to head_end */
    int hp = rl_end + 2;
    int hcount = 0;
    int first_header = o;
    put_u32(out, &o, 0); /* placeholder */
    long long content_length = -1;
    int host_count = 0;
    while (hp < head_end) {
        int line_start = hp;
        int line_end = hp;
        while (line_end < head_end && data[line_end] != 13) line_end++;
        int line_len = line_end - line_start;
        if (line_len == 0) { hp = line_end + 2; break; }
        if (data[line_start] == 32 || data[line_start] == 9) { return fail(out, &o, 400); }
        int colon = -1;
        for (int i = line_start; i < line_end; i++) if (data[i] == ':') { colon = i; break; }
        if (colon <= line_start) { return fail(out, &o, 400); }
        int name_len = colon - line_start;
        for (int i = 0; i < name_len; i++) if (!is_token_char(data[line_start + i])) { return fail(out, &o, 400); }
        int vs = colon + 1;
        int ve = line_end;
        while (vs < ve && (data[vs] == 32 || data[vs] == 9)) vs++;
        while (ve > vs && (data[ve-1] == 32 || data[ve-1] == 9)) ve--;
        int value_len = ve - vs;
        for (int i = vs; i < ve; i++) {
            unsigned char c = data[i];
            if ((c < 32 && c != 9) || c == 127) { return fail(out, &o, 400); }
        }
        if (!valid_utf8(data + vs, value_len)) { return fail(out, &o, 400); }
        if (ensure(out, &o, out_size, 8 + name_len + value_len)) return o;
        put_u32(out, &o, name_len); memcpy(out + o, data + line_start, name_len); o += name_len;
        put_u32(out, &o, value_len); memcpy(out + o, data + vs, value_len); o += value_len;
        /* lowercase name for the key */
        for (int i = o - name_len - value_len - 4 - 4 + 4; i < o - value_len - 4; i++) {
            if (out[i] >= 'A' && out[i] <= 'Z') out[i] += 32;
        }
        int is_cl = (name_len == 14) && (memcmp(data + line_start, "content-length", 14) == 0 || memcmp(data + line_start, "Content-Length", 14) == 0);
        int is_host = (name_len == 4) && (memcmp(data + line_start, "host", 4) == 0 || memcmp(data + line_start, "Host", 4) == 0);
        int is_te = (name_len == 17) && (memcmp(data + line_start, "transfer-encoding", 17) == 0 || memcmp(data + line_start, "Transfer-Encoding", 17) == 0);
        if (is_cl) {
            if (value_len == 0 || content_length != -1) { return fail(out, &o, 400); }
            long long v = 0;
            for (int i = vs; i < ve; i++) {
                if (data[i] < '0' || data[i] > '9') { return fail(out, &o, 400); }
                v = v * 10 + (data[i] - '0');
                if (v > 1000000000LL) { return fail(out, &o, 413); }
            }
            content_length = v;
        }
        if (is_host) host_count++;
        if (is_te) { return fail(out, &o, 501); }
        hcount++;
        hp = line_end + 2;
    }
    if (host_count != 1) { return fail(out, &o, 400); }
    /* fix header count */
    unsigned char* pn = out + first_header;
    pn[0] = hcount & 0xFF; pn[1] = (hcount >> 8) & 0xFF; pn[2] = (hcount >> 16) & 0xFF; pn[3] = (hcount >> 24) & 0xFF;
    put_u64(out, &o, (uint64_t)(content_length < 0 ? 0 : content_length));
    return o;
}