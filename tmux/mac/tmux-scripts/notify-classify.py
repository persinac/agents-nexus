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
otherwise a deterministic read-only allowlist (git/kubectl/helm/terraform/docker/
gh/aws, curl/wget/sed/find/mv with care), otherwise an Anthropic Haiku call
(litellm) decides + summarizes. Writes / MCP / web egress / unknown -> modify (LLM
summarizes, decision forced modify). Compound shell commands are "read" only if
every part is read-only.

Loosened 2026-08-14 per Alex — more confident given block-credential-dump.sh as a
separate, independent layer for the credential-exposure threat specifically. That
hook does NOT cover any of the below; each of these is its own risk call:
  - kubectl/aws: all read verbs allowlisted. exec/cp/attach/debug/run/port-forward/
    proxy stay excluded on purpose — they execute code, copy data into a live
    resource, or open a network tunnel, none of which is "read".
  - A force-less `git push` now auto-approves. -f/--force/--force-with-lease/--hard
    still hard-deny unconditionally, checked before this allowlisting ever runs.
  - `mv` auto-approves unless the segment touches a credential-ish path or uses -f.
  - `chmod -R`/`chown -R` removed from the hard denylist (falls to the LLM now).

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
    r"|:\(\)\s*\{|>\s*/dev/"
    # Force pushes always hard-deny, checked before the git-push allowlisting below
    # ever runs. --force/--force-with-lease/--hard catch the long forms anywhere in
    # the command; the git-push-scoped clause closes the short-form `-f` gap without
    # blocking every unrelated `-f` flag (grep -f, curl -f, ...) fleet-wide.
    r"|\bgit\s+push\b[^|;&]*-f\b|--force\b|--hard\b"
    r"|\|\s*(sh|bash|zsh)\b|\b(curl|wget)\b[^|]*\|\s*(sh|bash)"
    r"|\bnpm\s+publish\b|\btruncate\b)",
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
    # push added 2026-08-14: a force-less push is content-inert on the local side and
    # any force variant is already hard-denied above, unconditionally, before this
    # allowlist is ever consulted -- so "push" reaching here is guaranteed force-less.
    "git": frozenset({"status", "log", "diff", "show", "merge-base", "range-diff",
                      "rev-parse", "rev-list", "blame", "ls-files", "ls-tree", "describe",
                      "shortlog", "reflog", "cat-file", "for-each-ref", "show-ref",
                      "fetch", "whatchanged", "grep", "version", "remote", "push"}),
    # diff/events/auth/wait added 2026-08-14 (none mutate cluster state). "auth" needs
    # a further check below -- `auth can-i` is read but `auth reconcile -f rbac.yaml`
    # applies RBAC changes, so membership here alone is not enough for it. Deliberately
    # excluded: exec/cp/attach/debug/run (execute code or create a resource),
    # port-forward/proxy (open a network tunnel -- not a "read" of anything), config
    # (can mutate the local kubeconfig, e.g. delete-context/use-context).
    "kubectl": frozenset({"get", "describe", "logs", "top", "explain", "api-resources",
                          "api-versions", "version", "cluster-info", "diff", "events",
                          "auth", "wait"}),
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

# mv auto-approves (added 2026-08-14) EXCEPT when it touches a credential-ish path
# (same spirit as block-credential-dump.sh's CRED list, trimmed to what's relevant
# here) or forces an overwrite with -f. This is deliberately narrow -- it is not a
# general data-loss guard, just enough to keep a careless mv off dotfiles/secrets.
_MV_SENSITIVE = re.compile(
    r"(\.ssh\b|\.aws\b|\.kube\b|\.npmrc\b|\.netrc\b|id_rsa|id_ed25519"
    r"|\.pem\b|\.env\b|\.git/config|secrets?\.ya?ml)", re.I)


