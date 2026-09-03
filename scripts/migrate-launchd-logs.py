#!/usr/bin/env python3
"""Move agents-nexus launchd job logs off /tmp, which macOS purges."""

import argparse
import pathlib
import re
import shutil
import sys

HOME = pathlib.Path.home()
LOG_DIR = HOME / "Library" / "Logs" / "agents-nexus"
NEXUS = HOME / "garner" / "repos" / "agents-nexus"

PLIST_DIRS = [
    HOME / "Library" / "LaunchAgents",
    NEXUS / "launchd",
    NEXUS / "launchd" / "optional",
    NEXUS / "launchd" / "personal",
]

TMP_LOG = re.compile(r"/tmp/(?:agents-nexus-)?([A-Za-z0-9._-]+)\.log")


def rewrite(text: str, *, template: bool) -> str:
    """Templates keep __HOME__ so the Taskfile's sed owns the machine path."""
    root = "__HOME__/Library/Logs/agents-nexus" if template else str(LOG_DIR)
    return TMP_LOG.sub(lambda m: f"{root}/{m.group(1)}.log", text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    changed = []
    for d in PLIST_DIRS:
        if not d.is_dir():
            continue
        template = NEXUS in d.parents or d == NEXUS
        for plist in sorted(d.glob("*.plist")):
            original = plist.read_text()
            updated = rewrite(original, template=template)
            if updated == original:
                continue
            hits = sorted(set(TMP_LOG.findall(original)))
            changed.append((plist, hits))
            if not args.dry_run:
                shutil.copy2(plist, plist.with_suffix(".plist.pre-logmove"))
                plist.write_text(updated)

    verb = "would rewrite" if args.dry_run else "rewrote"
    for plist, hits in changed:
        print(f"  {verb} ~/{plist.relative_to(HOME)}  ({', '.join(hits)})")
    print(f"\n{verb}: {len(changed)} plist(s)")
    print(f"log dir: {LOG_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
