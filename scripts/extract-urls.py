"""Extract unique GitHub clone URLs from repos listed in found-repos.log."""

import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOG_FILE = SCRIPT_DIR / "found-repos.log"
URLS_FILE = SCRIPT_DIR / "clone-urls.txt"


def get_github_url(repo_path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
        if url and "github.com" in url:
            return url
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


if __name__ == "__main__":
    if not LOG_FILE.exists():
        print(f"No {LOG_FILE} found — run find-repos.py first.")
        raise SystemExit(1)

    repos = [Path(line.strip()) for line in LOG_FILE.read_text().splitlines() if line.strip()]

    known_urls: set[str] = set()
    if URLS_FILE.exists():
        known_urls = {line.strip() for line in URLS_FILE.read_text().splitlines() if line.strip()}

    new_urls: list[str] = []
    for repo in repos:
        url = get_github_url(repo)
        if url and url not in known_urls:
            known_urls.add(url)
            new_urls.append(url)

    if new_urls:
        with URLS_FILE.open("a") as f:
            for url in new_urls:
                f.write(f"{url}\n")
        print(f"Added {len(new_urls)} new URLs (skipped {len(known_urls) - len(new_urls)} already known):\n")
        for url in new_urls:
            print(f"  {url}")
    else:
        print(f"No new URLs found ({len(known_urls)} already in clone-urls.txt).")
