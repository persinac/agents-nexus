#!/usr/bin/env python3
"""Audit what notify-classify.py actually did: which permission prompts it cleared and,
more importantly, which ones still reached a human — with the command text attached.

Neither log alone answers that. `~/.tmux/notification-debug.log` records EVERY
notification (before the classifier runs) but not the tool call or the verdict;
`~/.tmux/auto-approve.log` records only the approvals, by pane and epoch. Joining them
on (pane, time) gives the withheld set, and the pending tool_use is then recovered from
the session transcript the notification names.

The verdict is RE-DERIVED by importing notify-classify and calling classify() with the
LLM tier stubbed out, so the reported reason is the deterministic one. Prompts that the
live run sent to the model are reported as `llm-tier` rather than guessed at.

Usage:
  python3 scripts/classify-audit.py                 # whole notification-debug window
  python3 scripts/classify-audit.py --hours 24
  python3 scripts/classify-audit.py --json          # machine-readable
  python3 scripts/classify-audit.py --limit 40      # withheld examples to print
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import importlib.util
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
NOTIF_LOG = os.path.join(HOME, ".tmux", "notification-debug.log")
APPROVE_LOG = os.path.join(HOME, ".tmux", "auto-approve.log")
# Written by hook-notification.sh from 2026-08-19: one line per prompt that reached a
# human, carrying the tool and the classifier's own verdict. Preferred over the legacy
# join whenever it covers the window — it is ground truth rather than inference.
ASKED_LOG = os.path.join(HOME, ".tmux", "notify-asked.log")
REPEAT_LOG = os.path.join(HOME, ".tmux", "notify-repeat.log")
CLASSIFY_PY = os.path.join(HOME, ".tmux", "notify-classify.py")   # symlink into the repo
# Fall back to the repo copy if the installed one is missing.
REPO_CLASSIFY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "tmux", "mac", "tmux-scripts", "notify-classify.py")

# Join window: the hook writes the debug line, then spawns the classifier, then writes
# auto-approve.log. The classifier's own LLM call can take seconds, so allow a generous
# window on the late side and a small one on the early side (clock granularity is 1s).
JOIN_EARLY, JOIN_LATE = 2, 25

_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[([^\]]+)\] type=(\S+) prompt=(\S*) raw=(.*)$")


def _load_classifier():
    path = CLASSIFY_PY if os.path.exists(CLASSIFY_PY) else os.path.abspath(REPO_CLASSIFY)
    spec = importlib.util.spec_from_file_location("notify_classify_audit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Stub the LLM: this is an offline audit and must never spend tokens or vary run to
    # run. classify() returns "modify" with a None llm, which is exactly the fail-safe
    # path — so a reported `llm-tier` means "the live run asked the model", not "denied".
    mod._llm = lambda name, inp: None
    return mod, path


def _parse_notifications(since_epoch):
    """Yield dicts for every permission_prompt in the debug log at/after since_epoch."""
    out = []
    with open(NOTIF_LOG, errors="replace") as fh:
        for ln in fh:
            m = _LINE.match(ln.rstrip("\n"))
            if not m:
                continue
            ts, pane, ntype, _prompt, raw = m.groups()
            if ntype != "permission_prompt":
                continue
            epoch = dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
            if since_epoch and epoch < since_epoch:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            out.append({
                "ts": ts, "epoch": epoch, "pane": pane,
                "transcript": payload.get("transcript_path", ""),
                "cwd": payload.get("cwd", ""),
                "session": payload.get("session_id", ""),
            })
    return out


def _parse_approvals():
    """[(epoch, verb, pane)] from auto-approve.log."""
    out = []
    with open(APPROVE_LOG, errors="replace") as fh:
        for ln in fh:
            parts = ln.split()
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            out.append((int(parts[0]), parts[1], parts[2]))
    return out


def _index_approvals(approvals):
    by_pane = collections.defaultdict(list)
    for epoch, verb, pane in approvals:
        by_pane[pane].append((epoch, verb))
    for v in by_pane.values():
        v.sort()
    return by_pane


def _match_approval(by_pane, notif, used):
    """Find an unconsumed approval for this pane near this notification's time."""
    for i, (epoch, verb) in enumerate(by_pane.get(notif["pane"], ())):
        if (notif["pane"], i) in used:
            continue
        if -JOIN_EARLY <= epoch - notif["epoch"] <= JOIN_LATE:
            used.add((notif["pane"], i))
            return verb
    return None


def _tool_at(transcript, epoch, window=7200):
    """The pending tool_use for a notification: the last tool_use written at or before
    the notification, within `window` seconds. Mirrors notify-classify._last_tool_use,
    but time-anchored instead of tail-anchored, since this runs long after the fact.

    `window` is deliberately generous. Claude Code RE-EMITS the Notification every ~2
    minutes while a prompt sits unanswered, so a notification can be far newer than the
    tool_use it belongs to — an unattended prompt that waits an hour produces ~30
    notifications, all for the same call. A tight window reported those as
    "no-tool-found" and made the withheld set look ~30% larger than the number of
    decisions actually owed. The anchor timestamp is returned so repeats collapse."""
    if not transcript or not os.path.exists(transcript):
        return None
    best = None
    try:
        with open(transcript, errors="replace") as fh:
            for ln in fh:
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                raw_ts = obj.get("timestamp") or ""
                try:
                    when = dt.datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if when > epoch + 2 or when < epoch - window:
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        if best is None or when >= best[0]:
                            best = (when, b.get("name") or "", b.get("input") or {})
    except OSError:
        return None
    return best          # (when, name, input) or None


