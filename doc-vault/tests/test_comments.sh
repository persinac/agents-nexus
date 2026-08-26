#!/bin/bash
set -euo pipefail
T=$(mktemp -d); W=$(mktemp -d)
cd "$(dirname "$0")/.."
export DOCVAULT_HOME="$T"
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

echo "=== doc page renders with comments UI ==="
curl -s "$B/doc/1" -o "$T/p.html"
for pat in 'id="cpane"' 'id="cform"' 'action="/doc/1/comment"' 'comments (0)'; do
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
printf '  count in toggle: '; rg -o 'comments \(\d+\)' "$T/p2.html" | head -1
printf '  general body:    '; rg -qF 'A general note.' "$T/p2.html" && echo present || echo MISSING
printf '  highlight quote: '; rg -qF 'quick brown fox' "$T/p2.html" && echo present || echo MISSING
printf '  author:          '; rg -o 'local · [0-9-]{10}' "$T/p2.html" | head -1

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
printf '  version tag:     '; rg -o 'v2 of 2' "$T/p3.html" | head -1
printf '  old comments marked on v1: '; rg -c 'on v1' "$T/p3.html" || echo 0
printf '  comment count:   '; rg -o 'comments \(\d+\)' "$T/p3.html" | head -1

kill $SRV 2>/dev/null || true; wait $SRV 2>/dev/null || true
rm -rf "$T" "$W"
