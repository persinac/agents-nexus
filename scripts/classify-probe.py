#!/usr/bin/env python3
"""Probe notify-classify against concrete tool calls and report the verdict + which
denylist clause (if any) fired. Run after editing the classifier, or when a real prompt
reached a human and you want to know why.

  python3 scripts/classify-probe.py              # the built-in regression set
  python3 scripts/classify-probe.py --live        # also exercise the LLM tier (spends tokens)
  python3 scripts/classify-probe.py --bash 'cmd'  # one ad-hoc command
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
DEFAULT = os.path.join(HOME, ".tmux", "notify-classify.py")
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "tmux", "mac", "tmux-scripts", "notify-classify.py")


def load(live):
    path = DEFAULT if os.path.exists(DEFAULT) else os.path.abspath(REPO)
    spec = importlib.util.spec_from_file_location("nc_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not live:
        mod._llm = lambda name, inp: None
    return mod, path


def which_clause(rx, cmd):
    """Name the alternation branch of a compiled denylist that matched `cmd`."""
    if not rx.search(cmd):
        return ""
    for branch in re.split(r"\|(?![^\[\]]*\])", rx.pattern):
        b = branch.strip().strip("()")
        if not b:
            continue
        try:
            if re.search(b, cmd, re.I):
                return b
        except re.error:
            continue
    return "<matched, branch unresolved>"


# name -> input. Bash cases are strings for brevity.
BASH_CASES = [
    # measured withheld 2026-08-19 — each is a question about whether it SHOULD have been
    'curl -s --max-time 10 localhost:8788/agents',
    'sed -n \'365,395p\' /tmp/ui-member-release/upload-translations.log',
    'for w in o11y-merge o11y-dryrun; do git worktree remove --force "/tmp/$w"; done',
    'pkill -f "node /tmp/h2_proxy.mjs" 2>/dev/null; sleep 1; nc -z 127.0.0.1 6443',
    'rm -f /tmp/sse-jwt.txt',
    # guardrails that must KEEP asking
    'kubectl delete pod foo',
    'DELETE FROM members WHERE id = 1',
    'terraform destroy',
    'git push --force origin main',
    'sudo rm -rf /',
    'doppler secrets set FOO=bar',
]

MCP_CASES = [
    ("mcp__atlassian__getConfluencePage", {"pageId": "12345"}),
    ("mcp__atlassian__updateConfluencePage", {"pageId": "12345", "body": "x"}),
    ("mcp__plugin_slack_slack__slack_search_public", {"query": "in:#releases"}),
    ("mcp__plugin_slack_slack__slack_send_message", {"channel": "C1", "text": "hi"}),
    ("mcp__plugin_slack_slack__slack_read_user_profile", {"user": "U1"}),
    ("mcp__bifrost__grafana-list_oncall_schedules", {}),
    ("mcp__bifrost__grafana-grafana_api_request", {"method": "GET", "path": "/api/x"}),
    ("mcp__datadog__search_datadog_logs", {"query": "service:x"}),
    ("mcp__agent-memory__create_note", {"content": "x", "project": "p"}),
    ("mcp__atlassian__transitionJiraIssue", {"issueIdOrKey": "FC-1", "transition": {}}),
    ("ToolSearch", {"query": "select:Read", "max_results": 5}),
    ("Skill", {"skill": "mr-brief"}),
    ("WebFetch", {"url": "https://example.com"}),
    ("WebSearch", {"query": "x"}),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="allow real LLM calls")
    ap.add_argument("--bash", default="", help="classify a single command and exit")
    args = ap.parse_args()
    mod, path = load(args.live)
    print(f"classifier: {path}   permissive={mod._PERMISSIVE}   llm={'live' if args.live else 'stubbed'}")

    if args.bash:
        t = time.time()
        d, c, s = mod.classify("Bash", {"command": args.bash})
        print(f"{d:<7} {c:<24} {(time.time()-t)*1000:6.0f}ms  {s[:100]}")
        for label, rx in (("_DENY", mod._DENY), ("_DESTRUCTIVE", mod._DESTRUCTIVE)):
            hit = which_clause(rx, args.bash)
            if hit:
                print(f"  {label} matched: {hit}")
        return 0

    print("\n--- Bash ---")
    for cmd in BASH_CASES:
        t = time.time()
        d, c, _ = mod.classify("Bash", {"command": cmd})
        ms = (time.time() - t) * 1000
        mark = "CLEAR" if d == "read" else "ASK  "
        print(f"  {mark} {c:<22} {ms:6.0f}ms  {cmd[:74]}")
        for label, rx in (("_DENY", mod._DENY), ("_DESTRUCTIVE", mod._DESTRUCTIVE)):
            hit = which_clause(rx, cmd)
            if hit:
                print(f"        {label}: {hit}")

    print("\n--- MCP / other ---")
    for name, inp in MCP_CASES:
        t = time.time()
        d, c, _ = mod.classify(name, inp)
        ms = (time.time() - t) * 1000
        mark = "CLEAR" if d == "read" else "ASK  "
        print(f"  {mark} {c:<22} {ms:6.0f}ms  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
