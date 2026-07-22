#!/usr/bin/env python3
"""timers-status.py — launchd (macOS) scheduled-job status for the herdr command-center panel.

For each com.agents-nexus.* LaunchAgent, prints one line:
    <dot> <label>   <schedule>   last <when> <ok/FAIL>   — <purpose>

Sources (all read-only):
  - schedule   : the plist's StartCalendarInterval / StartInterval / KeepAlive (plistlib)
  - loaded     : `launchctl list` contains the label
  - last exit  : `launchctl list <label>` → LastExitStatus (0 = ok)
  - last run   : mtime of the plist's StandardOutPath log (best-effort proxy)
  - purpose    : launchd/descriptions.json

stdlib only. Fails soft per-field (missing plist key / log / launchctl entry → blank).
Usage: timers-status.py [descriptions.json path]  (defaults to ../../launchd/descriptions.json)
"""
import os
import sys
import glob
import json
import plistlib
import subprocess
from datetime import datetime, timezone

LA_DIR = os.path.expanduser("~/Library/LaunchAgents")
PREFIX = "com.agents-nexus."

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # launchd Weekday: 0/7=Sun,1=Mon…6=Sat


def descriptions(path):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def fmt_schedule(pl):
    """Human schedule string from a launchd plist dict."""
    if "KeepAlive" in pl or pl.get("RunAtLoad") and "StartInterval" not in pl and "StartCalendarInterval" not in pl:
        # KeepAlive daemons aren't scheduled jobs
        if "KeepAlive" in pl:
            return "daemon (keepalive)"
    if "StartInterval" in pl:
        s = int(pl["StartInterval"])
        if s % 3600 == 0:
            return f"every {s // 3600}h"
        if s % 60 == 0:
            return f"every {s // 60}m"
        return f"every {s}s"
    sci = pl.get("StartCalendarInterval")
    if sci:
        entries = sci if isinstance(sci, list) else [sci]
        # collect distinct HH:MM and the set of weekdays
        times, days, has_dom = set(), set(), False
        for e in entries:
            h = e.get("Hour"); m = e.get("Minute", 0)
            if h is not None:
                times.add(f"{h:02d}:{m:02d}")
            wd = e.get("Weekday")
            if wd is not None:
                days.add(int(wd) % 7)   # 0/7 → Sun
            if "Day" in e:
                has_dom = True
        t = sorted(times)
        tstr = t[0] if len(t) == 1 else (",".join(t) if t else "?")
        if len(days) == 5 and days == {1, 2, 3, 4, 5}:
            return f"weekdays {tstr}"
        if days:
            # map python idx (0=Sun) to label; our DOW is Mon-first, so index carefully
            names = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
            return f"{','.join(names[d] for d in sorted(days))} {tstr}"
        if has_dom:
            return f"monthly {tstr}"
        return f"daily {tstr}"
    if pl.get("RunAtLoad"):
        return "at load"
    return "?"


def launchctl_info(label):
    """(loaded, last_exit) from `launchctl list <label>`. last_exit None if unknown."""
    try:
        out = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False, None
    if not out.strip():
        return False, None
    last = None
    for line in out.splitlines():
        if "LastExitStatus" in line:
            digits = "".join(c for c in line.split("=", 1)[-1] if c.isdigit() or c == "-")
            try:
                last = int(digits)
            except Exception:
                pass
    return True, last


def ago(ts):
    if not ts:
        return ""
    secs = int((datetime.now(timezone.utc) - datetime.fromtimestamp(ts, timezone.utc)).total_seconds())
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def main():
    desc_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "launchd", "descriptions.json")
    desc = descriptions(desc_path)

    plists = sorted(glob.glob(os.path.join(LA_DIR, PREFIX + "*.plist")))
    if not plists:
        print("  (none installed)")
        return

    for p in plists:
        full = os.path.basename(p)[:-6]           # strip .plist
        label = full[len(PREFIX):]
        try:
            pl = plistlib.load(open(p, "rb"))
        except Exception:
            pl = {}
        loaded, last_exit = launchctl_info(full)
        sched = fmt_schedule(pl)

        # last run: log mtime (best-effort)
        last_run = ""
        logp = pl.get("StandardOutPath") or pl.get("StandardErrorPath")
        if logp and os.path.exists(logp):
            last_run = ago(os.path.getmtime(logp))

        # status glyph: ● loaded / ○ not loaded; result from last_exit
        dot = "●" if loaded else "○"
        if last_exit is None:
            res = ""
        elif last_exit == 0:
            res = "ok"
        else:
            res = f"FAIL({last_exit})"

        parts = [f"  {dot} {label:<24} {sched:<16}"]
        tail = " ".join(x for x in (f"last {last_run}" if last_run else "", res) if x)
        if tail:
            parts.append(f" {tail}")
        d = desc.get(full, "")
        if d:
            parts.append(f"  — {d}")
        if not loaded:
            parts.append("  [not loaded]")
        print("".join(parts))


if __name__ == "__main__":
    main()
