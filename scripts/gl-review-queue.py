# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "rich"]
# ///
"""GitLab review queue — open MRs where you're a reviewer, priority-sorted.

Priority score = age in days (older = more urgent).
Drafts always sort last. CI failures bump score up (+15).
Merge conflicts lower score (-10, can't merge anyway).
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table
from rich import box

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com")
GITLAB_USERNAME = os.environ.get("GITLAB_USERNAME", "alex.persinger")

VAULT_DIR = Path.home() / "obs-garner" / "Garner"

CI_ICONS = {
    "success": "[green]✓[/green]",
    "failed": "[red]✗[/red]",
    "running": "[yellow]⋯[/yellow]",
    "pending": "[yellow]·[/yellow]",
    "canceled": "[dim]✕[/dim]",
    "skipped": "[dim]-[/dim]",
    None: "[dim]-[/dim]",
}

CI_PLAIN = {
    "success": "✓", "failed": "✗", "running": "⋯",
    "pending": "·", "canceled": "✕", "skipped": "-", None: "-",
}


def get_token() -> str:
    token = os.environ.get("GL_TOKEN") or os.environ.get("GITLAB_TOKEN", "")
    if not token:
        print("Error: GL_TOKEN or GITLAB_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


def get_user_id(token: str) -> int:
    with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=10.0) as client:
        resp = client.get(f"{GITLAB_URL}/api/v4/user")
        resp.raise_for_status()
        return resp.json()["id"]


def fetch_reviewer_mrs(token: str) -> list[dict]:
    user_id = get_user_id(token)
    mrs = []
    page = 1
    with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=15.0) as client:
        while True:
            resp = client.get(
                f"{GITLAB_URL}/api/v4/merge_requests",
                params={
                    "reviewer_id": user_id,
                    "state": "opened",
                    "scope": "all",
                    "per_page": 50,
                    "page": page,
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            mrs.extend(batch)
            if len(batch) < 50:
                break
            page += 1

    # Fetch per-MR details for CI status (head_pipeline not in list response)
    detailed = []
    with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=15.0) as client:
        for mr in mrs:
            proj = mr["project_id"]
            iid = mr["iid"]
            r = client.get(f"{GITLAB_URL}/api/v4/projects/{proj}/merge_requests/{iid}")
            if r.status_code == 200:
                detailed.append(r.json())
            else:
                detailed.append(mr)
    return detailed


def priority_score(mr: dict) -> int:
    if mr.get("draft") or mr.get("work_in_progress"):
        return -9999

    created = datetime.fromisoformat(mr["created_at"].replace("Z", "+00:00"))
    age_days = (datetime.now(timezone.utc) - created).days
    score = age_days

    pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
    ci_status = pipeline.get("status")
    if ci_status == "failed":
        score += 15
    elif ci_status in ("running", "pending"):
        score -= 5

    if mr.get("has_conflicts"):
        score -= 10

    if not mr.get("blocking_discussions_resolved", True):
        score += 10

    return score


def format_age(created_at: str) -> str:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - created).days
    if days == 0:
        return "today"
    if days == 1:
        return "1d"
    if days < 30:
        return f"{days}d"
    months = days // 30
    return f"{months}mo"


def short_repo(mr: dict) -> str:
    ref = mr.get("references", {}).get("full", "")
    # e.g. "garner-health/engineering/main!4346" → "main"
    parts = ref.replace("!", "/").split("/")
    if len(parts) >= 3:
        return parts[-2]
    return ref


def build_table(mrs: list[dict]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_footer=False, padding=(0, 1))
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Title", min_width=35, max_width=55, no_wrap=True)
    table.add_column("Repo", style="cyan", width=14)
    table.add_column("Age", justify="right", width=5)
    table.add_column("CI", justify="center", width=4)
    table.add_column("Notes", justify="right", width=5)
    table.add_column("Author", style="dim", width=16)

    ranked = sorted(mrs, key=priority_score, reverse=True)
    for rank, mr in enumerate(ranked, 1):
        is_draft = mr.get("draft") or mr.get("work_in_progress")
        pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
        ci_status = pipeline.get("status")

        title = mr["title"]
        if is_draft:
            title = f"[dim]{title}[/dim]"

        author = (mr.get("author") or {}).get("username", "?")
        if len(author) > 15:
            author = author[:14] + "…"

        table.add_row(
            str(rank),
            title,
            short_repo(mr),
            format_age(mr["created_at"]),
            CI_ICONS.get(ci_status, "[dim]-[/dim]"),
            str(mr.get("user_notes_count", 0)),
            author,
        )
    return table, ranked


def write_daily_note(mrs: list[dict]) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    daily_path = VAULT_DIR / "Daily Notes" / f"{today}.md"

    lines = ["\n## Review Queue\n"]
    ranked = sorted(mrs, key=priority_score, reverse=True)
    if not ranked:
        lines.append("_No open MRs assigned for review._\n")
    else:
        for rank, mr in enumerate(ranked, 1):
            pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
            ci = CI_PLAIN.get(pipeline.get("status"), "-")
            age = format_age(mr["created_at"])
            draft = " [DRAFT]" if (mr.get("draft") or mr.get("work_in_progress")) else ""
            author = (mr.get("author") or {}).get("username", "?")
            repo = short_repo(mr)
            url = mr["web_url"]
            lines.append(f"{rank}. [{mr['title']}]({url}){draft} — {repo} · {age} · CI:{ci} · by {author}\n")

    section = "".join(lines)

    if daily_path.exists():
        content = daily_path.read_text(encoding="utf-8")
        # Replace existing section if present
        if "## Review Queue" in content:
            import re
            content = re.sub(
                r"\n## Review Queue\n.*?(?=\n## |\Z)",
                section,
                content,
                flags=re.DOTALL,
            )
        else:
            content = content.rstrip() + "\n" + section
        daily_path.write_text(content, encoding="utf-8")
    else:
        daily_path.parent.mkdir(parents=True, exist_ok=True)
        daily_path.write_text(f"# {today}\n" + section, encoding="utf-8")

    print(f"  → wrote to {daily_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="GitLab MR review queue")
    parser.add_argument("--daily", action="store_true", help="Append to today's Obsidian daily note")
    args = parser.parse_args()

    token = get_token()
    console = Console(width=120)

    console.print(f"[dim]Fetching reviewer MRs for {GITLAB_USERNAME}…[/dim]")

    mrs = fetch_reviewer_mrs(token)

    if not mrs:
        console.print("[yellow]No open MRs assigned for review.[/yellow]")
        if args.daily:
            write_daily_note([])
        return

    table, ranked = build_table(mrs)
    console.print(table)
    console.print(f"[dim]{len(mrs)} MR(s) pending review[/dim]\n")

    for mr in ranked:
        is_draft = mr.get("draft") or mr.get("work_in_progress")
        url = mr["web_url"]
        if is_draft:
            console.print(f"  [dim]{url}[/dim]")
        else:
            console.print(f"  {url}")
    console.print()

    if args.daily:
        write_daily_note(mrs)


if __name__ == "__main__":
    main()
