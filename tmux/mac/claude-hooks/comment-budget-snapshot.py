#!/usr/bin/env python3
"""PreToolUse hook: snapshot a file's content before Write overwrites it.

comment-budget.py runs PostToolUse, by which point the pre-write content is gone.
Without it, Write is scored as 100% added lines, so a rewrite that DELETES comments
is reported as adding them. Edit/MultiEdit need no snapshot — their old_string is
in the payload.

Snapshots land in a temp dir keyed by a hash of the path. comment-budget.py consumes
and deletes each one; this hook sweeps anything older than SWEEP_AGE in case a Write
was denied and no PostToolUse ever ran.

Fails OPEN, always.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time

SWEEP_AGE = 3600.0


def snap_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "cc-comment-budget")
    os.makedirs(d, exist_ok=True)
    return d


def snap_path(file_path: str) -> str:
    key = hashlib.sha256(os.path.realpath(file_path).encode()).hexdigest()[:32]
    return os.path.join(snap_dir(), f"{key}.snap")


def sweep(d: str) -> None:
    cutoff = time.time() - SWEEP_AGE
    for name in os.listdir(d):
        if not name.endswith(".snap"):
            continue
        p = os.path.join(d, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.unlink(p)
        except OSError:
            pass


def main() -> int:
    if os.environ.get("CC_COMMENT_OFF") == "1":
        return 0

    payload = json.load(sys.stdin)
    if (payload.get("tool_name") or "") != "Write":
        return 0

    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path or not os.path.isfile(path):
        return 0
    if os.path.getsize(path) > 4_000_000:
        return 0

    d = snap_dir()
    sweep(d)
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    with open(snap_path(path), "w", encoding="utf-8") as fh:
        fh.write(content)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
