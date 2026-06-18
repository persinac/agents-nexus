#!/usr/bin/env python3
"""Permission-prompt auto-approval classifier for the slack-bridge round-trip.

Reads a Claude Code Notification hook payload on stdin and prints ONE word:
  read    -> safe to auto-approve (the hook selects menu option 1)
  modify  -> needs a human (surfaced to Slack)

Policy (conservative — when in doubt, "modify"):
  * Only fires for KIND=permission_prompt (elicitation/questions always -> modify).
  * Local read tools (Read/Glob/Grep/LS/NotebookRead) -> read.
  * Mutating tools (Edit/Write/MultiEdit/NotebookEdit), MCP tools, web egress
    (WebFetch/WebSearch), and anything unknown -> modify.
  * Bash: a hard denylist of irreversible/dangerous patterns -> modify; otherwise
    a small LLM (Anthropic Haiku via litellm) categorizes the command. A compound
    command counts as "read" only if every part is read-only.

Fail-safe: ANY error (no key, import failure, API/timeout, parse issue) -> "modify".
The worst case is "ask the human", never an accidental auto-run.

Env: KIND=notification_type. Anthropic key loaded from the repo .env if not already
in the environment. No api_base override (the .env base is container-only).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import deque

MODEL = "anthropic/claude-haiku-4-5-20251001"

READ_TOOLS = {"read", "glob", "grep", "ls", "notebookread"}
WRITE_TOOLS = {"edit", "write", "multiedit", "notebookedit"}

# Irreversible / dangerous shell patterns that must NEVER auto-approve, even if the
# LLM mis-classifies. Belt-and-suspenders over the model's own judgement.
_DENY = re.compile(
    r"(\brm\b|\brmdir\b|\bsudo\b|\bdd\b|\bmkfs|\bshutdown\b|\breboot\b|\bkillall\b"
    r"|:\(\)\s*\{|>\s*/dev/|\bgit\s+push\b|--force\b|(^|\s)-[a-zA-Z]*f[a-zA-Z]*\b"
    r"|\|\s*(sh|bash|zsh)\b|\b(curl|wget)\b[^|]*\|\s*(sh|bash)"
    r"|\bchmod\s+-R|\bchown\s+-R|\bnpm\s+publish\b|\btruncate\b|\bmv\s)",
    re.I,
)

_PROMPT = """You are a safety gate that decides whether a shell command may be auto-approved.

Reply with exactly one word: "read" or "modify".

"read"  = the command ONLY inspects or reads state and changes nothing (e.g. ls, cat,
          grep, find (without -delete or a mutating -exec), git status/log/diff/show,
          ps, head, tail, wc, echo, which, env, pwd, jq). For a compound command
          (&&, ||, ;, |) it is "read" only if EVERY part is read-only.
"modify" = it writes/creates/deletes/moves files, redirects to a file (>, >>), changes
          git state, installs/downloads, runs via sudo, pipes into a shell, mutates a
          remote/service, or you are at all unsure.

When uncertain, answer "modify".

Command:
{cmd}

One word:"""


def _load_key() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        # tmux/mac/tmux-scripts/notify-classify.py -> repo root is 4 levels up.
        root = os.path.realpath(__file__)
        for _ in range(4):
            root = os.path.dirname(root)
        for ln in open(os.path.join(root, ".env"), errors="replace"):
            ln = ln.strip()
            if ln.startswith("ANTHROPIC_API_KEY=") and "=" in ln:
                os.environ["ANTHROPIC_API_KEY"] = ln.split("=", 1)[1].strip()
                break
    except Exception:
        pass


def _classify_bash(cmd: str) -> str:
    _load_key()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "modify"
    try:
        import litellm
        litellm.suppress_debug_info = True
        resp = litellm.completion(
            model=MODEL,
            messages=[{"role": "user", "content": _PROMPT.format(cmd=cmd[:1500])}],
            max_tokens=5,
            temperature=0,
            timeout=10,
        )
        out = (resp.choices[0].message.content or "").strip().lower()
        return "read" if out.startswith("read") else "modify"
    except Exception:
        return "modify"


def _last_tool_use(transcript_path: str):
    """(name, input) of the last tool_use in the most recent assistant message, or None."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        lines = deque(open(transcript_path, errors="replace"), maxlen=500)
    except OSError:
        return None
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        tool = None
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tool = (b.get("name") or "", b.get("input") or {})
        return tool  # last tool_use in this assistant message (or None)
    return None


def classify() -> str:
    if os.environ.get("KIND") != "permission_prompt":
        return "modify"  # elicitation / questions always go to a human
    try:
        data = json.load(sys.stdin) or {}
    except Exception:
        return "modify"
    tool = _last_tool_use(data.get("transcript_path", ""))
    if not tool:
        return "modify"
    name, inp = tool
    short = name.split("__")[-1].lower()
    if short in ("bash", "shell"):
        cmd = (inp.get("command") or "").strip()
        if not cmd or _DENY.search(cmd):
            return "modify"
        return _classify_bash(cmd)
    if short in READ_TOOLS:
        return "read"
    return "modify"  # writes, MCP tools, web egress, unknown -> ask


if __name__ == "__main__":
    try:
        print(classify())
    except Exception:
        print("modify")
