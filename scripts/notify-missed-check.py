#!/usr/bin/env python3
"""Prompts that reached a human when the gate should have cleared them. Exits 1 on findings."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_PY = os.path.join(HERE, "classify-audit.py")

JOIN_SLOP = 2


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pending_at(transcript, epoch):
    """The tool_use awaiting a decision at `epoch` -> (when, id, name, input), anchored on
    the absent-or-later tool_result: a waiting prompt is older than the notifications for it."""
    if not transcript or not os.path.exists(transcript):
        return None, "no-transcript", set()
    uses, results = [], {}
    try:
        with open(transcript, errors="replace") as fh:
            for ln in fh:
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                try:
                    when = dt.datetime.fromisoformat(
                        (obj.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if msg.get("role") == "assistant" and b.get("type") == "tool_use":
                        uses.append((when, b.get("id") or "", b.get("name") or "",
                                     b.get("input") or {}))
                    elif b.get("type") == "tool_result":
                        rid = b.get("tool_use_id") or ""
                        if rid and rid not in results:
                            results[rid] = when
    except OSError:
        return None, "unreadable", set()
    uses.sort()
    names = {u[2] for u in uses}
    started = [u for u in uses if u[0] <= epoch + JOIN_SLOP]
    if not started:
        return None, "none-started", names
    for u in reversed(started):
        done = results.get(u[1])
        if done is None or done > epoch:
            return u, "boundary", names
    return started[-1], "no-boundary", names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    audit = _load(AUDIT_PY, "classify_audit_missed")
    mod, classify_path = audit._load_classifier()          # LLM already stubbed to None

    since = dt.datetime.now().timestamp() - args.hours * 3600
    asked = audit._read_asked(since)
    notifs = audit._parse_notifications(since)

    by_pane = collections.defaultdict(list)
    for n in notifs:
        by_pane[n["pane"]].append(n)

    rows, seen, synthetic = [], set(), 0
    for epoch, pane, kind, body in asked:
        transcript, session = "", ""
        for n in by_pane.get(pane, ()):
            if abs(n["epoch"] - epoch) <= JOIN_SLOP:
                transcript, session = n["transcript"], n.get("session", "")
                break
        # A hook fed a hand-made payload, not a real prompt. It has no resolvable tool, so
        # it would otherwise land as a `no-tool` finding and alert the cron.
        if transcript in ("/dev/null", "-") or session == "probe":
            synthetic += 1
            continue
        pending, how, seen_names = _pending_at(transcript, epoch)
        reported = (body.get("tool") or "").strip()
        real_name = pending[2] if pending else ""
        real_inp = pending[3] if pending else {}

        verdict = ""
        if pending:
            try:
                verdict = mod.classify(real_name, real_inp)[0]
            except Exception:
                verdict = "error"

        if not reported:
            outcome = "no-tool"
        elif real_name and reported != real_name and reported not in seen_names:
            # A subagent's calls are absent from the parent transcript, so this file is not
            # evidence about them. Only a reported tool that IS here, already finished, is
            # the stale-read bug; anything else is outside what the transcript can settle.
            outcome = "no-coverage"
        elif real_name and reported != real_name:
            outcome = "wrong-tool"
        elif verdict == "read":
            outcome = "would-clear"
        else:
            outcome = "ok"

        key = (pane, pending[1] if pending else epoch, outcome)
        if key in seen:
            continue
        seen.add(key)

        cmd = str(real_inp.get("command") or real_inp.get("url")
                  or real_inp.get("file_path") or "")
        rows.append({
            "ts": dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": epoch, "pane": pane, "kind": kind,
            "agent": body.get("name") or "",
            "reported_tool": reported or "-",
            "real_tool": real_name or "-",
            "real_detail": cmd[:200],
            "pending_age_s": round(epoch - pending[0], 1) if pending else None,
            "anchor": how,
            "deterministic_verdict": verdict or "-",
            "outcome": outcome,
        })

    findings = [r for r in rows if r["outcome"] not in ("ok", "no-coverage")]

    if args.json:
        print(json.dumps({
            "classifier": classify_path,
            "window_hours": args.hours,
            "asked": len(rows),
            "synthetic_skipped": synthetic,
            "findings": len(findings),
            "by_outcome": dict(collections.Counter(r["outcome"] for r in rows)),
            "rows": findings,
        }, indent=2))
        return 1 if findings else 0

    print(f"classifier: {classify_path}")
    print(f"window:     last {args.hours}h   prompts that reached a human: {len(rows)}"
          + (f"   ({synthetic} synthetic probe row(s) skipped)" if synthetic else ""))
    counts = collections.Counter(r["outcome"] for r in rows)
    for k in ("ok", "no-coverage", "would-clear", "no-tool", "wrong-tool"):
        if counts.get(k):
            print(f"  {k:<12} {counts[k]}")
    if not findings:
        print("\nno findings — every prompt that reached a human was owed one.")
        return 0
    print(f"\nfindings ({min(args.limit, len(findings))} of {len(findings)}):")
    for r in findings[-args.limit:]:
        print(f"  {r['ts']}  {r['pane']:<8} {r['outcome']:<11} "
              f"reported={r['reported_tool'][:22]:<22} real={r['real_tool'][:14]:<14} "
              f"verdict={r['deterministic_verdict']:<7} age={r['pending_age_s']}s")
        if r["real_detail"]:
            print(f"      {r['real_detail'][:150]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
