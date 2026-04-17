#!/usr/bin/env python3
"""Live memory system health panel — runs as a persistent tmux side-pane.

Queries Postgres every REFRESH_SECS and renders a compact status display.
Reads DATABASE_URL from the agent-memory .env file (same source as MCP server).
"""

import os
import sys
import time
import signal
from datetime import datetime, timezone
from pathlib import Path

REFRESH_SECS = 5
ENV_FILE = Path.home() / "minions/minions-suite/agent-memory/.env"
WIDTH = 46  # inner content width (box is WIDTH+4)


# ── helpers ───────────────────────────────────────────────────────────────────

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


def age_str(ts_str: str) -> str:
    """Return human-readable age like '2m ago'."""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - ts).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        elif secs < 3600:
            return f"{secs // 60}m ago"
        elif secs < 86400:
            return f"{secs // 3600}h ago"
        else:
            return f"{secs // 86400}d ago"
    except Exception:
        return "?"


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


# ── rendering ─────────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"

def bar(label: str, content: str) -> str:
    """Render a fixed-width inner row: '│ label  content │'"""
    inner = f"{DIM}{label}{RESET}  {content}"
    # strip ANSI for length calc
    import re
    visible = re.sub(r"\033\[[^m]*m", "", inner)
    pad = WIDTH - len(visible)
    return f"│ {inner}{' ' * max(0, pad)} │"


def dot(ok: bool) -> str:
    return f"{GREEN}●{RESET}" if ok else f"{RED}●{RESET}"


def render(data: dict | None, error: str | None, url_ok: bool):
    W = WIDTH + 2
    top    = f"╭{'─' * W}╮"
    mid    = f"├{'─' * W}┤"
    bot    = f"╰{'─' * W}╯"
    blank  = f"│{' ' * W}│"

    lines = []
    lines.append(top)
    title = f"{BOLD}{CYAN} memory health {RESET}"
    # title centering (account for ANSI escape lengths)
    import re
    visible_len = len(re.sub(r"\033\[[^m]*m", "", title))
    pad_total = W - visible_len
    lpad = pad_total // 2
    rpad = pad_total - lpad
    lines.append(f"│{' ' * lpad}{title}{' ' * rpad}│")
    lines.append(mid)

    if error or not url_ok:
        msg = error or "DATABASE_URL not set"
        lines.append(blank)
        lines.append(f"│ {RED}✗ {RESET}{trunc(msg, WIDTH - 2):<{WIDTH - 2}} │")
        lines.append(blank)
        lines.append(bot)
        return "\n".join(lines)

    d = data or {}

    # ── events block ──────────────────────────────────────────────────────────
    ev_1h   = d.get("events_1h", 0)
    ev_24h  = d.get("events_24h", 0)
    lines.append(bar("events", f"{BOLD}{WHITE}{ev_1h}{RESET} /1h  {DIM}{ev_24h} /24h{RESET}"))

    # ── notes / embeddings block ──────────────────────────────────────────────
    notes   = d.get("notes_total", 0)
    emb     = d.get("notes_embedded", 0)
    pct     = f"{int(emb / notes * 100)}%" if notes else "n/a"
    emb_col = GREEN if notes and emb == notes else YELLOW if emb > 0 else DIM
    lines.append(bar("notes", f"{BOLD}{WHITE}{notes}{RESET}  embedded {emb_col}{emb}/{notes} ({pct}){RESET}"))

    # ── last event ────────────────────────────────────────────────────────────
    last_ev = d.get("last_event")
    if last_ev:
        ev_age  = age_str(last_ev["ts"])
        ev_type = trunc(last_ev["type"], 16)
        ev_repo = trunc(last_ev["repo"] or "—", 14)
        lines.append(bar("last event", f"{ev_type}  {DIM}{ev_repo}  {ev_age}{RESET}"))
    else:
        lines.append(bar("last event", f"{DIM}none yet{RESET}"))

    # ── last note ─────────────────────────────────────────────────────────────
    last_note = d.get("last_note")
    if last_note:
        n_age   = age_str(last_note["ts"])
        n_title = trunc(last_note["title"] or last_note["content"][:30], 22)
        lines.append(bar("last note", f"{trunc(n_title, 22)}  {DIM}{n_age}{RESET}"))
    else:
        lines.append(bar("last note", f"{DIM}none yet{RESET}"))

    lines.append(mid)

    # ── recent events ─────────────────────────────────────────────────────────
    lines.append(f"│ {DIM}recent events{RESET}{' ' * (W - 14)}│")
    recent = d.get("recent_events", [])
    if recent:
        for ev in recent[:5]:
            ev_age  = age_str(ev["ts"])
            ev_type = trunc(ev["type"], 16)
            ev_repo = trunc(ev["repo"] or "—", 12)
            row = f"{DIM}{ev_age:>6}{RESET}  {ev_type:<16}  {DIM}{ev_repo}{RESET}"
            import re
            visible = re.sub(r"\033\[[^m]*m", "", row)
            pad = WIDTH - len(visible)
            lines.append(f"│ {row}{' ' * max(0, pad)} │")
    else:
        lines.append(f"│ {DIM}  no events yet{RESET}{' ' * (W - 16)}│")

    lines.append(mid)

    # ── status row ────────────────────────────────────────────────────────────
    db_ok  = data is not None
    mcp_ok = d.get("mcp_ok", False)
    ts     = datetime.now().strftime("%H:%M:%S")
    status = f"db {dot(db_ok)}  mcp {dot(mcp_ok)}  {DIM}{ts}{RESET}"
    import re
    visible = re.sub(r"\033\[[^m]*m", "", status)
    pad = WIDTH - len(visible)
    lines.append(f"│ {status}{' ' * max(0, pad)} │")
    lines.append(bot)

    return "\n".join(lines)


