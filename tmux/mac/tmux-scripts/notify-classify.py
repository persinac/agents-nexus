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
    r"|:\(\)\s*\{|>\s*/dev/|\bgit\s+push\b|--force\b|--hard\b"
    r"|\|\s*(sh|bash|zsh)\b|\b(curl|wget)\b[^|]*\|\s*(sh|bash)"
    r"|\bchmod\s+-R|\bchown\s+-R|\bnpm\s+publish\b|\btruncate\b|\bmv\s)",
    re.I,
)

# --- Deterministic read-only allowlist -------------------------------------
# Common inspection commands auto-approve WITHOUT an LLM call — instant and 100%
# reliable, so the gate never wavers on dual-use tools (curl, git, kubectl). The
# LLM only sees commands this can't vouch for. Conservative: any output redirect
# or unknown command head falls through to the LLM.
_READ_CMDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "echo", "pwd", "which", "whoami", "hostname",
    "uname", "date", "env", "printenv", "df", "du", "stat", "file", "tree", "realpath",
    "dirname", "basename", "grep", "egrep", "fgrep", "rg", "ag", "ack", "sort", "uniq",
    "cut", "tr", "column", "jq", "yq", "xxd", "od", "cmp", "tac", "nl", "cd", "true",
    "sleep", "test", "readlink", "type", "id", "ps", "top", "free", "uptime", "comm",
})
_READ_SUB = {
    "git": frozenset({"status", "log", "diff", "show", "merge-base", "range-diff",
                      "rev-parse", "rev-list", "blame", "ls-files", "ls-tree", "describe",
                      "shortlog", "reflog", "cat-file", "for-each-ref", "show-ref",
                      "fetch", "whatchanged", "grep", "version", "remote"}),
    "kubectl": frozenset({"get", "describe", "logs", "top", "explain", "api-resources",
                          "api-versions", "version", "cluster-info"}),
    "helm": frozenset({"template", "diff", "get", "list", "status", "show", "history",
                       "version", "lint"}),
    "terraform": frozenset({"plan", "show", "output", "validate", "version", "providers"}),
    "docker": frozenset({"ps", "images", "logs", "inspect", "version", "info", "top",
                         "stats", "history"}),
    "gh": frozenset({"pr", "issue", "run", "repo", "release", "api", "status"}),  # gh ... view/list mostly read; api below
}
# curl/wget become "modify" if they carry a write method, a request body, an upload,
# or write the response to a file.
_HTTP_WRITE = re.compile(
    r"(-X\s*(post|put|patch|delete)|--data\b|--data-[a-z]+|(^|\s)-d\b"
    r"|--upload-file|(^|\s)-T\b|(^|\s)-[oO]\b|--output)", re.I)


# Global flags that take a separate-token value and can appear BEFORE the
# subcommand (e.g. `kubectl --context prod get`, `git -C /repo show`). Their value
# is skipped when locating the real subcommand.
_VALUE_FLAGS = frozenset({
    "-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user", "--as",
    "--server", "-s", "-C", "--git-dir", "--work-tree", "--profile", "--region", "-o",
})


def _subcommand(toks):
    """First non-flag token (skipping flags and known flag-values) = the subcommand."""
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            if "=" not in t and t in _VALUE_FLAGS:
                skip = True
            continue
        return t
    return ""


def _segment_is_read(seg):
    seg = seg.strip()
    if not seg:
        return True
    toks = seg.split()
    while toks:  # strip VAR=val prefixes and benign command wrappers
        b = os.path.basename(toks[0])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]
        elif b == "timeout":
            toks = toks[2:] if len(toks) > 1 and re.match(r"^[\d.]+[smhd]?$", toks[1]) else toks[1:]
        elif b in ("command", "nice", "stdbuf", "nohup"):
            toks = toks[1:]
        else:
            break
    if not toks:
        return True
    cmd = os.path.basename(toks[0])
    if cmd in ("curl", "wget"):
        return not _HTTP_WRITE.search(seg)
    if cmd == "sed":
        return "-i" not in toks                                    # not in-place
    if cmd == "find":
        return not re.search(r"-(delete|exec|execdir|ok|fprint|fls|fprintf)\b", seg)
    if cmd in _READ_CMDS:
        return True
    if cmd in _READ_SUB:
        sub = _subcommand(toks[1:])                                # skip leading flags (e.g. --context X)
        if cmd == "gh":  # gh subcommands need a read action (view/list/status)
            return sub in _READ_SUB[cmd] and bool(re.search(r"\b(view|list|status|get)\b", seg))
        return sub in _READ_SUB[cmd]
    return False


def _deterministic_read(cmd):
    """True only if EVERY segment is a known read-only operation. Any output redirect
    (>, >>) or unrecognized command falls through to the LLM."""
    if ">" in cmd:
        return False
    return all(_segment_is_read(s) for s in re.split(r"&&|\|\||;|\|", cmd))

