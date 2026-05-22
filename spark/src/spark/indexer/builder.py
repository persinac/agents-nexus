"""Builder — constructs and maintains the Index.

'Reclaimer! Let me illuminate this installation's history.'
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("spark.builder")

import lancedb
import pyarrow as pa

from spark.config import SparkConfig
from spark.indexer.chunker import Chunk, chunk_installation
from spark.indexer.embedder import embed_texts

# LanceDB table name
TABLE_NAME = "the_index"

# Sparse columns added in the decision synthesis feature.
# These may be absent from indexes built before that feature was added.
_DECISION_COLUMNS = {"decision_date": "''", "decision_author": "''", "mr_url": "''"}

# Sparse columns added in the symbol chunking feature.
_SYMBOL_COLUMNS = {"symbol_name": "''", "symbol_type": "''"}


def ensure_decision_columns(table) -> None:
    """Add decision synthesis columns to the table if they don't exist yet.

    Safe to call multiple times — skips columns that are already present.
    """
    existing = {field.name for field in table.schema}
    missing = {col: default for col, default in _DECISION_COLUMNS.items() if col not in existing}
    if missing:
        table.add_columns(missing)


def ensure_symbol_columns(table) -> None:
    """Add symbol chunking columns to the table if they don't exist yet.

    Safe to call multiple times — skips columns that are already present.
    """
    existing = {field.name for field in table.schema}
    missing = {col: default for col, default in _SYMBOL_COLUMNS.items() if col not in existing}
    if missing:
        table.add_columns(missing)

def _discover_installations(config: SparkConfig) -> list[tuple[Path, str]]:
    """Walk the repos directory and discover all installations.

    Finds any directory containing a .git folder, regardless of hierarchy.
    Works with flat layouts (repos/svc-foo), nested teams (repos/search/concierge/svc-foo),
    or any other structure.

    Returns list of (absolute_path, relative_path_from_repos_root).
    """
    base = config.installations_path
    installations = []

    for root, dirs, _files in os.walk(base):
        root_path = Path(root)

        # If this directory has .git, it's an installation — index it and don't recurse deeper
        if (root_path / ".git").exists():
            rel = str(root_path.relative_to(base))
            # Skip guilty-spark itself
            if root_path.name != "guilty-spark":
                installations.append((root_path, rel))
            # Don't descend into git repos (no repos-inside-repos)
            dirs.clear()
            continue

        # Prune dirs we never want to walk into
        dirs[:] = [
            d for d in dirs
            if d not in config.exclude_dirs and not d.startswith(".")
        ]

    return sorted(installations, key=lambda x: x[1])


def find_installation(config: SparkConfig, repo_name: str) -> tuple[Path, str] | None:
    """Find an installation by repo name.

    Walks the installations directory looking for a git repo with the given name.
    Returns (absolute_path, relative_path) or None if not found.
    """
    for repo_dir, rel_path in _discover_installations(config):
        if repo_dir.name == repo_name:
            return (repo_dir, rel_path)
    return None


def _chunks_to_table_data(
    chunks: list[Chunk], embeddings: list[list[float]]
) -> list[dict]:
    """Convert chunks + embeddings into records for LanceDB."""
    records = []
    for chunk, vector in zip(chunks, embeddings):
        records.append(
            {
                "id": chunk.id,
                "installation": chunk.installation,
                "installation_path": chunk.installation_path,
                "team": chunk.team,
                "chunk_type": chunk.chunk_type,
                "file_path": chunk.file_path,
                "content": chunk.content,
                "vector": vector,
                # Symbol metadata
                "symbol_name": chunk.symbol_name,
                "symbol_type": chunk.symbol_type,
                # Decision synthesis metadata
                "decision_date": chunk.decision_date,
                "decision_author": chunk.decision_author,
                "mr_url": chunk.mr_url,
                # GitLab metadata
                "description": chunk.description,
                "topics": chunk.topics,
                "languages": chunk.languages,
                "last_activity": chunk.last_activity,
                "archived": chunk.archived,
                "web_url": chunk.web_url,
                # Detected project metadata
                "framework": chunk.framework,
                "ci_type": chunk.ci_type,
                "deploy_target": chunk.deploy_target,
                "test_command": chunk.test_command,
                "lint_command": chunk.lint_command,
                "clone_url": chunk.clone_url,
            }
        )
    return records


def reclaim(config: SparkConfig, verbose: bool = True, path_filter: str | None = None) -> dict:
    """Full index rebuild — 'Reclaim' the Index.

    Discovers all installations, chunks them, embeds, and stores in LanceDB.

    Args:
        path_filter: Only index installations whose relative path starts with this prefix.
                     e.g., "search" indexes search/directory/*, search/concierge/*, etc.
    Returns stats dict.
    """
    start = time.time()
    all_installations = _discover_installations(config)

    if path_filter:
        installations = [(p, r) for p, r in all_installations if r.startswith(path_filter)]
    else:
        installations = all_installations

    # Initialize VCS clients if configured
    gitlab_client = None
    github_client = None
    if config.gitlab_enabled:
        from spark.gitlab import GitLabClient
        gitlab_client = GitLabClient(config.gitlab_url, config.gitlab_token)
    if config.github_enabled:
        from spark.github import GitHubClient
        github_client = GitHubClient(config.github_token, config.github_url)

    if verbose:
        if path_filter:
            logger.info("Filter: %s/* (%d of %d installations)", path_filter, len(installations), len(all_installations))
        else:
            logger.info("Discovered %d installations", len(installations))
        logger.info("Embedding model: %s", config.embedding_model)
        if gitlab_client:
            logger.info("GitLab enrichment: enabled (%s)", config.gitlab_url)
        if github_client:
            logger.info("GitHub enrichment: enabled (%s)", config.github_url)
        logger.info("Index path: %s", config.index_path)

    from spark.detector import detect_project

    all_chunks: list[Chunk] = []
    vcs_hits = 0
    for i, (repo_dir, rel_path) in enumerate(installations):
        # Fetch VCS metadata — try GitLab first, then GitHub
        vcs_project = None
        vcs_url = ""
        merge_requests = None
        vcs_marker = ""

        if gitlab_client:
            from spark.gitlab import parse_gitlab_path
            gl_path = parse_gitlab_path(repo_dir)
            if gl_path:
                vcs_project = gitlab_client.get_project(gl_path)
                if vcs_project:
                    vcs_hits += 1
                    vcs_url = config.gitlab_url
                    vcs_marker = " [GL]"
                    merge_requests = gitlab_client.get_recent_merge_requests(gl_path, limit=10)

        if vcs_project is None and github_client:
            from spark.github import parse_github_path
            gh_path = parse_github_path(repo_dir)
            if gh_path:
                vcs_project = github_client.get_project(gh_path)
                if vcs_project:
                    vcs_hits += 1
                    vcs_url = config.github_url
                    vcs_marker = " [GH]"
                    merge_requests = github_client.get_recent_pull_requests(gh_path, limit=10)

        # Run project detection (framework, CI, deploy, test/lint)
        detected = detect_project(repo_dir, vcs_project, vcs_url)

        chunks = chunk_installation(
            repo_dir, rel_path, config,
            gitlab_project=vcs_project, detected=detected,
            merge_requests=merge_requests,
        )
        all_chunks.extend(chunks)
        if verbose:
            summary_count = sum(1 for c in chunks if c.chunk_type == "summary")
            file_count = sum(1 for c in chunks if c.chunk_type == "file")
            symbol_count = sum(1 for c in chunks if c.chunk_type == "symbol")
            mr_count = sum(1 for c in chunks if c.chunk_type == "merge_request")
            det_info = ""
            if detected.framework:
                det_info = f" ({detected.framework}"
                if detected.deploy_target:
                    det_info += f"/{detected.deploy_target}"
                det_info += ")"
            mr_info = f" + {mr_count} MRs" if mr_count else ""
            code_info = f"{symbol_count} symbols" if symbol_count else f"{file_count} files"
            logger.info("[%d/%d] %s: 1 summary + %s%s%s%s",
                i + 1, len(installations), rel_path, code_info, mr_info, vcs_marker, det_info)

    if not all_chunks:
        logger.warning("No chunks to index!")
        return {"installations": 0, "chunks": 0, "duration": 0}

    if verbose:
        logger.info("Embedding %d chunks...", len(all_chunks))

    texts = [c.content for c in all_chunks]
    embeddings = embed_texts(texts, config)

    if verbose:
        logger.info("Writing to the Index...")

    records = _chunks_to_table_data(all_chunks, embeddings)
    index_path = config.index_path
    index_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(index_path))

    def _drop_and_create():
        try:
            db.drop_table(TABLE_NAME)
        except Exception:
            pass
        db.create_table(TABLE_NAME, data=records)

    if path_filter:
        # Partial rebuild: delete chunks for filtered installations, then add new ones
        try:
            table = db.open_table(TABLE_NAME)
            install_names = {repo_dir.name for repo_dir, _ in installations}
            for name in install_names:
                table.delete(f'installation = "{name}"')
            if records:
                table.add(records)
        except ValueError:
            # Schema mismatch (e.g., new columns added) — full rebuild required
            if verbose:
                logger.warning("Schema changed — rebuilding full table...")
            _drop_and_create()
        except Exception:
            # Table doesn't exist yet, create it
            _drop_and_create()
    else:
        # Full rebuild: wipe and recreate
        _drop_and_create()

    if gitlab_client:
        gitlab_client.close()
    if github_client:
        github_client.close()

    if config.hybrid_search_enabled:
        if verbose:
            logger.info("Building full-text index...")
        table = db.open_table(TABLE_NAME)
        table.create_fts_index("content", replace=True)

    duration = time.time() - start
    stats = {
        "installations": len(installations),
        "chunks": len(all_chunks),
        "summaries": sum(1 for c in all_chunks if c.chunk_type == "summary"),
        "files": sum(1 for c in all_chunks if c.chunk_type == "file"),
        "symbols": sum(1 for c in all_chunks if c.chunk_type == "symbol"),
        "merge_requests": sum(1 for c in all_chunks if c.chunk_type == "merge_request"),
        "vcs_enriched": vcs_hits,
        "duration": round(duration, 1),
    }

    if verbose:
        mr_str = f", {stats['merge_requests']} MRs" if stats["merge_requests"] else ""
        sym_str = f"{stats['symbols']} symbols" if stats["symbols"] else f"{stats['files']} files"
        logger.info("Reclamation complete! %d installations, %d chunks (%d summaries, %s%s)%s in %ss",
            stats["installations"], stats["chunks"], stats["summaries"], sym_str, mr_str,
            f", {vcs_hits} VCS enriched" if vcs_hits else "",
            stats["duration"])

    return stats


def activate_installation(
    config: SparkConfig,
    repo_dir: Path,
    installation_path: str,
    verbose: bool = True,
) -> dict:
    """Incremental update — re-index a single installation.

    Deletes existing chunks for this installation and inserts fresh ones.
    """
    installation = repo_dir.name

    if verbose:
        logger.info("Activating installation: %s", installation)

    # Fetch VCS metadata — try GitLab first, then GitHub
    vcs_project = None
    vcs_url = ""
    merge_requests = None

    if config.gitlab_enabled:
        from spark.gitlab import GitLabClient, parse_gitlab_path
        gl_path = parse_gitlab_path(repo_dir)
        if gl_path:
            client = GitLabClient(config.gitlab_url, config.gitlab_token)
            vcs_project = client.get_project(gl_path)
            merge_requests = client.get_recent_merge_requests(gl_path, limit=10)
            client.close()
            if vcs_project:
                vcs_url = config.gitlab_url
                if verbose:
                    logger.info("  GitLab metadata: %s...", vcs_project.description[:60] or "(no description)")

    if vcs_project is None and config.github_enabled:
        from spark.github import GitHubClient, parse_github_path
        gh_path = parse_github_path(repo_dir)
        if gh_path:
            client = GitHubClient(config.github_token, config.github_url)
            vcs_project = client.get_project(gh_path)
            merge_requests = client.get_recent_pull_requests(gh_path, limit=10)
            client.close()
            if vcs_project:
                vcs_url = config.github_url
                if verbose:
                    logger.info("  GitHub metadata: %s...", vcs_project.description[:60] or "(no description)")

    if verbose and merge_requests:
        logger.info("  Recent PRs/MRs: %d", len(merge_requests))

    from spark.detector import detect_project
    detected = detect_project(repo_dir, vcs_project, vcs_url)

    chunks = chunk_installation(
        repo_dir, installation_path, config,
        gitlab_project=vcs_project, detected=detected,
        merge_requests=merge_requests,
    )
    texts = [c.content for c in chunks]
    embeddings = embed_texts(texts, config)
    records = _chunks_to_table_data(chunks, embeddings)

    db = lancedb.connect(str(config.index_path))
    table = db.open_table(TABLE_NAME)
    ensure_decision_columns(table)
    ensure_symbol_columns(table)

    # Delete old chunks for this installation
    table.delete(f'installation = "{installation}"')

    # Insert new chunks
    if records:
        table.add(records)

    if config.hybrid_search_enabled:
        table.create_fts_index("content", replace=True)

    if verbose:
        logger.info("Indexed %d chunks for %s", len(chunks), installation)

    return {"installation": installation, "chunks": len(chunks)}


# ── sync ─────────────────────────────────────────────────────────────


def _git_head_ts(repo_dir: Path) -> int | None:
    """Return the unix epoch of the local HEAD commit.

    Spark sync compares this timestamp against the previously-indexed value
    to decide whether a repo needs re-embedding. We deliberately do NOT
    fetch from origin here — the spark container has /repos mounted
    read-only and no git credentials. Keeping HEAD up to date is the host's
    responsibility (typically via a separate nightly repo-sync job).

    Returns None on any failure (missing HEAD, broken repo, etc.).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "-1", "--format=%ct", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return int(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
        return None


def _git_clone_url(repo_dir: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def sync_installations(
    config: SparkConfig,
    dry_run: bool = False,
    path_filter: str | None = None,
) -> dict[str, int]:
    """Incremental sync: re-index only installations whose origin/HEAD has moved.

    Discovers installations under config.installations_path, classifies each as
    `up-to-date`, `changed`, `new`, `removed`, or `fetch-failed`, then runs
    activate_installation() for changed/new and deletes rows for removed.
    Per-installation failures are logged and skipped; one failure does not
    abort the run.

    When `path_filter` is set, only installations whose rel_path starts with
    the prefix are considered. Useful for scoped verification or surgical
    re-indexes; in this mode `removed` is never reported (we can't tell if a
    repo outside the filter was actually deleted).

    Returns a dict of classification counts. With dry_run=True, performs
    classification only — no activation, no prune, no metadata write.
    """
    from spark.indexer.metadata import InstallationMeta, load_metadata, save_metadata

    start = time.time()
    meta = load_metadata(config.metadata_path)
    discovered = _discover_installations(config)
    if path_filter:
        discovered = [(p, r) for p, r in discovered if r.startswith(path_filter)]
    discovered_by_rel = {rel: repo_dir for repo_dir, rel in discovered}

    if path_filter:
        # Confine metadata operations to the filter scope so unrelated repos
        # aren't classified as removed.
        scoped_meta_keys = {k for k in meta.keys() if k.startswith(path_filter)}
        known_rels = scoped_meta_keys
    else:
        known_rels = set(meta.keys())
    found_rels = set(discovered_by_rel.keys())
    new_rels = sorted(found_rels - known_rels)
    removed_rels = sorted(known_rels - found_rels)
    present_rels = sorted(found_rels & known_rels)

    # Classification: "head-read-failed" replaces "fetch-failed" now that we
    # only read local HEAD. Same shape externally so the summary line stays
    # backward-compatible — failures are still rare and surfaced as warnings.
    counts = {"up-to-date": 0, "changed": 0, "new": 0, "removed": 0, "fetch-failed": 0}
    chunks_written = 0

    # 1. New installations — full index
    for rel in new_rels:
        repo_dir = discovered_by_rel[rel]
        logger.info("sync: %s -> new", rel)
        if dry_run:
            counts["new"] += 1
            continue
        try:
            head_ts = _git_head_ts(repo_dir)
            result = activate_installation(config, repo_dir, rel, verbose=False)
            chunks_written += result.get("chunks", 0)
            meta[rel] = InstallationMeta(
                indexed_at=datetime.now(timezone.utc).isoformat(),
                last_remote_ts=head_ts or 0,
                clone_url=_git_clone_url(repo_dir),
            )
            counts["new"] += 1
        except Exception as e:
            logger.warning("sync: %s activation failed: %s", rel, e)

    # 2. Present installations — compare local HEAD against stored timestamp
    for rel in present_rels:
        repo_dir = discovered_by_rel[rel]
        head_ts = _git_head_ts(repo_dir)
        if head_ts is None:
            logger.warning("sync: %s -> head-read-failed (left at last indexed state)", rel)
            counts["fetch-failed"] += 1
            continue
        prior = meta[rel]
        if head_ts <= prior.last_remote_ts:
            logger.info("sync: %s -> up-to-date", rel)
            counts["up-to-date"] += 1
            continue
        logger.info("sync: %s -> changed (HEAD ts %d -> %d)", rel, prior.last_remote_ts, head_ts)
        if dry_run:
            counts["changed"] += 1
            continue
        try:
            result = activate_installation(config, repo_dir, rel, verbose=False)
            chunks_written += result.get("chunks", 0)
            meta[rel] = InstallationMeta(
                indexed_at=datetime.now(timezone.utc).isoformat(),
                last_remote_ts=head_ts,
                clone_url=_git_clone_url(repo_dir) or prior.clone_url,
            )
            counts["changed"] += 1
        except Exception as e:
            logger.warning("sync: %s activation failed: %s — prior metadata retained", rel, e)

    # 3. Removed installations — prune from LanceDB and metadata
    if removed_rels and not dry_run:
        try:
            db = lancedb.connect(str(config.index_path))
            table = db.open_table(TABLE_NAME)
        except Exception as e:
            logger.warning("sync: cannot open LanceDB to prune removed installations: %s", e)
            table = None
        for rel in removed_rels:
            logger.info("sync: %s -> removed", rel)
            if table is None:
                continue
            try:
                table.delete(f'installation_path = "{rel}"')
                meta.pop(rel, None)
                counts["removed"] += 1
            except Exception as e:
                logger.warning("sync: %s prune failed: %s — metadata entry retained for retry", rel, e)
    elif dry_run:
        for rel in removed_rels:
            logger.info("sync: %s -> removed (dry-run)", rel)
            counts["removed"] += 1

    # 4. Persist + summary
    if not dry_run:
        try:
            save_metadata(config.metadata_path, meta)
        except OSError as e:
            logger.error("sync: failed to write %s: %s", config.metadata_path, e)

    elapsed = time.time() - start
    logger.info(
        "sync: complete up-to-date=%d changed=%d new=%d removed=%d fetch-failed=%d "
        "chunks_written=%d elapsed=%.1fs%s",
        counts["up-to-date"], counts["changed"], counts["new"],
        counts["removed"], counts["fetch-failed"], chunks_written, elapsed,
        " (dry-run)" if dry_run else "",
    )
    return counts
