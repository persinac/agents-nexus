"""Webhook receiver — dispatches GitLab events to registered handlers.

'I have been asked to facilitate the Reclamation.' — 127 Guilty Spark

Architecture:
  GitLab sends a Merge Request Hook → webhook endpoint parses it into a
  MergeRequestEvent → WebhookDispatcher fires all handlers registered for
  that action (e.g., "merge" → reindex, "open" → code review).

  Handlers run in background threads. The webhook returns 200 immediately.
"""
from __future__ import annotations

import logging
import queue
import subprocess
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spark.config import SparkConfig
from spark.indexer.builder import activate_installation, find_installation

logger = logging.getLogger("spark.server")


@dataclass
class MergeRequestEvent:
    """Parsed GitLab merge request webhook payload."""

    action: str  # "open", "merge", "close", "update", "reopen", etc.
    repo_name: str
    gitlab_path: str  # e.g., "my-group/my-repo"
    target_branch: str
    source_branch: str
    mr_title: str
    mr_url: str
    mr_iid: int
    author: str
    labels: list[str] = None

    # Local installation info (populated by dispatcher if found)
    repo_dir: Path | None = None
    rel_path: str | None = None

    def __post_init__(self):
        if self.labels is None:
            self.labels = []

    @classmethod
    def from_payload(cls, payload: dict) -> MergeRequestEvent:
        project = payload.get("project", {})
        attrs = payload.get("object_attributes", {})
        user = payload.get("user", {})
        labels = [l.get("title", "") for l in payload.get("labels", [])]
        return cls(
            action=attrs.get("action", ""),
            repo_name=project.get("path", project.get("name", "")),
            gitlab_path=project.get("path_with_namespace", ""),
            target_branch=attrs.get("target_branch", ""),
            source_branch=attrs.get("source_branch", ""),
            mr_title=attrs.get("title", ""),
            mr_url=attrs.get("url", ""),
            mr_iid=attrs.get("iid", 0),
            author=user.get("username", ""),
            labels=labels,
        )


# Type alias for handler functions
EventHandler = Callable[[MergeRequestEvent, SparkConfig], None]


class WebhookDispatcher:
    """Dispatches MR events to registered handlers by action.

    Handlers run in background threads so the webhook returns immediately.
    """

    def __init__(self, config: SparkConfig):
        self.config = config
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def on(self, action: str, handler: EventHandler) -> None:
        """Register a handler for a specific MR action."""
        self._handlers[action].append(handler)
        logger.info(f"[webhook] Registered handler {handler.__name__} for action={action}")

    def dispatch(self, event: MergeRequestEvent) -> list[str]:
        """Dispatch an event to all matching handlers.

        Resolves the local installation path and fires handlers in background threads.
        Returns list of handler names that were triggered.
        """
        # Resolve local installation
        result = find_installation(self.config, event.repo_name)
        if result:
            event.repo_dir, event.rel_path = result

        handlers = self._handlers.get(event.action, [])
        triggered = []
        for handler in handlers:
            thread = threading.Thread(
                target=self._run_handler,
                args=(handler, event),
                daemon=True,
            )
            thread.start()
            triggered.append(handler.__name__)

        return triggered

    def _run_handler(self, handler: EventHandler, event: MergeRequestEvent) -> None:
        try:
            handler(event, self.config)
        except Exception as e:
            logger.exception(
                f"[webhook] Handler {handler.__name__} failed for "
                f"{event.repo_name} ({event.action}): {e}"
            )

    @property
    def registered_actions(self) -> dict[str, list[str]]:
        return {
            action: [h.__name__ for h in handlers]
            for action, handlers in self._handlers.items()
        }


# --- Built-in handlers ---


