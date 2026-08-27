#!/bin/bash
set -euo pipefail
T=$(mktemp -d); W=$(mktemp -d)
cd "$(dirname "$0")/.."
export DOCVAULT_HOME="$T"
SRV=""
cleanup() {
  [ -n "$SRV" ] && kill "$SRV" 2>/dev/null && wait "$SRV" 2>/dev/null
  rm -rf "$T" "$W"
  return 0
}
trap cleanup EXIT
python3 docvault.py init >/dev/null
python3 - "$W/probe.html" <<'PY'
import sys
open(sys.argv[1],"w").write(
  "<!doctype html><html><head><title>Comment Probe Doc</title></head><body>"
  "<h1>Comment Probe Doc</h1><p class='dek'>Probe.</p><p>" +
  ("The quick brown fox jumps over the lazy dog. " * 300) + "</p></body></html>")
PY
python3 docvault.py put "$W/probe.html" --collection notes >/dev/null
python3 docvault.py serve --port 8361 >"$T/srv.log" 2>&1 &
SRV=$!; sleep 3
B=http://127.0.0.1:8361

# Never let a missing match abort the run under `set -o pipefail` — an aborted
# run skips the trap's kill and leaks a server that poisons the next run's port.
grab() { local v; v=$(rg -o "$1" "$2" | head -1 || true); echo "${v:-MISSING}"; }

echo "=== doc page renders with comments UI ==="
curl -s "$B/doc/1" -o "$T/p.html"
for pat in 'id="board"' 'id="seltip"' 'id="cdata"' 'notes (0)' 'id="cadd"'; do
  printf '  %-28s ' "$pat"; rg -qF "$pat" "$T/p.html" && echo present || echo MISSING
done

echo "=== post a general comment ==="
curl -s -o /dev/null -w "  status %{http_code} (expect 303)\n" -X POST \
  --data-urlencode 'kind=general' --data-urlencode 'body=A general note.' "$B/doc/1/comment"

echo "=== post a highlight comment ==="
curl -s -o /dev/null -w "  status %{http_code} (expect 303)\n" -X POST \
  --data-urlencode 'kind=highlight' --data-urlencode 'body=Anchored note.' \
  --data-urlencode 'quote=quick brown fox' --data-urlencode 'prefix=The ' \
  --data-urlencode 'suffix=jumps' "$B/doc/1/comment"

echo "=== both render, with author + kind ==="
curl -s "$B/doc/1" -o "$T/p2.html"
printf '  count in toggle: '; grab 'notes \(\d+\)' "$T/p2.html"
printf '  general body:    '; rg -qF 'A general note.' "$T/p2.html" && echo present || echo MISSING
printf '  json fetch reply:'; curl -s -H 'X-Requested-With: fetch' -X POST --data-urlencode 'kind=general' --data-urlencode 'body=via fetch' "$B/doc/1/comment" | rg -qF '"author"' && echo ' json ok' || echo ' MISSING'
printf '  highlight quote: '; rg -qF 'quick brown fox' "$T/p2.html" && echo present || echo MISSING
printf '  author in cdata: '; rg -qF '"author": "local"' "$T/p2.html" && echo present || echo MISSING

echo "=== rejections ==="
printf '  empty body       -> '; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST --data-urlencode 'kind=general' --data-urlencode 'body=   ' "$B/doc/1/comment"
printf '  bad kind         -> '; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST --data-urlencode 'kind=evil' --data-urlencode 'body=x' "$B/doc/1/comment"
printf '  unknown doc      -> '; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST --data-urlencode 'kind=general' --data-urlencode 'body=x' "$B/doc/999/comment"
printf '  bad endpoint     -> '; curl -s -o /dev/null -w '%{http_code} (expect 404)\n' -X POST --data-urlencode 'body=x' "$B/doc/1/evil"
printf '  cross-origin     -> '; curl -s -o /dev/null -w '%{http_code} (expect 403)\n' -X POST -H 'Origin: https://evil.example' --data-urlencode 'kind=general' --data-urlencode 'body=x' "$B/doc/1/comment"
printf '  same-origin ok   -> '; curl -s -o /dev/null -w '%{http_code} (expect 303)\n' -X POST -H "Origin: http://127.0.0.1:8361" --data-urlencode 'kind=general' --data-urlencode 'body=same origin ok' "$B/doc/1/comment"
printf '  oversized body   -> '; python3 -c "print('x'*40000)" > "$T/big.txt"; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST --data-urlencode 'kind=general' --data-urlencode "body@$T/big.txt" "$B/doc/1/comment"

echo "=== comments survive a new version, tagged with the version they were made on ==="
python3 - "$W/probe.html" <<'PY'
import sys
open(sys.argv[1],"w").write(
  "<!doctype html><html><head><title>Comment Probe Doc</title></head><body>"
  "<h1>Comment Probe Doc</h1><p class='dek'>Probe v2.</p><p>" +
  ("Entirely different second version text. " * 300) + "</p></body></html>")
PY
python3 docvault.py put "$W/probe.html" --collection notes | head -1
curl -s "$B/doc/1" -o "$T/p3.html"
printf '  version tag:     '; grab 'v2 of 2' "$T/p3.html"
printf '  board on v2:     '; grab 'data-version="2"' "$T/p3.html"
printf '  notes still on v1: '; grab '"version_n": 1' "$T/p3.html"
printf '  comment count:   '; grab 'notes \(\d+\)' "$T/p3.html"
