#!/usr/bin/env python3
"""memory-search.py — keyword search over memory notes.

Fast, embedding-free counterpart to the semantic search in
`agent_memory.cli search`. Queries agents.memory_nodes directly via
DATABASE_URL (parameterized — never interpolates user input into SQL).

Usage:
  memory-search.py --query <text> [--project NAME|all] [--limit N] [--format json]

Outputs a JSON array (default) of matching notes, or nothing on error
(fail-open). Matches content/title (case-insensitive substring) or an exact
tag match.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

ENV_FILE = Path(os.environ.get("AGENTS_NEXUS_DIR", Path.home() / "repos/agents-nexus")) / ".env"


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def db_url() -> str | None:
    import re
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return None
    url = re.sub(r'[&?]search_path=[^&]*', '', url)
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


def search_notes(query: str, project: str, limit: int) -> list[dict]:
    url = db_url()
    if not url:
        return []
    like = f"%{query}%"
    all_projects = project == "" or project.lower() == "all"
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                if all_projects:
                    cur.execute(
                        """
                        SELECT id, title, content, tags, created_at, project
                        FROM agents.memory_nodes
                        WHERE content ILIKE %s OR title ILIKE %s OR %s = ANY(tags)
                        ORDER BY (COALESCE(last_accessed, created_at)) DESC
                        LIMIT %s
                        """,
                        (like, like, query, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, title, content, tags, created_at, project
                        FROM agents.memory_nodes
                        WHERE project = %s
                          AND (content ILIKE %s OR title ILIKE %s OR %s = ANY(tags))
                        ORDER BY (COALESCE(last_accessed, created_at)) DESC
                        LIMIT %s
                        """,
                        (project, like, like, query, limit),
                    )
                rows = cur.fetchall()
    except Exception:
        return []

    return [
        {
            "id": r[0],
            "title": r[1] or "",
            "content": r[2],
            "tags": list(r[3]) if r[3] else [],
            "created_at": str(r[4]) if r[4] else "",
            "project": r[5] or "",
        }
        for r in rows
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--project", default="all")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--format", choices=["json"], default="json")
    args = parser.parse_args()

    load_env()
    notes = search_notes(args.query, args.project, args.limit)
    print(json.dumps(notes))


if __name__ == "__main__":
    main()