def git_pull(repo_dir: Path) -> bool:
    """Pull latest changes from origin.

    Tries fast-forward pull first. Falls back to fetch + reset
    if the working tree is dirty or diverged.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return True

        # Fallback: fetch + reset to origin/HEAD
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "origin"],
            capture_output=True, text=True, timeout=60,
        )
        branch_result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        branch = branch_result.stdout.strip() or "main"
        subprocess.run(
            ["git", "-C", str(repo_dir), "reset", "--hard", f"origin/{branch}"],
            capture_output=True, text=True, timeout=30,
        )
        return True
    except Exception as e:
        logger.error(f"git pull failed for {repo_dir}: {e}")
        return False


# Reindex lock — ensures only one reindex runs at a time (LanceDB write safety)
_reindex_lock = threading.Lock()


def handle_reindex(event: MergeRequestEvent, config: SparkConfig) -> None:
    """Pull the repo and reindex it. Runs on MR merge."""
    if not event.repo_dir or not event.rel_path:
        logger.warning(f"[reindex] Installation '{event.repo_name}' not found locally, skipping")
        return

    with _reindex_lock:
        logger.info(f"[reindex] Pulling {event.repo_name}...")
        if not git_pull(event.repo_dir):
            logger.error(f"[reindex] git pull failed for {event.repo_name}, skipping")
            return

        logger.info(f"[reindex] Reindexing {event.repo_name}...")
        stats = activate_installation(config, event.repo_dir, event.rel_path, verbose=False)
        logger.info(f"[reindex] Done {event.repo_name}: {stats['chunks']} chunks")


MINIONS_SUITE_DIR = Path.home() / "minions" / "example-repo"


def handle_mr_review(event: MergeRequestEvent, config: SparkConfig) -> None:
    """Spawn a minion to review the MR. Runs on MR open."""
    if any(l.startswith("minions-job-") for l in event.labels):
        logger.info(f"[review] Skipping {event.repo_name} !{event.mr_iid} — has minions-job label")
        return

    if not event.mr_url:
        logger.warning(f"[review] No MR URL for {event.repo_name} !{event.mr_iid}, skipping")
        return

    logger.info(f"[review] Spawning minion review for {event.repo_name} !{event.mr_iid}")
    try:
        result = subprocess.run(
            ["task", "minion:review", "--", event.mr_url],
            cwd=str(MINIONS_SUITE_DIR),
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            logger.info(f"[review] Review complete for {event.repo_name} !{event.mr_iid}")
        else:
            logger.error(
                f"[review] Review failed for {event.repo_name} !{event.mr_iid}: "
                f"{result.stderr[:500]}"
            )
    except subprocess.TimeoutExpired:
        logger.error(f"[review] Review timed out for {event.repo_name} !{event.mr_iid}")
    except Exception as e:
        logger.exception(f"[review] Failed to spawn review for {event.repo_name}: {e}")


def handle_decision_synthesis(event: MergeRequestEvent, config: SparkConfig) -> None:
    """Synthesize a decision record from a merged MR and index it.

    Runs alongside handle_reindex on the 'merge' action. Fetches full MR
    data and discussion notes, calls the LLM synthesizer, and upserts a
    decision chunk into LanceDB. Logs and returns silently on any failure.
    """
    import time

    if not config.decisions_enabled:
        return

    if not config.gitlab_enabled:
        logger.warning("[decision] GitLab not configured — skipping decision synthesis")
        return

    if not event.gitlab_path or not event.mr_iid:
        logger.warning(f"[decision] Missing gitlab_path or mr_iid for {event.repo_name}, skipping")
        return

    start = time.monotonic()
    logger.info(f"[decision] Synthesizing decision for {event.repo_name} !{event.mr_iid}")

    try:
        from spark.gitlab import GitLabClient
        from spark.indexer.builder import TABLE_NAME
        from spark.indexer.embedder import embed_single
        from spark.synthesizer import synthesize_decision

        client = GitLabClient(config.gitlab_url, config.gitlab_token)
        mr_data = client.get_mr_full(event.gitlab_path, event.mr_iid)
        notes = client.get_mr_notes(event.gitlab_path, event.mr_iid)
        client.close()

        if not mr_data:
            logger.warning(f"[decision] Could not fetch MR data for {event.repo_name} !{event.mr_iid}")
            return

        team = config.resolve_team(event.rel_path or "")
        content = synthesize_decision(
            mr_title=mr_data["title"],
            mr_description=mr_data["description"],
            mr_notes=notes,
            repo_name=event.repo_name,
            team=team,
            merged_at=mr_data["merged_at"],
            author=mr_data["author"],
            config=config,
        )

        if not content:
            logger.warning(f"[decision] Empty synthesis result for {event.repo_name} !{event.mr_iid}, skipping")
            return

        from spark.indexer.chunker import Chunk
        chunk_id = f"{event.repo_name}::decision::{event.mr_iid}"
        chunk = Chunk(
            id=chunk_id,
            installation=event.repo_name,
            installation_path=event.rel_path or "",
            team=team,
            chunk_type="decision",
            file_path="",
            content=f"# Decision: {mr_data['title']}\nRepo: {event.repo_name} | Team: {team}\n\n{content}",
            decision_date=mr_data["merged_at"][:10] if mr_data["merged_at"] else "",
            decision_author=mr_data["author"],
            mr_url=mr_data["web_url"],
        )

        vector = embed_single(chunk.content, config)
        record = {
            "id": chunk.id,
            "installation": chunk.installation,
            "installation_path": chunk.installation_path,
            "team": chunk.team,
            "chunk_type": chunk.chunk_type,
            "file_path": chunk.file_path,
            "content": chunk.content,
            "vector": vector,
            "decision_date": chunk.decision_date,
            "decision_author": chunk.decision_author,
            "mr_url": chunk.mr_url,
            "description": "",
            "topics": "",
            "languages": "",
            "last_activity": "",
            "archived": False,
            "web_url": "",
            "framework": "",
            "ci_type": "",
            "deploy_target": "",
            "test_command": "",
            "lint_command": "",
            "clone_url": "",
            "services": "",
        }

        import lancedb
        from spark.indexer.builder import ensure_decision_columns, ensure_services_columns
        db = lancedb.connect(str(config.index_path))
        table = db.open_table(TABLE_NAME)
        ensure_decision_columns(table)
        ensure_services_columns(table)
        table.delete(f'id = "{chunk_id}"')
        table.add([record])

        duration = round(time.monotonic() - start, 1)
        logger.info(f"[decision] Indexed decision for {event.repo_name} !{event.mr_iid} ({duration}s)")

    except Exception as e:
        duration = round(time.monotonic() - start, 1)
        logger.exception(f"[decision] Failed for {event.repo_name} !{event.mr_iid} ({duration}s): {e}")


def create_default_dispatcher(config: SparkConfig) -> WebhookDispatcher:
    """Create a dispatcher with the default built-in handlers."""
    dispatcher = WebhookDispatcher(config)
    dispatcher.on("merge", handle_reindex)
    dispatcher.on("merge", handle_decision_synthesis)
    dispatcher.on("open", handle_mr_review)
    return dispatcher
