#!/usr/bin/env python3
"""Build the slack-bridge /notify body from a Claude Code Notification hook payload.

Reads the hook JSON on stdin and enriches the surfaced message with *what the agent
is actually asking* — the pending tool call (e.g. the bash command, the file being
edited, the URL being fetched) or the question text — pulled from the tail of the
session transcript. Falls back to the hook's own `message`, then to $FB.

Env: AN=agent name, PANE=tmux pane id, KIND=notification_type, FB=fallback text.
Emits one line of JSON: {"name","pane","message","kind"} for POST /notify.
"""
from __future__ import annotations

import json
import os
import sys
from collections import deque


def _load_stdin() -> dict:
    try:
        return json.load(sys.stdin) or {}
    except Exception:
        return {}


def _trunc(s, n: int = 280) -> str:
    s = " ".join(str(s).split())  # flatten whitespace/newlines (keeps Slack blockquote intact)
    return s if len(s) <= n else s[: n - 1] + "…"


def _last_assistant_blocks(transcript_path: str) -> list:
    """Content blocks of the most recent assistant message in the transcript tail."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    try:
        lines = deque(open(transcript_path, errors="replace"), maxlen=500)
    except OSError:
        return []
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            return content
        if isinstance(content, str) and content.strip():
            return [{"type": "text", "text": content}]
    return []


def _summarize_tool(name: str, inp: dict) -> str:
    """One-line, Slack-friendly description of a pending tool call."""
    short = (name or "").split("__")[-1].lower()
    if short in ("bash", "shell"):
        return f"Bash: `{_trunc(inp.get('command', ''), 240)}`"
    if short in ("edit", "multiedit"):
        return f"Edit `{inp.get('file_path', '?')}`"
    if short == "write":
        return f"Write `{inp.get('file_path', '?')}`"
    if short == "notebookedit":
        return f"Edit notebook `{inp.get('notebook_path', '?')}`"
    if short == "read":
        return f"Read `{inp.get('file_path', '?')}`"
    if short == "webfetch":
        return f"WebFetch {_trunc(inp.get('url', ''), 200)}"
    if short == "websearch":
        return f"WebSearch: {_trunc(inp.get('query', ''), 200)}"
    if "askuserquestion" in short:
        qs = inp.get("questions") or []
        if isinstance(qs, list) and qs and isinstance(qs[0], dict):
            q0 = qs[0]
            opts = ", ".join(
                o.get("label", "") for o in (q0.get("options") or []) if isinstance(o, dict)
            )
            base = _trunc(q0.get("question", "question"), 220)
            return f"{base}" + (f" — _options: {_trunc(opts, 120)}_" if opts else "")
    # generic: tool name + the most descriptive field we can find
    for k in ("command", "query", "url", "prompt", "file_path", "path", "pattern", "title", "description"):
        if inp.get(k):
            return f"{name}: {_trunc(inp[k], 220)}"
    return name or "tool call"


def _describe(blocks: list) -> str | None:
    """Prefer the last pending tool call; else the assistant's last text (a question)."""
    tool_summary = None
    last_text = None
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use":
            tool_summary = _summarize_tool(b.get("name"), b.get("input") or {})
        elif b.get("type") == "text" and b.get("text", "").strip():
            last_text = b["text"]
    if tool_summary:
        return tool_summary
    if last_text:
        return _trunc(last_text, 400)
    return None


def main() -> None:
    data = _load_stdin()
    detail = _describe(_last_assistant_blocks(data.get("transcript_path", "")))
    message = detail or (data.get("message") or "").strip() or os.environ.get("FB", "needs input")
    print(json.dumps({
        "name": os.environ.get("AN", ""),
        "pane": os.environ.get("PANE", ""),
        "message": message,
        "kind": os.environ.get("KIND", ""),
    }))


if __name__ == "__main__":
    main()
