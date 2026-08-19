#!/usr/bin/env python3
"""Follow ~/.tmux/notify-asked.log and print one compact line per prompt that reached a
human. Built for the Monitor tool (each stdout line becomes one notification), but it is
just as usable in a terminal.

Only prompts the classifier could NOT clear appear here — that is the whole point. A quiet
stream means the gate is doing its job; a burst of the same tool means there is a rule to
add. Pair it with `python3 scripts/classify-audit.py` for the aggregate view.

  python3 scripts/notify-watch.py                  # follow (default)
  python3 scripts/notify-watch.py --tail 20        # last 20, then follow
  python3 scripts/notify-watch.py --once           # print recent and exit
  python3 scripts/notify-watch.py --repeats        # also report suppressed duplicates
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
ASKED_LOG = os.path.join(HOME, ".tmux", "notify-asked.log")
REPEAT_LOG = os.path.join(HOME, ".tmux", "notify-repeat.log")


def _fmt(line):
    """One log line -> one compact event line, or None if unparseable."""
    parts = line.split(None, 3)
    if len(parts) < 4 or not parts[0].isdigit():
        return None
    epoch, pane, kind = int(parts[0]), parts[1], parts[2]
    try:
        body = json.loads(parts[3])
    except Exception:
        body = {}
    when = dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
    agent = body.get("name") or "?"
    tool = (body.get("tool") or "?").split("__")[-1]
    cat = body.get("category") or kind
    summary = " ".join(str(body.get("summary") or "").split())[:160]
    return f"ASK {when} {agent}[{pane}] {tool} · {cat} · {summary}"


def _fmt_repeat(line):
    parts = line.split()
    if len(parts) < 3 or not parts[0].isdigit():
        return None
    when = dt.datetime.fromtimestamp(int(parts[0])).strftime("%H:%M:%S")
    return f"DUP {when} {parts[2]} · duplicate alert suppressed"


def _tail_lines(path, n):
    if n <= 0 or not os.path.exists(path):
        return []
    with open(path, errors="replace") as fh:
        return fh.readlines()[-n:]


class Follower:
    """Line-follower that survives the log being rotated out from under it."""

    def __init__(self, path, fmt, from_end=True):
        self.path, self.fmt, self.fh, self.ino = path, fmt, None, None
        self.from_end = from_end

    def _open(self):
        try:
            fh = open(self.path, errors="replace")
        except OSError:
            return
        self.fh, self.ino = fh, os.fstat(fh.fileno()).st_ino
        if self.from_end:
            fh.seek(0, os.SEEK_END)
        self.from_end = True          # only the first open honours --tail's position

    def poll(self):
        """Emit any new lines. Reopens on rotation (new inode) or truncation."""
        if self.fh is None:
            self._open()
            if self.fh is None:
                return
        for line in self.fh:
            out = self.fmt(line)
            if out:
                print(out, flush=True)
        try:
            st = os.stat(self.path)
            if st.st_ino != self.ino or st.st_size < self.fh.tell():
                self.fh.close()
                self.fh, self.from_end = None, False
        except OSError:
            self.fh = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", type=int, default=0, help="print the last N entries first")
    ap.add_argument("--once", action="store_true", help="print --tail entries and exit")
    ap.add_argument("--repeats", action="store_true", help="also report suppressed duplicates")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    for line in _tail_lines(ASKED_LOG, args.tail):
        out = _fmt(line)
        if out:
            print(out, flush=True)
    if args.once:
        return 0
    if not os.path.exists(ASKED_LOG):
        print(f"waiting for {ASKED_LOG}", flush=True)

    watchers = [Follower(ASKED_LOG, _fmt)]
    if args.repeats:
        watchers.append(Follower(REPEAT_LOG, _fmt_repeat))
    while True:
        for w in watchers:
            w.poll()
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