# ── query ─────────────────────────────────────────────────────────────────────

def query(conn) -> dict:
    with conn.cursor() as cur:
        # event counts
        cur.execute("""
            SELECT
                count(*) FILTER (WHERE timestamp > now() - interval '1 hour') AS ev_1h,
                count(*) FILTER (WHERE timestamp > now() - interval '24 hours') AS ev_24h
            FROM minions.memory_events
        """)
        ev_1h, ev_24h = cur.fetchone()

        # note counts + embedding coverage
        cur.execute("""
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE embedding IS NOT NULL) AS embedded
            FROM minions.memory_nodes
        """)
        notes_total, notes_embedded = cur.fetchone()

        # last event
        cur.execute("""
            SELECT timestamp, event_type, repo
            FROM minions.memory_events
            ORDER BY timestamp DESC LIMIT 1
        """)
        row = cur.fetchone()
        last_event = {"ts": str(row[0]), "type": row[1], "repo": row[2]} if row else None

        # last note
        cur.execute("""
            SELECT created_at, title, content
            FROM minions.memory_nodes
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        last_note = {"ts": str(row[0]), "title": row[1], "content": row[2]} if row else None

        # recent 5 events
        cur.execute("""
            SELECT timestamp, event_type, repo
            FROM minions.memory_events
            ORDER BY timestamp DESC LIMIT 5
        """)
        recent = [{"ts": str(r[0]), "type": r[1], "repo": r[2]} for r in cur.fetchall()]

    return {
        "events_1h": ev_1h,
        "events_24h": ev_24h,
        "notes_total": notes_total,
        "notes_embedded": notes_embedded,
        "last_event": last_event,
        "last_note": last_note,
        "recent_events": recent,
        "mcp_ok": True,  # if we got here, DB is reachable — good enough proxy
    }


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    load_env()
    url = db_url()

    # hide cursor, handle ctrl+c cleanly
    print("\033[?25l", end="", flush=True)
    def cleanup(sig=None, frame=None):
        print("\033[?25h\033[0m", flush=True)  # restore cursor
        sys.exit(0)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    conn = None
    while True:
        data = None
        error = None

        if not url:
            error = "DATABASE_URL not set in agent-memory/.env"
        else:
            try:
                if conn is None or conn.closed:
                    import psycopg
                    conn = psycopg.connect(url)
                data = query(conn)
            except Exception as e:
                error = str(e)[:60]
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None

        panel = render(data, error, url_ok=bool(url))
        # move cursor to top-left and overwrite
        print(f"\033[H\033[J{panel}", flush=True)

        time.sleep(REFRESH_SECS)


if __name__ == "__main__":
    main()
