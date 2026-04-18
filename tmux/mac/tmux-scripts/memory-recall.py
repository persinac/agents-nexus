#!/usr/bin/env python3
"""memory-recall.py — fetch recent memory notes at agent session start.

Called by open-claude.sh to inject prior project knowledge into the startup prompt.
Queries memory_nodes directly (no MCP server needed — runs before Claude starts).

Usage:
  memory-recall.py <project> [--max-tokens N]

Outputs a markdown "## Prior Knowledge" section, or nothing if no notes exist
or the DB is unreachable (fail-open — never blocks Claude from starting).
"""

import os
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

ENV_FILE = Path(os.environ.get("AGENTS_NEXUS_DIR", Path.home() / "repos/agents-nexus")) / "mnemon/.env"
DEFAULT_MAX_TOKENS = 2000
CHARS_PER_TOKEN = 4  # conservative estimate


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def db_url() -> str | None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return None
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


def age_label(ts_str: str) -> str:
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - ts).total_seconds())
        if secs < 3600:
            return f"{secs // 60}m ago"
        elif secs < 86400:
            return f"{secs // 3600}h ago"
        else:
            return f"{secs // 86400}d ago"
    except Exception:
        return ""


def fetch_notes(project: str, limit: int = 15) -> list[dict]:
    url = db_url()
    if not url:
        return []
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                if project == "general":
                    # Cross-project: recent notes from any project, include project label.
                    cur.execute(
                        """
                        SELECT title, content, tags, created_at, access_count, project
                        FROM minions.memory_nodes
                        ORDER BY
                            (COALESCE(last_accessed, created_at)) DESC,
                            access_count DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                else:
                    # Project-scoped: blend recency and access frequency.
                    cur.execute(
                        """
                        SELECT title, content, tags, created_at, access_count, NULL
                        FROM minions.memory_nodes
                        WHERE project = %s
                        ORDER BY
                            (COALESCE(last_accessed, created_at)) DESC,
                            access_count DESC
                        LIMIT %s
                        """,
                        (project, limit),
                    )
                rows = cur.fetchall()
    except Exception:
        return []

    notes = []
    for title, content, tags, created_at, access_count, proj in rows:
        notes.append({
            "title": title or "",
            "content": content,
            "tags": list(tags) if tags else [],
            "created_at": str(created_at) if created_at else "",
            "access_count": access_count or 0,
            "project": proj or "",
        })
    return notes


def format_notes(notes: list[dict], max_tokens: int, is_general: bool = False) -> str:
    if not notes:
        return ""

    max_chars = max_tokens * CHARS_PER_TOKEN
    sections = []
    used = 0

    for note in notes:
        title = note["title"] or note["content"][:50]
        proj_str = f"  [{note['project']}]" if note.get("project") else ""
        tags_str = "  " + " ".join(f"#{t}" for t in note["tags"]) if note["tags"] else ""
        age = age_label(note["created_at"])
        age_str = f"  ({age})" if age else ""
        header = f"**{title}**{proj_str}{tags_str}{age_str}"
        body = note["content"].strip()
        block = f"{header}\n{body}"

        cost = len(block)
        if used + cost > max_chars:
            break
        sections.append(block)
        used += cost

    if not sections:
        return ""

    scope = "across all projects" if is_general else "about this project"
    joined = "\n\n---\n".join(sections)
    return (
        "## Prior Knowledge (from memory store)\n\n"
        f"The following notes were previously recorded {scope}:\n\n"
        f"{joined}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", help="Project name (basename of repo dir)")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args()

    load_env()
    notes = fetch_notes(args.project)

    if args.format == "json":
        import json
        print(json.dumps(notes))
        return

    output = format_notes(notes, args.max_tokens, is_general=(args.project == "general"))
    if output:
        print(output)


if __name__ == "__main__":
    main()
