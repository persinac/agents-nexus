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

echo "=== drag a note: position persists, empty coords re-anchor ==="
printf '  move             -> '; curl -s -o /dev/null -w '%{http_code} (expect 200)\n' -X POST \
  --data-urlencode 'x=612' --data-urlencode 'y=1840' "$B/doc/1/comment/1/pos"
curl -s "$B/doc/1" -o "$T/pm.html"
printf '  pos in cdata:    '; grab '"pos_x": 612' "$T/pm.html"
printf '  re-anchor        -> '; curl -s -o /dev/null -w '%{http_code} (expect 200)\n' -X POST \
  --data-urlencode 'x=' --data-urlencode 'y=' "$B/doc/1/comment/1/pos"
curl -s "$B/doc/1" -o "$T/pm2.html"
printf '  pos cleared:     '; rg -qF '"pos_x": 612' "$T/pm2.html" && echo STILL-PINNED || echo cleared
printf '  non-numeric      -> '; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST \
  --data-urlencode 'x=drop' --data-urlencode 'y=0' "$B/doc/1/comment/1/pos"
printf '  NaN              -> '; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST \
  --data-urlencode 'x=nan' --data-urlencode 'y=0' "$B/doc/1/comment/1/pos"
printf '  infinity         -> '; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST \
  --data-urlencode 'x=1e400' --data-urlencode 'y=0' "$B/doc/1/comment/1/pos"
printf '  unknown comment  -> '; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST \
  --data-urlencode 'x=1' --data-urlencode 'y=1' "$B/doc/1/comment/9999/pos"
printf '  wrong doc        -> '; curl -s -o /dev/null -w '%{http_code} (expect 400)\n' -X POST \
  --data-urlencode 'x=1' --data-urlencode 'y=1' "$B/doc/999/comment/1/pos"
printf '  cross-origin     -> '; curl -s -o /dev/null -w '%{http_code} (expect 403)\n' -X POST \
  -H 'Origin: https://evil.example' --data-urlencode 'x=1' --data-urlencode 'y=1' \
  "$B/doc/1/comment/1/pos"

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

echo "=== a pre-position vault migrates instead of 500ing ==="
kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null || true; SRV=""
sqlite3 "$T/index.db" "ALTER TABLE comments DROP COLUMN pos_x;
                       ALTER TABLE comments DROP COLUMN pos_y;"
printf '  columns gone:    '
sqlite3 "$T/index.db" "SELECT COUNT(*) FROM pragma_table_info('comments') WHERE name='pos_x';"
printf '  migrate says:    '; python3 docvault.py init | rg -o 'added board positions.*' || echo MISSING
python3 docvault.py serve --port 8361 >"$T/srv2.log" 2>&1 &
SRV=$!; sleep 3
printf '  doc page:        '; curl -s -o "$T/p4.html" -w '%{http_code} (expect 200)\n' "$B/doc/1"
printf '  notes intact:    '; grab 'notes \(\d+\)' "$T/p4.html"
printf '  pos back:        '; grab '"pos_x": null' "$T/p4.html"