_PROMPT = """You are the middle-man between an autonomous coding agent and its human operator on Slack. The agent paused to ask permission to use a tool. Reply with ONLY a compact JSON object:

{{"decision":"read|modify","category":"<2-4 word label>","summary":"<one or two sentences>"}}

decision rules:
- "read" = the command only INSPECTS and does not change files, branches, remotes, deployed resources, or download-and-run / install anything. Treat ALL of these as read: git status/log/diff/show/branch -l/rev-parse/merge-base/range-diff/blame/ls-files, `git fetch` (updates only remote-tracking refs — safe), kubectl get/describe/logs/top, helm template/diff/get/list, terraform plan, docker ps/images/logs/inspect, and cat/ls/grep/find(without -delete)/head/tail (incl. -f follow)/wc/echo/which/jq.
- "modify" = changes state: writes/edits/deletes/moves files, redirects to a file (> or >>), git add/commit/push/reset/checkout/merge/rebase/stash, kubectl apply/create/delete/patch/scale/rollout/edit, helm install/upgrade/uninstall, terraform apply/destroy, package installs, sudo, or piping into a shell.
- Compound command (&&, ||, ;, |): "read" only if EVERY part is read; one modifying part makes the whole "modify".
- BE DECISIVE: if every part is plainly an inspection, answer "read" — do NOT hedge to "modify" just because the domain (git/k8s/deploy) feels operational. Reserve "modify" for an actual state change or genuine ambiguity. Your decision MUST agree with your summary: if the summary concludes nothing is modified, decision is "read".

category: short label, e.g. "read-only inspection", "git fetch", "file edit", "k8s apply", "package install", "delete files".
summary: 1-2 sentences for the operator — what the agent wants to do, what it's pertinent to, and a brief HONEST safety read. Refer to it as "the agent". Do not invent facts.

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
        "wait_since": os.environ.get("WAIT_SINCE", ""),
        "category": category,
        "summary": summary,
    }))
    sys.exit(10)


def _last_tool_use(transcript_path):
    """Most recent tool_use in the transcript tail. Scans back through the last few
    assistant messages (not just the latest, which may be text-only) so a pending
    tool call is still found when the assistant wrote prose around it."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        lines = deque(open(transcript_path, errors="replace"), maxlen=500)
    except OSError:
        return None
    seen = 0
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
            for b in reversed(content):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    return (b.get("name") or "", b.get("input") or {})
        seen += 1
        if seen >= 6:                # bound the look-back so we don't grab a stale tool
            break
    return None


def classify(name, inp):
    """Classify a pending tool call. Returns (decision, category, summary) where
    decision is 'read' (safe to auto-approve) or 'modify' (needs a human). This is
    the reusable core shared by the Notification hook (main) and the Agent SDK
    runner's can_use_tool gate (--tool mode)."""
    inp = inp or {}
    short = (name or "").split("__")[-1].lower()
    det = _deterministic_summary(name, inp)
    # 1. Clearly read-only local tools -> read, no LLM.
    if short in READ_TOOLS:
        return "read", "read-only", det
    # 2. Bash: hard denylist (modify) -> deterministic read allowlist (auto) -> LLM.
    if short in ("bash", "shell"):
        cmd = (inp.get("command") or "").strip()
        safe = bool(cmd) and not _DENY.search(cmd)
        if safe and _deterministic_read(cmd):
            return "read", "read-only inspection", det
        llm = _llm(name, inp)
        if safe and llm and llm[0] == "read":
            return "read", llm[1], (llm[2] or det)
        return "modify", (llm[1] if llm else "shell command"), (llm[2] if llm and llm[2] else det)
    # 3. Writes / web egress / MCP / unknown -> modify (LLM summarizes).
    llm = _llm(name, inp)
    return "modify", (llm[1] if llm else "needs review"), (llm[2] if llm and llm[2] else det)


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
    decision, category, summary = classify(name, inp)
    if decision == "read":
        sys.exit(0)                                       # safe -> auto-approve
    _emit_modify(category, summary)                        # needs a human


if __name__ == "__main__":
    # Direct classification mode for the Agent SDK runner's can_use_tool: read a
    # {"name","input"} JSON on stdin, print {"decision","category","summary"} JSON.
    # Never exits non-zero — the caller fails safe to "modify" on a bad/empty line.
    if "--tool" in sys.argv[1:]:
        try:
            _req = json.load(sys.stdin) or {}
            _d, _c, _s = classify(_req.get("name", ""), _req.get("input") or {})
            print(json.dumps({"decision": _d, "category": _c, "summary": _s}))
        except Exception:
            print(json.dumps({"decision": "modify", "category": "needs review", "summary": ""}))
        sys.exit(0)
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _emit_modify("needs review", os.environ.get("FB", "needs input"))
