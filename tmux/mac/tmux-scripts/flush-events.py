#!/usr/bin/env python3
"""Drain ~/.tmux/memory-events.jsonl into Postgres.

Run every 2 minutes via launchd. Uses an atomic rename so events written
while flushing are not lost — they land in the next flush cycle.

Requires: psycopg (from the agent-memory venv, called via flush-events.sh)
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path


def _db_url() -> str:
    # DATABASE_URL lives in the repo-root .env (work machine -> local docker
    # PG; personal machine -> cloud PG). Each machine's .env is gitignored
    # and configured at setup time per README_SETUP_{WORK,PERSONAL}.md.
    # Fall back to <mnemon>/.env for backward-compat with old setups.
    nexus_dir = Path(os.environ.get("AGENTS_NEXUS_DIR", Path.home() / "repos/agents-nexus"))
    mnemon_dir = Path(os.getenv("AGENT_MEMORY_DIR", nexus_dir / "mnemon"))
    for env_file in (nexus_dir / ".env", mnemon_dir / ".env"):
        if not env_file.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
        except ImportError:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
        break  # first hit wins

    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
    if not url:
        return ""
    # psycopg doesn't support search_path as a URI param — strip it
    import re
    url = re.sub(r'[&?]search_path=[^&]*', '', url)
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


def main() -> int:
    tmux_home = os.getenv("TMUX_HOME", str(Path.home() / ".tmux"))
    buffer = Path(tmux_home) / "memory-events.jsonl"
    if not buffer.exists() or buffer.stat().st_size == 0:
        return 0

    # Atomic rename — new events during flush land in the original path
    flushing = buffer.with_suffix(".jsonl.flushing")
    buffer.rename(flushing)

    events: list[dict] = []
    for line in flushing.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    if not events:
        flushing.unlink(missing_ok=True)
        return 0

    url = _db_url()
    if not url:
        # No DB configured — put events back so they're not lost
        with buffer.open("a") as f:
            f.write(flushing.read_text())
        flushing.unlink(missing_ok=True)
        print(f"[memory-flush] no DATABASE_URL configured — {len(events)} event(s) returned to buffer", file=sys.stderr)
        return 1

    try:
        import psycopg
        with psycopg.connect(url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path TO agents, public")
                for ev in events:
                    cur.execute(
                        """
                        INSERT INTO agents.memory_events
                            (id, project, event_type, device, repo, branch,
                             agent_slot, session_id, payload)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            uuid.uuid4().hex[:12],
                            ev.get("project", ""),
                            ev.get("event_type", "unknown"),
                            ev.get("device", ""),
                            ev.get("repo", ""),
                            ev.get("branch", ""),
                            ev.get("agent_slot", ""),
                            ev.get("session_id") or None,
                            json.dumps(ev.get("payload", {})),
                        ),
                    )
        print(f"[memory-flush] flushed {len(events)} event(s)")
        flushing.unlink(missing_ok=True)
        return 0

    except Exception as e:
        # Put events back so the next flush cycle retries
        with buffer.open("a") as f:
            f.write(flushing.read_text())
        flushing.unlink(missing_ok=True)
        print(f"[memory-flush] error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