def _reason(mod, name, inp):
    """Why the deterministic tiers withheld this call. Recomputes the same order
    classify() uses, so the label names the clause that actually decided."""
    short = (name or "").split("__")[-1].lower()
    if short in mod.READ_TOOLS or short in getattr(mod, "INERT_TOOLS", ()):
        # classify() would clear this. Reaching the withheld set means either the live
        # run predates the current code, or the (pane, time) join missed its approval.
        return "would-clear-now"
    if short in mod.SURFACE_TOOLS:
        return "surface-tool"
    if short in ("bash", "shell"):
        cmd = (inp.get("command") or "").strip()
        if not cmd:
            return "empty-command"
        if mod._DENY.search(cmd):
            return "deny"
        if mod._DESTRUCTIVE.search(cmd):
            return "destructive"
        return "llm-tier"          # permissive tier is on, so this only happens if...
    if short in ("edit", "multiedit", "write", "notebookedit"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if not path:
            return "edit-no-path"
        if mod._MV_SENSITIVE.search(path):
            return "credential-path"
        return "llm-tier"
    if name.startswith("mcp__"):
        return "mcp-read" if mod._mcp_is_read(name, inp) else "mcp-write"
    return f"tool:{short or '?'}"


def _read_asked(since_epoch):
    """[(epoch, pane, ntype, body)] from notify-asked.log — prompts that reached a human."""
    out = []
    if not os.path.exists(ASKED_LOG):
        return out
    with open(ASKED_LOG, errors="replace") as fh:
        for ln in fh:
            parts = ln.split(None, 3)
            if len(parts) < 4 or not parts[0].isdigit():
                continue
            epoch = int(parts[0])
            if since_epoch and epoch < since_epoch:
                continue
            try:
                body = json.loads(parts[3])
            except Exception:
                body = {}
            out.append((epoch, parts[1], parts[2], body))
    return out


def _count_since(path, since_epoch):
    n = 0
    if not os.path.exists(path):
        return 0
    with open(path, errors="replace") as fh:
        for ln in fh:
            parts = ln.split()
            if parts and parts[0].isdigit() and (not since_epoch or int(parts[0]) >= since_epoch):
                n += 1
    return n


def _head(cmd):
    """A coarse command head for aggregation."""
    cmd = (cmd or "").strip()
    if not cmd:
        return "?"
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        toks = seg.strip().split()
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]
        if toks:
            return os.path.basename(toks[0])[:28]
    return "?"


