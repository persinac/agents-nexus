"""GitHub API client for installation enrichment.

'This installation's records are quite thorough!' — 127 Guilty Spark
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass
class GitHubProject:
    """Metadata for a GitHub repository."""

    project_id: int
    description: str
    topics: list[str]
    languages: dict[str, float]  # language -> percentage
    last_activity: str  # ISO date
    archived: bool
    default_branch: str
    web_url: str


class GitHubClient:
    """Thin client for GitHub REST API v3.

    Paces requests at ~5/sec — well under GitHub's 5000/hr authenticated limit.
    """

    def __init__(self, token: str, base_url: str = "https://api.github.com"):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15.0,
        )
        self._last_request: float = 0.0
        self._min_interval: float = 0.2  # 5 req/sec

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    def get_project(self, owner_repo: str) -> GitHubProject | None:
        """Fetch repo metadata by 'owner/repo'."""
        try:
            self._rate_limit()
            resp = self.client.get(f"/repos/{owner_repo}")
            resp.raise_for_status()
            data = resp.json()

            # Language byte counts -> percentages
            self._rate_limit()
            lang_resp = self.client.get(f"/repos/{owner_repo}/languages")
            raw_langs = lang_resp.json() if lang_resp.is_success else {}
            total = sum(raw_langs.values()) or 1
            languages = {lang: round(b / total * 100, 1) for lang, b in raw_langs.items()}

            # Topics require the mercy-preview header
            self._rate_limit()
            topics_resp = self.client.get(
                f"/repos/{owner_repo}/topics",
                headers={"Accept": "application/vnd.github.mercy-preview+json"},
            )
            topics = topics_resp.json().get("names", []) if topics_resp.is_success else []

            return GitHubProject(
                project_id=data["id"],
                description=data.get("description") or "",
                topics=topics,
                languages=languages,
                last_activity=data.get("pushed_at") or data.get("updated_at", ""),
                archived=data.get("archived", False),
                default_branch=data.get("default_branch", "main"),
                web_url=data.get("html_url", ""),
            )
        except Exception:
            return None

    def get_recent_pull_requests(self, owner_repo: str, limit: int = 10) -> list[dict]:
        """Fetch recent merged PRs (title + body).

        Over-fetches closed PRs since not all closed are merged.
        """
        try:
            self._rate_limit()
            resp = self.client.get(
                f"/repos/{owner_repo}/pulls",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": min(limit * 3, 100),
                },
            )
            resp.raise_for_status()
            merged = [pr for pr in resp.json() if pr.get("merged_at")]
            return [
                {
                    "iid": pr["number"],
                    "title": pr["title"],
                    "description": pr.get("body") or "",
                    "merged_at": pr.get("merged_at", ""),
                    "author": pr.get("user", {}).get("login", ""),
                }
                for pr in merged[:limit]
            ]
        except Exception:
            return []

    def get_pr_full(self, owner_repo: str, pr_number: int) -> dict | None:
        """Fetch full PR details: description, diff stats, labels, milestone, html_url.

        Returns None on any error without raising.
        """
        try:
            self._rate_limit()
            resp = self.client.get(f"/repos/{owner_repo}/pulls/{pr_number}")
            resp.raise_for_status()
            data = resp.json()
            labels = [lb["name"] for lb in data.get("labels", [])]
            milestone = data.get("milestone") or {}
            return {
                "title": data.get("title", ""),
                "description": data.get("body") or "",
                "merged_at": data.get("merged_at", ""),
                "author": data.get("user", {}).get("login", ""),
                "labels": labels,
                "milestone": milestone.get("title", ""),
                "diff_stats": {
                    "files_changed": data.get("changed_files", 0),
                    "insertions": data.get("additions", 0),
                    "deletions": data.get("deletions", 0),
                },
                "web_url": data.get("html_url", ""),
            }
        except Exception:
            return None

    def get_pr_comments(self, owner_repo: str, pr_number: int) -> list[dict]:
        """Fetch human-authored review comments for a PR.

        Filters bot accounts (login ending in [bot]).
        Returns list of {author, body, created_at} dicts.
        """
        try:
            self._rate_limit()
            resp = self.client.get(
                f"/repos/{owner_repo}/pulls/{pr_number}/comments",
                params={"per_page": 100},
            )
            resp.raise_for_status()
            comments = []
            for c in resp.json():
                login = c.get("user", {}).get("login", "")
                if login.endswith("[bot]"):
                    continue
                comments.append({
                    "author": login,
                    "body": c.get("body", ""),
                    "created_at": c.get("created_at", ""),
                })
            return comments
        except Exception:
            return []

    def close(self) -> None:
        self.client.close()


def parse_github_path(repo_dir: Path) -> str | None:
    """Extract GitHub owner/repo from a repo's git remote URL.

    Only matches github.com remotes — returns None for GitLab or other hosts.
    Handles:
      git@github.com:owner/repo.git  -> owner/repo
      https://github.com/owner/repo  -> owner/repo
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

    if not url or "github.com" not in url:
        return None

    # SSH: git@github.com:owner/repo.git
    match = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)

    # HTTPS: https://github.com/owner/repo.git
    match = re.match(r"https?://github\.com/(.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)

    return None
