#!/bin/bash
set -euo pipefail
T=$(mktemp -d); W=$(mktemp -d)
cd "$(dirname "$0")/.."
export DOCVAULT_HOME="$T"
python3 docvault.py init >/dev/null

mk() { # $1=file $2=marker
  python3 - "$1" "$2" <<'PY'
import sys
p, marker = sys.argv[1], sys.argv[2]
body = "<p>" + (f"{marker} content line. " * 400) + "</p>"
open(p, "w").write(
  "<!doctype html><html><head><title>Versioning Probe Doc</title>"
  "<meta name='doc-theme' content='garner-doc/1'></head><body>"
  "<h1>Versioning Probe Doc</h1><p class='dek'>A probe.</p>" + body +
  "</body></html>")
PY
}

echo "=== v1: first deposit ==="
mk "$W/probe.html" "alpha"
python3 docvault.py put "$W/probe.html" --collection notes --tag probe | head -2

echo "=== same content again (expect duplicate, same id) ==="
python3 docvault.py put "$W/probe.html" --collection notes | head -1

echo "=== rewrite in place (expect versioned, SAME id) ==="
mk "$W/probe.html" "beta"
python3 docvault.py put "$W/probe.html" --collection notes | head -2

echo "=== rewrite again (expect v3) ==="
mk "$W/probe.html" "gamma"
python3 docvault.py put "$W/probe.html" --collection notes | head -1

echo "=== revert to v2 content (expect reverted, no new version) ==="
mk "$W/probe.html" "beta"
python3 docvault.py put "$W/probe.html" --collection notes | head -1

echo "=== same content at a DIFFERENT path (expect duplicate, same id) ==="
mk "$W/copy.html" "beta"
python3 docvault.py put "$W/copy.html" --collection notes | head -1

echo
echo "=== final state ==="
sqlite3 "$T/index.db" "SELECT 'docs: ' || COUNT(*) FROM docs;"
sqlite3 "$T/index.db" "SELECT 'doc id=' || id || '  version_n=' || version_n || '  title=' || title FROM docs;"
sqlite3 "$T/index.db" "SELECT 'version n=' || n || ' hash=' || SUBSTR(content_hash,1,8) FROM doc_versions ORDER BY n;"
sqlite3 "$T/index.db" "SELECT 'origins: ' || COUNT(*) FROM origins;"
sqlite3 "$T/index.db" "SELECT 'fts rows: ' || COUNT(*) FROM docs_fts;"
echo "--- vault files on disk (one per version) ---"
ls -1 "$T/docs" | sed 's/^/  /'
rm -rf "$T" "$W"
