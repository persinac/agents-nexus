#!/usr/bin/env python3
"""Stop-hook surfacer: decide whether a just-finished agent turn needs the HUMAN.

Reads a Claude Code Stop hook payload (stdin) and inspects the agent's final
assistant message. The fleet's agents talk to EACH OTHER over the A2A bus, so most
turn-ends are progress / FYI / other-agent chatter that must NOT ping the operator.
This is the "middle" between full-naive (surface everything → Slack floods) and
the existing permission-only surfacing: post to Slack ONLY when the agent is
blocked on the human.

Output contract (so the hook needs no JSON parsing):
  exit 0   -> do NOT surface (progress / done / talking to another agent / unclear).
  exit 10  -> needs the human. Prints a /notify JSON body on stdout
              ({name,pane,kind:"question",summary}) ready to POST. kind is NOT a
              permission kind, so a Slack thread reply delivers VERBATIM to the agent.

Cheap pre-filter (no LLM): only turns whose final text carries a question/decision
marker reach the LLM — everything else exits 0 immediately. If the LLM is
unavailable but the text looked like a question, fail TOWARD surfacing (the raw
text) rather than dropping a real ask. Any unexpected error fails to exit 0 (no
spurious posts).

Env: AN=agent name, PANE=tmux pane id.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import deque

MODEL = "anthropic/claude-haiku-4-5-20251001"

# Question / decision markers — gate the LLM so the common (non-question) turn is free.
_MARKER = re.compile(
    r"\?|\b(should i|shall i|which (one|option|approach|way)|do you want|would you like"
    r"|let me know|your call|wdyt|please confirm|approve|prefer|option\s*[0-9]"
    r"|go with|proceed\b|or should|need (your|you to)|waiting on you|decide|sign\s?off"
    r"|thoughts\?|ok to)\b",
    re.I,
)

_PROMPT = """You are the middle-man between an autonomous coding agent and its human operator on Slack. SEVERAL such agents run at once and they message EACH OTHER over a bus. One agent just finished its turn and is now idle. Below is its final message.

Decide whether the agent is BLOCKED waiting on the HUMAN operator to answer a question or make a decision.

Answer "no" if the message is: reporting progress or completion, narrating/thinking out loud, or addressed to ANOTHER AGENT (e.g. "notifying wallet-api", "asked infrastructure to…", "@infra", "pinged cns", "waiting on the cns agent"). Answer "yes" ONLY when it genuinely needs the human to reply or decide.

Reply with ONLY a compact JSON object:
{{"needs_user":"yes|no","summary":"<one sentence: exactly what the human must decide or answer>"}}

Final message:
{msg}

JSON:"""


def _trunc(s, n=500):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _last_assistant_text(transcript_path):
    """Final assistant message's text (joined text blocks) from the transcript tail."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        lines = deque(open(transcript_path, errors="replace"), maxlen=500)
    except OSError:
        return ""
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(p for p in parts if p.strip()).strip()
        return ""
    return ""


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


def _emit(summary):
    print(json.dumps({
        "name": os.environ.get("AN", ""),
        "pane": os.environ.get("PANE", ""),
        "kind": "question",                      # NOT a permission kind -> verbatim reply
        "summary": _trunc(summary, 1500),
    }))
    sys.exit(10)


def main():
    try:
        data = json.load(sys.stdin) or {}
    except Exception:
        data = {}
    text = _last_assistant_text(data.get("transcript_path", ""))
    if not text:
        sys.exit(0)                              # turn ended with no prose to surface
    if not _MARKER.search(text):
        sys.exit(0)                              # no question/decision signal -> skip (no LLM)

    # Looks like a question — confirm it needs the HUMAN (not another agent).
    _load_key()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _emit(text)                              # no LLM available -> surface the question text
    try:
        import litellm
        litellm.suppress_debug_info = True
        resp = litellm.completion(
            model=MODEL,
            messages=[{"role": "user", "content": _PROMPT.format(msg=text[:2000])}],
            max_tokens=160, temperature=0, timeout=12,
        )
        out = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", out, re.S)
        obj = json.loads(m.group(0)) if m else {}
        if str(obj.get("needs_user", "")).lower().startswith("y"):
            _emit(obj.get("summary") or text)
        sys.exit(0)                              # LLM judged: not for the human
    except Exception:
        _emit(text)                              # LLM failed but it looked like a question


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)                              # fail safe: never spam on unexpected error