def _report_asked(asked, since, args, classify_path):
    """Report from notify-asked.log: exact, no join, no transcript re-derivation."""
    # The cleared count and the asked count must span the SAME window, or the ratio is
    # meaningless: auto-approve.log reaches back months, while notify-asked.log starts the
    # day it was added. With no explicit --since, the window starts at the asked-log's
    # first line.
    win_start = since or asked[0][0]
    cleared = _count_since(APPROVE_LOG, win_start)
    suppressed = _count_since(REPEAT_LOG, win_start)
    rows = [{
        "ts": dt.datetime.fromtimestamp(e).strftime("%Y-%m-%d %H:%M:%S"),
        "pane": pane, "kind": kind,
        "tool": body.get("tool") or "?",
        "category": body.get("category") or "?",
        "summary": body.get("summary") or "",
        # The literal command for Bash, secret-redacted by the classifier. Present from
        # 2026-08-19; older lines have only the paraphrase, hence the fallback.
        "detail": body.get("detail") or "",
        "agent": body.get("name") or "",
    } for e, pane, kind, body in asked]
    for r in rows:
        is_bash = r["tool"].lower() == "bash"
        r["head"] = _head((r["detail"] or r["summary"]).strip("`")) if is_bash else ""
        r["what"] = (r["detail"] if is_bash and r["detail"] else r["summary"]).strip("`")

    total = cleared + len(rows)
    if args.json:
        print(json.dumps({"source": "notify-asked.log", "cleared": cleared,
                          "asked": len(rows), "suppressed_repeats": suppressed,
                          "rows": rows}, indent=2))
        return 0
    print(f"classifier: {classify_path}")
    print(f"source:     {ASKED_LOG} (exact)")
    print(f"window:     {rows[0]['ts']} .. {rows[-1]['ts']}")
    pct = 100.0 * cleared / total if total else 0.0
    print(f"cleared:    {cleared} of {total} decisions ({pct:.1f}%)"
          f"   reached a human: {len(rows)}")
    print(f"            {suppressed} duplicate alerts suppressed (repeat notifications)")

    def hist(key, title, n=14):
        c = collections.Counter(r[key] for r in rows if r[key])
        if not c:
            return
        print(f"\n{title}")
        for k, v in c.most_common(n):
            print(f"  {v:5d}  {k}")

    hist("tool", "reached a human, by tool")
    hist("category", "reached a human, by classifier category")
    hist("head", "Bash command heads")
    hist("agent", "by agent")

    print(f"\nmost recent ({min(args.limit, len(rows))} of {len(rows)}):")
    for r in rows[-args.limit:]:
        print(f"  {r['ts']}  {r['pane']:<8} {r['tool'][:34]:<34} {r['what'][:96]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0, help="only the last N hours")
    ap.add_argument("--since", default="", help="only at/after this local 'YYYY-MM-DD HH:MM:SS'")
    ap.add_argument("--limit", type=int, default=25, help="withheld examples to print")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--legacy", action="store_true",
                    help="force the notification-debug + auto-approve join (pre-2026-08-19 windows)")
    args = ap.parse_args()

    if args.since:
        since = dt.datetime.strptime(args.since.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    elif args.hours:
        since = dt.datetime.now().timestamp() - args.hours * 3600
    else:
        since = 0
    mod, classify_path = _load_classifier()

    # Preferred path: the asked-log is exact. Used whenever it has lines in the window;
    # the legacy join below stays for windows that predate it (2026-08-19).
    asked = _read_asked(since)
    if asked and not args.legacy:
        return _report_asked(asked, since, args, classify_path)

    notifs = _parse_notifications(since)
    if not notifs:
        print("no permission_prompt notifications in window", file=sys.stderr)
        return 1
    by_pane = _index_approvals(_parse_approvals())

    used = set()
    approved = surfaced = withheld = repeats = unresolved = 0
    rows = []
    anchors = {}                 # (pane, transcript, tool_use ts) -> row index
    for n in notifs:
        verb = _match_approval(by_pane, n, used)
        if verb == "auto-approve":
            approved += 1
            continue
        if verb == "auto-approve-surface":
            surfaced += 1
        withheld += 1
        tool = _tool_at(n["transcript"], n["epoch"])
        if not tool:
            unresolved += 1
            rows.append({"ts": n["ts"], "pane": n["pane"], "tool": "?",
                         "reason": "no-tool-found", "verdict": "modify", "head": "",
                         "detail": "", "surfaced": False, "notifs": 1,
                         "repo": os.path.basename(n["cwd"] or "")})
            continue
        when, name, inp = tool
        key = (n["pane"], n["transcript"], when)
        if key in anchors:
            # Same pending call, re-notified while it waited. One decision, not two.
            rows[anchors[key]]["notifs"] += 1
            repeats += 1
            continue
        anchors[key] = len(rows)
        rows.append({
            "ts": n["ts"], "pane": n["pane"], "tool": name or "?",
            "reason": _reason(mod, name, inp),
            "verdict": mod.classify(name, inp)[0],
            "head": _head(inp.get("command")) if name.lower().endswith("bash") else "",
            "detail": mod._deterministic_summary(name, inp),
            "surfaced": verb == "auto-approve-surface",
            "notifs": 1,
            "repo": os.path.basename(n["cwd"] or ""),
        })

    total = len(notifs)
    decisions = len(rows)
    if args.json:
        print(json.dumps({"total": total, "approved": approved, "withheld": withheld,
                          "decisions": decisions, "repeats": repeats,
                          "surfaced": surfaced, "rows": rows}, indent=2))
        return 0

    span = f"{notifs[0]['ts']} .. {notifs[-1]['ts']}"
    print(f"classifier: {classify_path}")
    print(f"window:     {span}   ({total} permission prompts)")
    pct = 100.0 * approved / total
    print(f"cleared:    {approved} ({pct:.1f}%)   withheld: {withheld} notifications"
          f" = {decisions} distinct decisions + {repeats} repeat notifications")
    if surfaced:
        print(f"            {surfaced} cleared-then-surfaced (AskUserQuestion)")
    if unresolved:
        print(f"            {unresolved} could not be tied to a tool_use")
    noisiest = sorted(rows, key=lambda r: -r["notifs"])[:3]
    if noisiest and noisiest[0]["notifs"] > 1:
        print("  worst repeat offenders (one decision, many alerts):")
        for r in noisiest:
            if r["notifs"] > 1:
                print(f"    {r['notifs']:3d}x  {r['pane']:<8} {r['tool']:<40} first seen {r['ts']}")

    def hist(key, title, n=12):
        c = collections.Counter(r[key] for r in rows if r[key])
        if not c:
            return
        print(f"\n{title}")
        for k, v in c.most_common(n):
            print(f"  {v:5d}  {k}")

    hist("reason", "withheld by reason")
    hist("tool", "withheld by tool")
    hist("head", "withheld Bash command heads")
    hist("repo", "withheld by cwd")

    print(f"\nmost recent withheld ({min(args.limit, len(rows))} of {len(rows)}):")
    for r in rows[-args.limit:]:
        flag = "S" if r["surfaced"] else " "
        print(f"  {flag} {r['ts']}  {r['pane']:<8} {r['reason']:<14} {r['tool']:<22} {r['detail'][:96]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