# Global flags that take a separate-token value and can appear BEFORE the
# subcommand (e.g. `kubectl --context prod get`, `git -C /repo show`). Their value
# is skipped when locating the real subcommand. --endpoint-url/--output added
# 2026-08-14 for `aws --profile x --region y ec2 describe-instances`-style commands.
_VALUE_FLAGS = frozenset({
    "-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user", "--as",
    "--server", "-s", "-C", "--git-dir", "--work-tree", "--profile", "--region", "-o",
    "--endpoint-url", "--output",
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


def _strip_leading_flags(toks):
    """Drop a leading run of flags (and their value-tokens) from toks. Used by the
    aws check, which needs TWO positional tokens (service, then action) rather than
    _subcommand's single one."""
    i = 0
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if ("=" not in toks[i] and toks[i] in _VALUE_FLAGS) else 1
    return toks[i:]


def _aws_is_read(toks):
    """AWS CLI read heuristic (added 2026-08-14): `aws s3 ls`/`presign`, or
    `aws <service> <get-*|describe-*|list-*|head-*>` -- the same prefix convention
    AWS's own read-only IAM policy examples use. Anything else (create-/put-/delete-/
    update-/modify-/run-/terminate-/tag-/attach-/... or a shape this can't parse)
    returns False and falls through to the LLM -- this never silently approves an
    unrecognized action."""
    rest = _strip_leading_flags(toks[1:])                # drop 'aws' + global flags
    if not rest:
        return False
    service = rest[0].lower()
    rest = _strip_leading_flags(rest[1:])
    if not rest:
        return False
    action = rest[0].lower()
    if service == "s3":
        return action in ("ls", "presign")
    return action.startswith(("get-", "describe-", "list-", "head-"))


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
    if cmd == "mv":
        return "-f" not in toks and not _MV_SENSITIVE.search(seg)
    if cmd == "aws":
        return _aws_is_read(toks)
    if cmd in _READ_CMDS:
        return True
    if cmd in _READ_SUB:
        sub = _subcommand(toks[1:])                                # skip leading flags (e.g. --context X)
        if cmd == "gh":  # gh subcommands need a read action (view/list/status)
            return sub in _READ_SUB[cmd] and bool(re.search(r"\b(view|list|status|get)\b", seg))
        if cmd == "kubectl" and sub == "auth":  # can-i is read; reconcile applies RBAC
            return bool(re.search(r"\bcan-i\b", seg))
        if cmd == "git" and sub == "push":
            # Self-contained, not just relying on the caller's _DENY pre-check: plain
            # frozenset membership can't see flags, and a segment-level check here is
            # what actually inspects THIS segment for a force flag, the same way the
            # mv check below inspects its own segment rather than trusting a caller.
            return "-f" not in toks and not re.search(r"--force", seg, re.I)
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
- "read" = the command only INSPECTS and does not change files, branches, remotes, deployed resources, or download-and-run / install anything. Treat ALL of these as read: git status/log/diff/show/branch -l/rev-parse/merge-base/range-diff/blame/ls-files, `git fetch` (updates only remote-tracking refs — safe), a force-less `git push` (no -f/--force/--force-with-lease), kubectl get/describe/logs/top/diff/events/auth/wait, helm template/diff/get/list, terraform plan, docker ps/images/logs/inspect, AWS CLI actions named get-*/describe-*/list-*/head-* (and plain `aws s3 ls`/`presign`), a plain `mv` that doesn't touch a credential-ish path (.ssh/.aws/.kube/.env/id_rsa/...) and isn't forced with -f, and cat/ls/grep/find(without -delete)/head/tail (incl. -f follow)/wc/echo/which/jq.
- "modify" = changes state: writes/edits/deletes files, redirects to a file (> or >>), a FORCED git push (-f/--force/--force-with-lease), git add/commit/reset/checkout/merge/rebase/stash, kubectl apply/create/delete/patch/scale/rollout/edit/exec/cp/attach/debug/run/port-forward, AWS CLI actions named create-*/put-*/delete-*/update-*/modify-*/run-*/terminate-*/tag-*/attach-* (or any shape you can't confidently place in the read list above), helm install/upgrade/uninstall, terraform apply/destroy, package installs, sudo, or piping into a shell.
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
