#!/usr/bin/env python3
"""agent-ledger.py — durable JSONL ledger of orchestrator-managed agents.

A single, dependency-free source of truth for "which agents the Slack
orchestrator spawned, and where their last checkpoint is." It is deliberately
INDEPENDENT of the slack-bridge: the overseer reaper (which must run without
Slack) appends `reap` events here, and the bridge appends `spawn`/`restore`
events and reads/reconciles state. Keeping it standalone is what lets both
callers share one implementation without the reaper depending on the bridge.

Storage: an append-only JSONL event log (default ~/.tmux/agent-ledger.jsonl,
override with $AGENT_LEDGER or --file). Current state is derived by replaying
the log — last event per repo key wins:

    spawn   -> live      (orchestrator launched an agent in a repo)
    restore -> live      (a dormant agent was respawned from its checkpoint)
    reap    -> dormant   (the reaper checkpointed-then-killed it)  [+checkpoint]
    gone    -> removed   (reconciled away: no live window backs this entry)

`reap` only records agents the ledger already tracks as live, so manually-started
agents the orchestrator never spawned are not turned into phantom dormant
entries. All writes take an flock so concurrent appends (bridge + reaper) and
compaction don't interleave.

Subcommands (all accept --file and --json where sensible):
    spawn    --repo R --name N [--seed S] [--pane P] [--slot N]
    restore  --repo R --name N [--pane P] [--slot N]
    reap     --name N [--repo R] [--checkpoint C] [--transcript T]
    gone     --repo R | --name N
    state    [--json]                      current entries (one per repo)
    list     --state {live,dormant} [--json]
    reconcile --registry-dir DIR [--json]  downgrade live entries with no window
    compact                                rewrite log to current state only
    get      --repo R [--json]
"""
import argparse
import json
import os
import sys
import time
from contextlib import contextmanager

try:
    import fcntl  # POSIX only; this stack is Linux/mac
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows fallback
    _HAVE_FCNTL = False


def ledger_path(args):
    if getattr(args, "file", None):
        return args.file
    return os.environ.get(
        "AGENT_LEDGER", os.path.join(os.path.expanduser("~"), ".tmux", "agent-ledger.jsonl")
    )


def _now():
    # Caller may pass AGENT_LEDGER_NOW for deterministic tests.
    override = os.environ.get("AGENT_LEDGER_NOW")
    return int(override) if override else int(time.time())


