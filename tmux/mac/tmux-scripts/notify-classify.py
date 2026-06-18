#!/usr/bin/env python3
"""Permission-prompt middle-man: auto-approve gate + human-friendly summary.

Reads a Claude Code Notification hook payload (stdin) for a permission_prompt and:
  * decides read vs modify (the auto-approve gate), and
  * for prompts that need a human, produces a `category` + a 1-2 sentence
    middle-man `summary` (what the agent wants, what it's pertinent to, an honest
    safety read).

Output contract (so the hook needs no JSON parsing in bash):
  exit 0   -> READ: safe to auto-approve. Nothing printed.
  exit 10  -> MODIFY: needs a human. Prints the /notify JSON body on stdout
              ({name,pane,kind,category,summary}) ready to POST.
Any error -> exit 10 with a deterministic fallback summary (fail safe to "ask").

Policy: local reads (Read/Glob/Grep/LS/NotebookRead) -> read (no LLM, fast).
Bash -> hard denylist (rm/sudo/dd/redirect/pipe-to-shell/--force/...) forces modify,
otherwise an Anthropic Haiku call (litellm) decides + summarizes. Writes / MCP / web
egress / unknown -> modify (LLM summarizes, decision forced modify). Compound shell
commands are "read" only if every part is read-only.

Env: AN=agent name, PANE=tmux pane id, KIND=notification_type, FB=fallback text.
Anthropic key loaded from the repo .env; default api base (the .env base is container-only).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import deque

MODEL = "anthropic/claude-haiku-4-5-20251001"
READ_TOOLS = {"read", "glob", "grep", "ls", "notebookread"}

_DENY = re.compile(
    r"(\brm\b|\brmdir\b|\bsudo\b|\bdd\b|\bmkfs|\bshutdown\b|\breboot\b|\bkillall\b"
    r"|:\(\)\s*\{|>\s*/dev/|\bgit\s+push\b|--force\b|(^|\s)-[a-zA-Z]*f[a-zA-Z]*\b"
    r"|\|\s*(sh|bash|zsh)\b|\b(curl|wget)\b[^|]*\|\s*(sh|bash)"
    r"|\bchmod\s+-R|\bchown\s+-R|\bnpm\s+publish\b|\btruncate\b|\bmv\s)",
    re.I,
)

_PROMPT = """You are the middle-man between an autonomous coding agent and its human operator on Slack. The agent has paused to ask permission to use a tool. Read the tool call and reply with ONLY a compact JSON object:

{{"decision":"read|modify","category":"<2-4 word label>","summary":"<one or two sentences>"}}

- decision: "read" if the action only inspects/reads state and changes nothing. For a shell command, EVERY part of a compound (&&, ||, ;, |) must be read-only. Otherwise "modify". When unsure, "modify".
- category: a short label, e.g. "read-only inspection", "file edit", "package install", "helm/k8s deploy", "git push", "delete files", "network write".
- summary: 1-2 sentences for the operator — what the agent wants to do, what it's pertinent to, and a brief HONEST safety read (e.g. "routine, looks safe" or "destructive — deletes data, review"). Refer to it as "the agent". Do not invent facts.

Tool: {name}
Input: {inp}

JSON:"""


def _trunc(s, n=240):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        root = os.path.realpath(__file__)
        for _ in range(4):  # tmux/mac/tmux-scripts/ -> repo root
            root = os.path.dirname(root)
        for ln in open(os.path.join(root, ".env"), errors="replace"):
            ln = ln.strip()
            if ln.startswith("ANTHROPIC_API_KEY=") and "=" in ln:
                os.environ["ANTHROPIC_API_KEY"] = ln.split("=", 1)[1].strip()
                break
    except Exception:
        pass


def _deterministic_summary(name, inp):
    short = (name or "").split("__")[-1].lower()
    if short in ("bash", "shell"):
        return f"`{_trunc(inp.get('command', ''))}`"
    if short in ("edit", "multiedit", "write", "notebookedit"):
        return f"`{inp.get('file_path') or inp.get('notebook_path', '?')}`"
    for k in ("command", "url", "query", "path", "file_path", "prompt"):
        if inp.get(k):
            return _trunc(inp[k])
    return name or "a tool call"


def _llm(name, inp):
    """Return (decision, category, summary) from the LLM, or None on any failure."""
    _load_key()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import litellm
        litellm.suppress_debug_info = True
        body = json.dumps(inp)[:1500]
        resp = litellm.completion(
            model=MODEL,
            messages=[{"role": "user", "content": _PROMPT.format(name=name, inp=body)}],
            max_tokens=200,
            temperature=0,
            timeout=12,
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        obj = json.loads(m.group(0))
        decision = "read" if str(obj.get("decision", "")).lower().startswith("read") else "modify"
        category = _trunc(obj.get("category") or "change", 40)
        summary = _trunc(obj.get("summary") or "", 500)
        return decision, category, summary
    except Exception:
        return None


def _emit_modify(category, summary):
    print(json.dumps({
        "name": os.environ.get("AN", ""),
        "pane": os.environ.get("PANE", ""),
        "kind": os.environ.get("KIND", ""),
        "category": category,
        "summary": summary,
    }))
    sys.exit(10)


def _last_tool_use(transcript_path):
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
        return tool
    return None


def main():
    if os.environ.get("KIND") != "permission_prompt":
        _emit_modify("question", os.environ.get("FB", "needs input"))
    try:
        data = json.load(sys.stdin) or {}
    except Exception:
        data = {}
    tool = _last_tool_use(data.get("transcript_path", ""))
    if not tool:
        _emit_modify("needs review", os.environ.get("FB", "needs input"))
    name, inp = tool
    short = name.split("__")[-1].lower()

    # Fast path: clearly read-only local tools auto-approve without an LLM call.
    if short in READ_TOOLS:
        sys.exit(0)

    llm = _llm(name, inp)
    det = _deterministic_summary(name, inp)

    if short in ("bash", "shell"):
        cmd = (inp.get("command") or "").strip()
        if cmd and not _DENY.search(cmd) and llm and llm[0] == "read":
            sys.exit(0)  # read-only command -> auto-approve
        if llm:
            _emit_modify(llm[1], llm[2] or det)
        _emit_modify("shell command", det)

    # writes / web egress / MCP / unknown -> always ask (decision forced modify)
    if llm:
        _emit_modify(llm[1], llm[2] or det)
    _emit_modify("needs review", det)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _emit_modify("needs review", os.environ.get("FB", "needs input"))
