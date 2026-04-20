#!/usr/bin/env python3
"""Quick check of the memory system state in Postgres."""
import os
from pathlib import Path

# Load .env
for line in Path(__file__).parent.joinpath(".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

url = os.environ.get("DATABASE_URL", "")
if not url:
    print("DATABASE_URL not set")
    raise SystemExit(1)

# Strip search_path from URL — psycopg doesn't support it as a query param
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
parsed = urlparse(url)
params = parse_qs(parsed.query)
params.pop("search_path", None)
url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

import psycopg
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT count(*) FILTER (WHERE timestamp > now() - interval '1 hour'),
               count(*) FILTER (WHERE timestamp > now() - interval '24 hours'),
               count(*)
        FROM agents.memory_events
    """)
    ev_1h, ev_24h, ev_total = cur.fetchone()

    cur.execute("SELECT count(*) FROM agents.memory_nodes")
    notes = cur.fetchone()[0]

    print(f"Events:  {ev_1h} (1h) / {ev_24h} (24h) / {ev_total} total")
    print(f"Notes:   {notes}")

    if ev_total > 0:
        cur.execute("""
            SELECT event_type, project, device, agent_slot, session_id, timestamp
            FROM agents.memory_events ORDER BY timestamp DESC LIMIT 10
        """)
        print("\nRecent events:")
        for row in cur.fetchall():
            etype, proj, dev, slot, sid, ts = row
            print(f"  {ts}  {etype:<20} proj={proj or '-'}  device={dev or '-'}  slot={slot or '-'}")

    if notes > 0:
        cur.execute("""
            SELECT title, tags, project, created_at
            FROM agents.memory_nodes ORDER BY created_at DESC LIMIT 10
        """)
        print("\nRecent notes:")
        for row in cur.fetchall():
            title, tags, proj, ts = row
            print(f"  {ts}  {title or '(untitled)'}  tags={tags}  proj={proj}")