@contextmanager
def _locked(path, mode):
    """Open `path` with an flock held for the duration (best-effort)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 'a+' so the lock target exists even on first read.
    f = open(path, mode if "a" in mode or "w" in mode else "a+")
    try:
        if _HAVE_FCNTL:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        try:
            if _HAVE_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _read_events(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate a torn final line
    return out


def _key(ev):
    """Repo is the canonical key; fall back to name for older/edge entries."""
    return ev.get("repo") or ev.get("name") or ""


_STATE_OF = {"spawn": "live", "restore": "live", "reap": "dormant", "gone": "_gone"}


def _replay(events):
    """Collapse the event log to one current record per repo key (last wins)."""
    cur = {}
    for ev in events:
        k = _key(ev)
        if not k:
            continue
        state = _STATE_OF.get(ev.get("event"), None)
        if state is None:
            continue
        if state == "_gone":
            cur.pop(k, None)
            continue
        rec = dict(cur.get(k, {}))
        rec.update({kk: vv for kk, vv in ev.items() if vv is not None})
        rec["state"] = state
        rec["key"] = k
        cur[k] = rec
    return cur


def _append(path, record):
    record = {k: v for k, v in record.items() if v is not None}
    record.setdefault("ts", _now())
    with _locked(path, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return record


def _find_live(path, repo=None, name=None):
    cur = _replay(_read_events(path))
    if repo and repo in cur and cur[repo].get("state") == "live":
        return cur[repo]
    if name:
        for rec in cur.values():
            if rec.get("name") == name and rec.get("state") == "live":
                return rec
    if repo:  # repo given but not a live key — still report if present
        for rec in cur.values():
            if rec.get("repo") == repo and rec.get("state") == "live":
                return rec
    return None


# --------------------------------------------------------------------------- #
# Subcommand handlers
# --------------------------------------------------------------------------- #
def cmd_spawn(args):
    rec = _append(ledger_path(args), {
        "event": "spawn", "repo": args.repo, "name": args.name,
        "seed": args.seed, "pane": args.pane, "slot": args.slot,
    })
    print(json.dumps(rec, separators=(",", ":")))


def cmd_restore(args):
    rec = _append(ledger_path(args), {
        "event": "restore", "repo": args.repo, "name": args.name,
        "pane": args.pane, "slot": args.slot,
    })
    print(json.dumps(rec, separators=(",", ":")))


def cmd_reap(args):
    path = ledger_path(args)
    live = _find_live(path, repo=args.repo, name=args.name)
    if not live:
        # Not an orchestrator-tracked agent — record nothing, exit cleanly.
        print(json.dumps({"skipped": "no live ledger entry", "name": args.name, "repo": args.repo},
                         separators=(",", ":")))
        return
    rec = _append(path, {
        "event": "reap", "repo": live.get("repo") or args.repo, "name": live.get("name") or args.name,
        "checkpoint": args.checkpoint, "transcript": args.transcript,
    })
    print(json.dumps(rec, separators=(",", ":")))


def cmd_gone(args):
    rec = _append(ledger_path(args), {"event": "gone", "repo": args.repo, "name": args.name})
    print(json.dumps(rec, separators=(",", ":")))


def _registry_panes(registry_dir):
    """Set of (name, pane) currently present in the tmux registry."""
    names, panes = set(), set()
    if not os.path.isdir(registry_dir):
        return names, panes
    for fn in os.listdir(registry_dir):
        fp = os.path.join(registry_dir, fn)
        try:
            with open(fp) as f:
                d = dict(
                    line.strip().split("=", 1)
                    for line in f if "=" in line
                )
            if d.get("NAME"):
                names.add(d["NAME"].strip())
            if d.get("PANE_ID"):
                panes.add(d["PANE_ID"].strip())
        except (OSError, ValueError):
            continue
    return names, panes


def cmd_reconcile(args):
    path = ledger_path(args)
    names, panes = _registry_panes(args.registry_dir)
    cur = _replay(_read_events(path))
    for k, rec in list(cur.items()):
        if rec.get("state") != "live":
            continue
        pane = rec.get("pane")
        name = rec.get("name")
        # Live entry is only "real" if a registry window still backs it.
        backed = (pane and pane in panes) or (name and name in names)
        if not backed:
            _append(path, {"event": "gone", "repo": rec.get("repo"), "name": name,
                           "reason": "reconcile:no-window"})
    _emit_state(path, as_json=args.json)


def cmd_state(args):
    _emit_state(ledger_path(args), as_json=args.json)


def cmd_list(args):
    cur = _replay(_read_events(ledger_path(args)))
    rows = [r for r in cur.values() if r.get("state") == args.state]
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    if args.json:
        print(json.dumps(rows, separators=(",", ":")))
    else:
        for r in rows:
            print(f"{r.get('state'):8} {r.get('repo',''):24} {r.get('name','')}"
                  f"  ckpt={r.get('checkpoint','')}")


def cmd_get(args):
    cur = _replay(_read_events(ledger_path(args)))
    rec = cur.get(args.repo)
    if not rec:
        for r in cur.values():
            if r.get("repo") == args.repo:
                rec = r
                break
    if args.json:
        print(json.dumps(rec or {}, separators=(",", ":")))
    elif rec:
        print(f"{rec.get('state')} {rec.get('repo')} {rec.get('name')} ckpt={rec.get('checkpoint','')}")
    else:
        print("(not found)")


def cmd_compact(args):
    path = ledger_path(args)
    with _locked(path, "a+"):
        cur = _replay(_read_events(path))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            for rec in sorted(cur.values(), key=lambda r: r.get("ts", 0)):
                ev = "reap" if rec.get("state") == "dormant" else "spawn"
                out = {k: v for k, v in rec.items() if k not in ("state", "key")}
                out["event"] = ev
                f.write(json.dumps(out, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    print(json.dumps({"compacted": len(cur)}, separators=(",", ":")))


def _emit_state(path, as_json):
    cur = _replay(_read_events(path))
    rows = sorted(cur.values(), key=lambda r: r.get("ts", 0), reverse=True)
    if as_json:
        print(json.dumps(rows, separators=(",", ":")))
    else:
        for r in rows:
            print(f"{r.get('state'):8} {r.get('repo',''):24} {r.get('name','')}")


def build_parser():
    p = argparse.ArgumentParser(description="Durable JSONL ledger of orchestrator-managed agents.")
    p.add_argument("--file", help="ledger path (default $AGENT_LEDGER or ~/.tmux/agent-ledger.jsonl)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn"); sp.add_argument("--repo", required=True); sp.add_argument("--name", required=True)
    sp.add_argument("--seed"); sp.add_argument("--pane"); sp.add_argument("--slot"); sp.set_defaults(func=cmd_spawn)

    rs = sub.add_parser("restore"); rs.add_argument("--repo", required=True); rs.add_argument("--name", required=True)
    rs.add_argument("--pane"); rs.add_argument("--slot"); rs.set_defaults(func=cmd_restore)

    rp = sub.add_parser("reap"); rp.add_argument("--name", required=True); rp.add_argument("--repo")
    rp.add_argument("--checkpoint"); rp.add_argument("--transcript"); rp.set_defaults(func=cmd_reap)

    gn = sub.add_parser("gone"); gn.add_argument("--repo"); gn.add_argument("--name"); gn.set_defaults(func=cmd_gone)

    st = sub.add_parser("state"); st.add_argument("--json", action="store_true"); st.set_defaults(func=cmd_state)

    ls = sub.add_parser("list"); ls.add_argument("--state", choices=["live", "dormant"], required=True)
    ls.add_argument("--json", action="store_true"); ls.set_defaults(func=cmd_list)

    rc = sub.add_parser("reconcile"); rc.add_argument("--registry-dir", required=True)
    rc.add_argument("--json", action="store_true"); rc.set_defaults(func=cmd_reconcile)

    gt = sub.add_parser("get"); gt.add_argument("--repo", required=True)
    gt.add_argument("--json", action="store_true"); gt.set_defaults(func=cmd_get)

    cp = sub.add_parser("compact"); cp.set_defaults(func=cmd_compact)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
