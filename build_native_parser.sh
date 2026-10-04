#!/usr/bin/env bash
# Build the optional native request/header parser shared library.
# Produces libnative_parse.so (Linux) / .dylib (macOS) in the package dir.
# The http native backend uses it when the server config sets
# "native_parser_lib" to the built library's path; otherwise it falls back to
# the interpreted Nift parser. The public API stays in Nift package-land.
set -euo pipefail
cd "$(dirname "$0")"
out="libnative_parse"
if [ "$(uname -s)" = "Darwin" ]; then
    cc -shared -O2 -fPIC -o "${out}.dylib" src/native_parse.c
else
    cc -shared -O2 -fPIC -o "${out}.so" src/native_parse.c
fi
echo "built $(ls -1 ${out}.*)"