#!/usr/bin/env python3
"""Render helper for the herdr memory-search panel. Reads memory-search.py's JSON array
on stdin and prints each note compactly. Kept as its own file (not an inline heredoc in
the panel script) so the piped JSON reaches stdin cleanly. argv[1] = the query, for the
empty-result message."""
import sys
import json

query = sys.argv[1] if len(sys.argv) > 1 else ""
raw = sys.stdin.read().strip()
try:
    notes = json.loads(raw) if raw else []
except Exception:
    notes = []

if not notes:
    print(f"  (no notes match '{query}')")
    sys.exit(0)

for n in notes:
    title = (n.get("title") or (n.get("content") or "")[:60] or "(untitled)").strip()
    proj = n.get("project") or ""
    tags = " ".join(f"#{t}" for t in (n.get("tags") or []))
    when = (n.get("created_at") or "")[:16]
    print(f"• {title}")
    meta = "   ".join(x for x in (f"[{proj}]" if proj else "", tags, when) if x)
    if meta:
        print(f"    {meta}")
    body = (n.get("content") or "").strip().replace("\n", " ")
    if body:
        print(f"    {body[:200]}{'…' if len(body) > 200 else ''}")
    print()
