# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "rich"]
# ///
"""Remove yourself as reviewer from stale open MRs.

Fetches current reviewers for each stale MR, removes you, and updates
with the remaining reviewer list intact.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import httpx
from rich.console import Console

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com")
DEFAULT_MIN_AGE_DAYS = 180  # 6 months


def get_token() -> str:
    token = os.environ.get("GL_TOKEN") or os.environ.get("GITLAB_TOKEN", "")
    if not token:
        print("Error: GL_TOKEN or GITLAB_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


def get_me(token: str) -> dict:
    with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=10.0) as client:
        resp = client.get(f"{GITLAB_URL}/api/v4/user")
        resp.raise_for_status()
        return resp.json()


def fetch_reviewer_mrs(token: str, user_id: int) -> list[dict]:
    mrs = []
    page = 1
    with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=15.0) as client:
        while True:
            resp = client.get(
                f"{GITLAB_URL}/api/v4/merge_requests",
                params={"reviewer_id": user_id, "state": "opened", "scope": "all", "per_page": 50, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            mrs.extend(batch)
            if len(batch) < 50:
                break
            page += 1
    return mrs


def age_str(created_at: str) -> str:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - created).days
    if days < 30:
        return f"{days}d"
    return f"{days // 30}mo"


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove yourself as reviewer from stale MRs")
    parser.add_argument("--min-age", type=int, default=DEFAULT_MIN_AGE_DAYS,
                        help=f"Minimum age in days to consider stale (default: {DEFAULT_MIN_AGE_DAYS})")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    args = parser.parse_args()

    token = get_token()
    console = Console()

    me = get_me(token)
    my_id = me["id"]

    console.print(f"[dim]Fetching reviewer MRs for {me['username']}…[/dim]")
    mrs = fetch_reviewer_mrs(token, my_id)

    now = datetime.now(timezone.utc)
    stale = [
        (mr, (now - datetime.fromisoformat(mr["created_at"].replace("Z", "+00:00"))).days)
        for mr in mrs
        if (now - datetime.fromisoformat(mr["created_at"].replace("Z", "+00:00"))).days >= args.min_age
    ]

    if not stale:
        console.print(f"[green]No MRs older than {args.min_age}d — nothing to prune.[/green]")
        return

    dry = " [dim](dry run)[/dim]" if args.dry_run else ""
    console.print(f"\nFound {len(stale)} MR(s) older than {args.min_age}d{dry}\n")

    with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=15.0) as client:
        for mr, age_days in stale:
            proj = mr["project_id"]
            iid = mr["iid"]

            detail = client.get(f"{GITLAB_URL}/api/v4/projects/{proj}/merge_requests/{iid}")
            detail.raise_for_status()
            current_reviewers = detail.json().get("reviewers", [])

            remaining_ids = [r["id"] for r in current_reviewers if r["id"] != my_id]
            kept = [r["username"] for r in current_reviewers if r["id"] != my_id]

            title = mr["title"][:60] + ("…" if len(mr["title"]) > 60 else "")
            kept_str = f"  [dim](kept: {', '.join(kept)})[/dim]" if kept else ""

            if not args.dry_run:
                # Pass remaining reviewer IDs; empty list clears all reviewers
                params = [("reviewer_ids[]", uid) for uid in remaining_ids] or [("reviewer_ids[]", "")]
                resp = client.put(f"{GITLAB_URL}/api/v4/projects/{proj}/merge_requests/{iid}", params=params)
                resp.raise_for_status()
                status = "[green]✓[/green]"
            else:
                status = "[dim]~[/dim]"

            console.print(f"  {status} [{age_str(mr['created_at'])}] {title}{kept_str}")
            console.print(f"      [dim]{mr['web_url']}[/dim]")

    if args.dry_run:
        console.print(f"\n[dim]Dry run complete — rerun without --dry-run to apply.[/dim]")
    else:
        console.print(f"\n[green]Removed yourself from {len(stale)} MR(s).[/green]")


if __name__ == "__main__":
    main()
