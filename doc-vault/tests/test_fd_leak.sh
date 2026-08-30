#!/bin/bash
# Guards the 2026-08-28 outage: one leaked db fd per request hit launchd's 256 cap.
set -euo pipefail
T=$(mktemp -d); W=$(mktemp -d)
cd "$(dirname "$0")/.."
export DOCVAULT_HOME="$T"
PORT=8399
REQS=${REQS:-150}

python3 docvault.py init >/dev/null
python3 tests/mkdoc.py "$W/probe.html" "FD Leak Probe Doc"
python3 docvault.py put "$W/probe.html" --collection notes --tag probe >/dev/null

python3 docvault.py serve --port "$PORT" >"$T/serve.log" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

for i in $(seq 1 40); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null && break
  sleep 0.25
done

fds() { lsof -nP -p "$SRV" 2>/dev/null | awk 'NR>1 && $4 ~ /^[0-9]+[rwu]/ {n++} END {print n+0}'; }
dbfds() { lsof -nP -p "$SRV" 2>/dev/null | grep -c "$T/index.db" || true; }

BEFORE=$(fds)
echo "fds after warmup:        $BEFORE  (index.db handles: $(dbfds))"

for i in $(seq 1 "$REQS"); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" || true
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/api/docs" || true
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/doc/1" || true
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/search?q=probe" || true
done

sleep 1
AFTER=$(fds); AFTERDB=$(dbfds)
echo "fds after $((REQS * 4)) requests: $AFTER  (index.db handles: $AFTERDB)"

GROWTH=$((AFTER - BEFORE))
echo "growth: $GROWTH"

if [ "$AFTERDB" -gt 2 ]; then
  echo "FAIL: $AFTERDB open index.db handles after $((REQS * 4)) requests (want <= 2)"
  exit 1
fi
if [ "$GROWTH" -gt 10 ]; then
  echo "FAIL: fd count grew by $GROWTH over $((REQS * 4)) requests (want <= 10)"
  exit 1
fi

echo "PASS: no fd leak ($GROWTH fd growth, $AFTERDB db handles)"
