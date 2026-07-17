"""Find all git repositories under a root directory, tracking discoveries in a log file."""

import argparse
import os
from pathlib import Path

LOG_FILE = Path(__file__).parent / "found-repos.log"
SKIP = {".git", ".worktrees", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build"}


def load_known_repos() -> set[str]:
    if not LOG_FILE.exists():
        LOG_FILE.touch()
        return set()
    return {line.strip() for line in LOG_FILE.read_text().splitlines() if line.strip()}


def is_worktree(path: str) -> bool:
    return ".worktrees" in Path(path).parts


def find_repos(root: Path) -> list[Path]:
    repos = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        if is_worktree(dirpath):
            dirnames.clear()
            continue
        if ".git" in os.listdir(dirpath):
            repos.append(Path(dirpath))
            dirnames.clear()
    return sorted(repos)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find git repos recursively")
    parser.add_argument("root", nargs="?", default="C:/projects", help="Root directory to search")
    args = parser.parse_args()

    known = load_known_repos()
    repos = find_repos(Path(args.root))
    new_repos = [r for r in repos if str(r) not in known]

    if new_repos:
        with LOG_FILE.open("a") as f:
            for r in new_repos:
                f.write(f"{r}\n")
        print(f"Found {len(new_repos)} new repos (skipped {len(repos) - len(new_repos)} already known):\n")
        for r in new_repos:
            print(f"  {r}")
    else:
        print(f"No new repos found ({len(known)} already known).")
