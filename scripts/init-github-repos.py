"""For each ungit project, initialize git and create a private GitHub repo."""

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOG_FILE = SCRIPT_DIR / "found-ungit-projects.log"
URLS_FILE = SCRIPT_DIR / "clone-urls.txt"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)


def has_gh() -> bool:
    try:
        subprocess.run(["gh", "--version"], capture_output=True, timeout=5)
        return True
    except FileNotFoundError:
        return False


GITIGNORE_ENTRIES = [
    "# IDEs",
    ".idea/",
    ".vscode/",
    "*.swp",
    "*.swo",
    "",
    "# Python",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".ruff_cache/",
    "*.egg-info/",
    "dist/",
    "",
    "# Node",
    "node_modules/",
    ".next/",
    "",
    "# OS",
    ".DS_Store",
    "Thumbs.db",
    "",
    "# Env",
    ".env",
    ".env.local",
    "",
    "# Build",
    "build/",
    "target/",
]

JUNK_DIRS = {".venv", "venv", "node_modules", ".idea", ".vscode", "__pycache__", ".mypy_cache", ".ruff_cache"}


def check_gitignore(project: Path) -> None:
    if (project / ".gitignore").exists():
        return

    found = [d for d in JUNK_DIRS if (project / d).is_dir()]
    if not found:
        return

    print(f"  Found: {', '.join(sorted(found))}")
    answer = input("  Create .gitignore? [Y/n] ").strip().lower()
    if answer in ("", "y"):
        (project / ".gitignore").write_text("\n".join(GITIGNORE_ENTRIES) + "\n")
        print("  Created .gitignore")


def get_remote_url(project: Path) -> str | None:
    result = run(["git", "-C", str(project), "remote", "get-url", "origin"], project)
    url = result.stdout.strip()
    return url if result.returncode == 0 and url else None


def append_clone_url(url: str) -> None:
    known = set()
    if URLS_FILE.exists():
        known = {line.strip() for line in URLS_FILE.read_text().splitlines() if line.strip()}
    if url not in known:
        with URLS_FILE.open("a") as f:
            f.write(f"{url}\n")


def init_and_push(project: Path) -> bool:
    name = project.name

    check_gitignore(project)

    # git init
    if not (project / ".git").exists():
        result = run(["git", "init"], project)
        if result.returncode != 0:
            print(f"  FAIL git init: {result.stderr.strip()}")
            return False

    # initial commit if empty
    log = run(["git", "log", "--oneline", "-1"], project)
    if log.returncode != 0:
        run(["git", "add", "."], project)
        result = run(["git", "commit", "-m", "Initial commit"], project)
        if result.returncode != 0:
            print(f"  FAIL initial commit: {result.stderr.strip()}")
            return False

    # create private GitHub repo + set remote + push
    result = run(["gh", "repo", "create", name, "--private", "--source", str(project), "--push"], project)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "already exists" in stderr:
            print(f"  SKIP repo already exists on GitHub")
            url = get_remote_url(project)
            if url:
                append_clone_url(url)
            return True
        print(f"  FAIL gh repo create: {stderr}")
        return False

    url = get_remote_url(project)
    if url:
        append_clone_url(url)
    return True


if __name__ == "__main__":
    if not has_gh():
        print("gh CLI not found. Install: https://cli.github.com/")
        sys.exit(1)

    if not LOG_FILE.exists():
        print(f"No {LOG_FILE} found — run find-ungit-projects.py first.")
        sys.exit(1)

    projects = [Path(line.strip()) for line in LOG_FILE.read_text().splitlines() if line.strip()]

    if not projects:
        print("No ungit projects in log.")
        sys.exit(0)

    print(f"About to create {len(projects)} private GitHub repos:\n")
    for p in projects:
        print(f"  {p.name} <- {p}")

    print()
    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    print()
    for p in projects:
        if not p.exists():
            print(f"[SKIP] {p.name} — directory not found")
            continue
        print(f"[INIT] {p.name}...")
        if init_and_push(p):
            print(f"  OK https://github.com/persinac/{p.name}")
        print()
