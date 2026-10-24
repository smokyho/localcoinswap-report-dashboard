#!/usr/bin/env bash
# Refresh the vendored OpenAPI spec.
#
# The docs page at https://api.localcoinswap.com/api-docs/ is an Angular SPA,
# so fetching it returns only the <title>. The real spec is served by the
# drf-yasg backend at either of these paths.
#
# Note: the spec disagrees with the live API in several important ways. Read
# api-behaviour.md before trusting anything in here.
set -euo pipefail

OUT="$(cd "$(dirname "$0")" && pwd)/lcs-openapi.json"
URL="https://api.localcoinswap.com/swagger.json"

curl -fsSL "$URL" -o "$OUT.tmp"
python3 - "$OUT.tmp" "$OUT" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
data = json.load(open(src))
paths = len(data.get("paths", {}))
defs = len(data.get("definitions", {}))
if paths < 50:
    raise SystemExit(f"only {paths} paths -- that is not the full spec, aborting")
json.dump(data, open(dst, "w"), indent=1)
print(f"wrote {dst}: {paths} paths, {defs} definitions")
PY
rm -f "$OUT.tmp"
