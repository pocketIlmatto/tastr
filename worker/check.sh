#!/usr/bin/env bash
# Build the Worker and check it the way CI does (.github/workflows/test.yml, job "worker"):
#   1. build.py copies the app in, and `pywrangler deploy --dry-run` bundles it with its Pyodide packages;
#   2. the bundle must stay under Cloudflare's limit (10 MiB gzipped on Workers Paid);
#   3. with --serve, `pywrangler dev` runs it in local mode and a few pages must answer.
# Run it with the app's own Python environment active (build.py imports tastr.web.server).
set -euo pipefail
cd "$(dirname "$0")"
LIMIT_KIB=10240
PORT=${PORT:-8799}

python build.py
[ -d node_modules ] || npm install --no-audit --no-fund --silent
out=$(uv run pywrangler deploy --dry-run --outdir .dry 2>&1) || { echo "$out"; exit 1; }
line=$(echo "$out" | sed 's/\x1b\[[0-9;]*m//g' | grep "Total Upload")
echo "$line"
gzip_kib=$(echo "$line" | sed -E 's/.*gzip: ([0-9.]+) KiB.*/\1/')
if awk "BEGIN {exit !($gzip_kib > $LIMIT_KIB)}"; then
  echo "The Worker is ${gzip_kib} KiB gzipped, over the ${LIMIT_KIB} KiB limit." >&2
  exit 1
fi

[ "${1:-}" = "--serve" ] || exit 0
log=$(mktemp)
uv run pywrangler dev --port "$PORT" --var TASTR_HOSTED:0 --persist-to "$(mktemp -d)" > "$log" 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null; pkill -P $pid 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
  grep -q "Ready on" "$log" && break
  kill -0 $pid 2>/dev/null || { cat "$log"; exit 1; }
  sleep 2
done
fail=0
for path in /api/health /api/config / /api/tracks /static/style.css; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "localhost:$PORT$path")
  echo "$code $path"
  [ "$code" = 200 ] || fail=1
done
[ $fail = 0 ] || { sed 's/\x1b\[[0-9;]*m//g' "$log" | tail -40; exit 1; }
