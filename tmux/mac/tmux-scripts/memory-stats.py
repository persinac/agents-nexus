#!/usr/bin/env python3
"""memory-stats.py — single-shot health stats in JSON format.

Used by the pixel-dashboard bridge server to serve /api/memory/stats.
Outputs one JSON object to stdout, or {"error": "..."} on failure.
"""

import json
import os
import sys
from pathlib import Path

ENV_FILE = Path.home() / "garner/repos/agents-nexus/mnemon/.env"


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def db_url():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return None
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


def main():
    load_env()
    url = db_url()
    if not url:
        print(json.dumps({"error": "DATABASE_URL not set"}))
        return

    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        count(*) FILTER (WHERE timestamp > now() - interval '1 hour'),
                        count(*) FILTER (WHERE timestamp > now() - interval '24 hours')
                    FROM minions.memory_events
                """)
                ev_1h, ev_24h = cur.fetchone()

                cur.execute("""
                    SELECT
                        count(*),
                        count(*) FILTER (WHERE embedding IS NOT NULL)
                    FROM minions.memory_nodes
                """)
                notes_total, notes_embedded = cur.fetchone()

                cur.execute("""
                    SELECT timestamp, event_type, repo
                    FROM minions.memory_events
                    ORDER BY timestamp DESC LIMIT 1
                """)
                row = cur.fetchone()
                last_event = {"ts": str(row[0]), "type": row[1], "repo": row[2]} if row else None

                cur.execute("""
                    SELECT created_at, title, content
                    FROM minions.memory_nodes
                    ORDER BY created_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                last_note = (
                    {"ts": str(row[0]), "title": row[1], "content": row[2][:60]}
                    if row else None
                )

        print(json.dumps({
            "events_1h": int(ev_1h),
            "events_24h": int(ev_24h),
            "notes_total": int(notes_total),
            "notes_embedded": int(notes_embedded),
            "last_event": last_event,
            "last_note": last_note,
        }))
    except Exception as e:
        print(json.dumps({"error": str(e)[:120]}))


if __name__ == "__main__":
    main()
