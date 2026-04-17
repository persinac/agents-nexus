#!/usr/bin/env python3
"""Append a memory event to the local buffer (~/.tmux/memory-events.jsonl).

Called from hook-memory.sh in the background — must be fast, no external deps.

Usage:
    memory-hook.py <event_type> <tmux_pane_id>

Reads hook JSON from stdin (cwd, session_id, tool_name, etc.).
All errors are silently swallowed — memory logging is best-effort.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def _tmux(pane: str, fmt: str) -> str:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", fmt],
            capture_output=True, text=True, timeout=1,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def main() -> None:
    event_type = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    pane_id    = sys.argv[2] if len(sys.argv) > 2 else os.getenv("TMUX_PANE", "")
    cwd_arg    = sys.argv[3] if len(sys.argv) > 3 else ""  # explicit cwd for session_start/end

    # Parse stdin hook JSON (best-effort)
    hook: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            hook = json.loads(raw)
    except Exception:
        pass

    # Resolve CWD: explicit arg → hook JSON → tmux pane path
    cwd = cwd_arg or hook.get("cwd", "")
    if not cwd and pane_id:
        cwd = _tmux(pane_id, "#{pane_current_path}")

    # Resolve slot (window index — live, handles renumber-windows)
    slot = _tmux(pane_id, "#{window_index}") if pane_id else ""

    # Project = basename of CWD
    project = os.path.basename(cwd) if cwd else ""

    # Device — prefer env var, fall back to hostname short name
    device = os.getenv("AGENT_DEVICE", socket.gethostname().split(".")[0])

    # Build payload from hook-specific fields
    payload: dict = {}
    if tool_name := hook.get("tool_name", ""):
        payload["tool_name"] = tool_name
    if notif_type := hook.get("notification_type", ""):
        payload["notification_type"] = notif_type

    event = {
        "timestamp":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": event_type,
        "project":    project,
        "cwd":        cwd,
        "device":     device,
        "repo":       project,
        "agent_slot": slot,
        "session_id": hook.get("session_id", ""),
        "payload":    payload,
    }

    buffer = Path.home() / ".tmux" / "memory-events.jsonl"
    buffer.parent.mkdir(exist_ok=True)
    with buffer.open("a") as f:
        f.write(json.dumps(event) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never fail — memory logging is best-effort
