"""GitLab API client for installation enrichment.

'The installation's records are quite thorough!' — 127 Guilty Spark
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass
class GitLabProject:
    """Metadata for a GitLab project."""

    project_id: int
    description: str
    topics: list[str]
    languages: dict[str, float]  # language -> percentage
    last_activity: str  # ISO date
    archived: bool
    default_branch: str
    web_url: str


class GitLabClient:
    """Thin client for GitLab REST API v4.

    Includes simple rate limiting (~5 req/sec) to stay within GitLab's
    300 req/min limit for authenticated users.
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=f"{self.base_url}/api/v4",
            headers={"PRIVATE-TOKEN": token},
            timeout=15.0,
        )
        self._last_request: float = 0.0
        self._min_interval: float = 0.2  # 5 req/sec

    def _rate_limit(self) -> None:
        """Simple rate limiter — ensure min interval between requests."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    def get_project(self, project_path: str) -> GitLabProject | None:
        """Fetch project metadata by path (e.g., 'group/subgroup/repo')."""
        encoded = project_path.replace("/", "%2F")
        try:
            self._rate_limit()
            resp = self.client.get(f"/projects/{encoded}")
            resp.raise_for_status()
            data = resp.json()

            # Fetch languages separately
            self._rate_limit()
            lang_resp = self.client.get(f"/projects/{encoded}/languages")
            languages = lang_resp.json() if lang_resp.is_success else {}

            return GitLabProject(
                project_id=data["id"],
                description=data.get("description") or "",
                topics=data.get("topics", []),
                languages=languages,
                last_activity=data.get("last_activity_at", ""),
                archived=data.get("archived", False),
                default_branch=data.get("default_branch", "main"),
                web_url=data.get("web_url", ""),
            )
        except Exception:
            return None

    def get_recent_merge_requests(
        self, project_path: str, limit: int = 10
    ) -> list[dict]:
        """Fetch recent merged MRs (title + description)."""
        encoded = project_path.replace("/", "%2F")
        try:
            self._rate_limit()
            resp = self.client.get(
                f"/projects/{encoded}/merge_requests",
                params={
                    "state": "merged",
                    "order_by": "updated_at",
                    "sort": "desc",
                    "per_page": limit,
                },
            )
            resp.raise_for_status()
            return [
                {
                    "iid": mr["iid"],
                    "title": mr["title"],
                    "description": mr.get("description") or "",
                    "merged_at": mr.get("merged_at", ""),
                    "author": mr.get("author", {}).get("username", ""),
                }
                for mr in resp.json()
            ]
        except Exception:
            return []

    def get_mr_full(self, project_path: str, mr_iid: int) -> dict | None:
        """Fetch full MR details: description, diff stats, labels, milestone, web_url.

        Returns None on any error without raising.
        """
        encoded = project_path.replace("/", "%2F")
        try:
            self._rate_limit()
            resp = self.client.get(f"/projects/{encoded}/merge_requests/{mr_iid}")
            resp.raise_for_status()
            data = resp.json()

            self._rate_limit()
            diff_resp = self.client.get(f"/projects/{encoded}/merge_requests/{mr_iid}/changes")
            diff_stats = {}
            if diff_resp.is_success:
                changes_data = diff_resp.json()
                diffs = changes_data.get("changes", [])
                diff_stats = {
                    "files_changed": len(diffs),
                    "insertions": sum(d.get("diff", "").count("\n+") for d in diffs),
                    "deletions": sum(d.get("diff", "").count("\n-") for d in diffs),
                }

            labels = [lb["name"] for lb in data.get("labels", [])] if data.get("labels") else []
            milestone = data.get("milestone", {})
            milestone_title = milestone.get("title", "") if milestone else ""

            return {
                "title": data.get("title", ""),
                "description": data.get("description") or "",
                "merged_at": data.get("merged_at", ""),
                "author": data.get("author", {}).get("username", ""),
                "labels": labels,
                "milestone": milestone_title,
                "diff_stats": diff_stats,
                "web_url": data.get("web_url", ""),
            }
        except Exception:
            return None

    def get_mr_notes(self, project_path: str, mr_iid: int) -> list[dict]:
        """Fetch human-authored discussion notes for an MR.

        Filters out system notes (system: true). Returns list of
        {author, body, created_at} dicts. Returns [] on any error.
        """
        encoded = project_path.replace("/", "%2F")
        try:
            self._rate_limit()
            resp = self.client.get(
                f"/projects/{encoded}/merge_requests/{mr_iid}/notes",
                params={"sort": "asc", "order_by": "created_at", "per_page": 100},
            )
            resp.raise_for_status()
            notes = []
            for note in resp.json():
                if note.get("system", False):
                    continue
                notes.append({
                    "author": note.get("author", {}).get("username", ""),
                    "body": note.get("body", ""),
                    "created_at": note.get("created_at", ""),
                })
            return notes
        except Exception:
            return []

    def add_project_webhook(
        self,
        project_path: str,
        webhook_url: str,
        secret_token: str,
    ) -> dict | None:
        """Register a merge-request webhook on a project.

        Returns the created webhook dict, or None on failure.
        Skips if an identical webhook URL already exists.
        """
        encoded = project_path.replace("/", "%2F")
        try:
            # Check for existing webhooks with the same URL
            self._rate_limit()
            resp = self.client.get(f"/projects/{encoded}/hooks")
            if resp.is_success:
                for hook in resp.json():
                    if hook.get("url") == webhook_url:
                        return {"id": hook["id"], "status": "already_exists"}

            # Create the webhook
            self._rate_limit()
            resp = self.client.post(
                f"/projects/{encoded}/hooks",
                json={
                    "url": webhook_url,
                    "token": secret_token,
                    "merge_requests_events": True,
                    "push_events": False,
                    "enable_ssl_verification": webhook_url.startswith("https"),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return {"id": data["id"], "status": "created"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    def close(self) -> None:
        self.client.close()


def parse_gitlab_path(repo_dir: Path) -> str | None:
    """Extract GitLab project path from a repo's git remote URL.

    Handles:
      git@gitlab.com:group/subgroup/repo.git  -> group/subgroup/repo
      https://gitlab.com/group/subgroup/repo.git -> group/subgroup/repo
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        url = result.stdout.strip()
    except Exception:
        return None

    if not url:
        return None

    # SSH format: git@host:path.git
    match = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)

    # HTTPS format: https://host/path.git
    match = re.match(r"https?://[^/]+/(.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)

    return None
