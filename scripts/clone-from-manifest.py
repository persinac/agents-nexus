#!/usr/bin/env python3
"""Clone repos from repos-manifest.yaml into categorized directories.

Layout:
    <dest>/
    ├── example-repo/   # example-org tagged repos
    ├── example-org/        # example-org tagged repos
    ├── personal/          # owner: personal (everything else)
    └── community/         # owner: community (everything else)

Usage:
    uv run --with pyyaml scripts/clone-from-manifest.py [--manifest PATH] [--dest DIR] [--dry-run]
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml


def bucket_for(repo: dict) -> str:
    tags = set(repo.get("tags") or [])
    owner = repo.get("owner", "community")

    if "example-org" in tags:
        return "example-repo"
    if "example-org" in tags:
        return "example-org"
    if owner == "personal":
        return "personal"
    return "community"


def clone(url: str, target: Path, depth: int | None = None) -> bool:
    if target.exists():
        print(f"  [SKIP] {target.name} — already cloned")
        return False

    cmd = ["git", "clone"]
    if depth:
        cmd += ["--depth", str(depth)]
    cmd += [url, str(target)]

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if result.returncode != 0:
        print(f"  [FAIL] {target.name}: {result.stderr.strip()}")
        return False

    print(f"  [OK]   {target.name}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Clone repos from manifest into categorized dirs")
    parser.add_argument("--manifest", type=Path, default=Path("repos-manifest.yaml"))
    parser.add_argument("--dest", type=Path, default=Path.home() / "repos")
    parser.add_argument("--shallow", action="store_true", help="shallow clone community repos (--depth 1)")
    parser.add_argument("--dry-run", action="store_true", help="show plan without cloning")
    args = parser.parse_args()

    manifest = yaml.safe_load(args.manifest.read_text())

    buckets: dict[str, list[dict]] = {}
    for repo in manifest:
        b = bucket_for(repo)
        buckets.setdefault(b, []).append(repo)

    total = sum(len(v) for v in buckets.values())
    print(f"Manifest: {total} repos -> {list(buckets.keys())}\n")

    for bucket, repos in sorted(buckets.items()):
        dest_dir = args.dest / bucket
        print(f"[{bucket}] ({len(repos)} repos) -> {dest_dir}")

        if args.dry_run:
            for r in repos:
                target = dest_dir / r["name"]
                status = "exists" if target.exists() else "clone"
                print(f"  [{status}] {r['name']}")
            print()
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        depth = 1 if args.shallow and bucket == "community" else None
        for r in repos:
            clone(r["url"], dest_dir / r["name"], depth=depth)
        print()

    print("Done.")


if __name__ == "__main__":
    main()
