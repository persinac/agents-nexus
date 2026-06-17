"""Chunker — breaks installations into indexable pieces.

Four-layer chunking strategy:
  Layer 1: Monitor logs (repo summaries) — one chunk per installation
  Layer 2: File-level symbols (tree-sitter) or sliding windows (fallback)
  Layer 3: Merge requests — recent merged MRs per installation

At search time, Layer 1 is used as a coarse filter (which repos?),
then Layers 2-3 are searched within the matching repos (which code/changes?).
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from spark.indexer.keywords import extract_domain_keywords
from spark.indexer.symbol_parser import parse_symbols

from spark.config import SparkConfig

if TYPE_CHECKING:
    from spark.detector import DetectedProject
    from spark.gitlab import GitLabProject

# Embedding model limits (Ollama nomic-embed-text safe max ~1000 chars)
CHUNK_SIZE = 900  # chars per window (leaves room for header)
CHUNK_OVERLAP = 150  # overlap between windows
SUMMARY_MAX_CHARS = 950  # max summary size
MR_CHUNK_MAX_CHARS = 900  # max chars per MR chunk


@dataclass
class Chunk:
    """A single indexable piece of an installation."""

    id: str  # unique: "{installation}::summary", "{installation}::{file}::0"
    installation: str  # repo name (e.g., "svc-chatbot")
    installation_path: str  # relative path from repos root
    team: str  # resolved team name
    chunk_type: str  # "summary" or "file"
    file_path: str  # relative file path within repo, or "" for summaries
    content: str  # the actual text content
    # Symbol metadata (populated for chunk_type="symbol" only)
    symbol_name: str = ""
    symbol_type: str = ""
    # Decision synthesis metadata (populated for chunk_type="decision" only)
    decision_date: str = ""
    decision_author: str = ""
    mr_url: str = ""
    # GitLab metadata (populated when GitLab enrichment is enabled)
    description: str = ""
    topics: str = ""  # comma-separated
    languages: str = ""  # top languages, e.g. "Python 85%, TypeScript 15%"
    last_activity: str = ""  # ISO date
    archived: bool = False
    web_url: str = ""
    # Detected project metadata (populated by detector)
    framework: str = ""
    ci_type: str = ""
    deploy_target: str = ""
    test_command: str = ""
    lint_command: str = ""
    clone_url: str = ""
    services: str = ""  # comma-separated tech/service tags, e.g. "cognito, oauth, s3"


# Files that contribute to a repo summary (priority order)
_SUMMARY_FILES = [
    "CLAUDE.md",
    "README.md",
    "readme.md",
    "README.rst",
    "pyproject.toml",
    "package.json",
    "main.tf",
    "variables.tf",
    "outputs.tf",
    ".gitlab-ci.yml",
    "Dockerfile",
    "Taskfile.yml",
    "Makefile",
]

# Subdirectories crawled for additional summary context after _SUMMARY_FILES,
# when budget remains. These often hold richer prose than root config files.
_SUMMARY_DOC_DIRS = ["docs", "notes", "doc", ".ai", ".ai/research"]
_MAX_SUMMARY_DOC_FILES = 6


def _should_exclude_dir(dirname: str, exclude_dirs: list[str]) -> bool:
    return dirname in exclude_dirs


def _matches_patterns(filename: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(filename, p) for p in patterns)


def _split_into_windows(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping windows.

    Returns at least one window, even for short text.
    """
    if len(text) <= chunk_size:
        return [text]

    windows = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        window = text[start:end]
        windows.append(window)
        if end >= len(text):
            break
        start = end - overlap

    return windows


def build_summary_chunk(
    repo_dir: Path,
    installation_path: str,
    installation: str,
    team: str,
    gitlab_project: GitLabProject | None = None,
    detected: DetectedProject | None = None,
    config: SparkConfig | None = None,
) -> Chunk:
    """Build a monitor-log (summary) chunk for an installation.

    Assembles key files into a single summary, respecting the embedding
    model's character limit. Prioritizes CLAUDE.md and README over config files.
    When GitLab/detected metadata is available, prepends a structured header.
    """
    parts = [f"# {installation}", f"Team: {team}", f"Path: {installation_path}"]
    # Add parent directory names as searchable aliases so queries mentioning
    # a parent group (e.g. "flashback fleet") can match nested repos
    path_segments = [s.replace("-", " ") for s in installation_path.split("/") if s != installation]
    if path_segments:
        parts.append(f"Groups: {', '.join(path_segments)}")

    # GitLab metadata fields for the Chunk dataclass
    gl_description = ""
    gl_topics = ""
    gl_languages = ""
    gl_last_activity = ""
    gl_archived = False
    gl_web_url = ""

    if gitlab_project:
        if gitlab_project.description:
            parts.append(f"Description: {gitlab_project.description}")
            gl_description = gitlab_project.description
        if gitlab_project.topics:
            gl_topics = ", ".join(gitlab_project.topics)
            parts.append(f"Topics: {gl_topics}")
        top_langs = sorted(gitlab_project.languages.items(), key=lambda x: -x[1])[:3]
        if top_langs:
            gl_languages = ", ".join(f"{lang} {pct:.0f}%" for lang, pct in top_langs)
            parts.append(f"Languages: {gl_languages}")
        if gitlab_project.last_activity:
            gl_last_activity = gitlab_project.last_activity[:10]
            parts.append(f"Last active: {gl_last_activity}")
        if gitlab_project.archived:
            gl_archived = True
            parts.append("Status: ARCHIVED")
        gl_web_url = gitlab_project.web_url

    # Detected project metadata
    det_framework = ""
    det_ci_type = ""
    det_deploy_target = ""
    det_test_command = ""
    det_lint_command = ""
    det_clone_url = ""
    det_services = ""

    if detected:
        det_framework = detected.framework
        det_ci_type = detected.ci_type
        det_deploy_target = detected.deploy_target
        det_test_command = detected.test_command
        det_lint_command = detected.lint_command
        det_clone_url = detected.clone_url
        det_services = ", ".join(detected.services)

        # Add detected info to summary content for embedding
        det_parts = []
        if detected.framework:
            det_parts.append(f"Framework: {detected.framework}")
        if detected.ci_type:
            det_parts.append(f"CI: {detected.ci_type}")
        if detected.deploy_target:
            det_parts.append(f"Deploy: {detected.deploy_target}")
        if det_parts:
            parts.append(" | ".join(det_parts))
        cmd_parts = []
        if detected.test_command:
            cmd_parts.append(f"Test: {detected.test_command}")
        if detected.lint_command:
            cmd_parts.append(f"Lint: {detected.lint_command}")
        if cmd_parts:
            parts.append(" | ".join(cmd_parts))
        # Surface detected services so broad queries ("cognito", "redis") match
        # this repo via both vector and BM25 search (BM25 indexes `content`).
        if det_services:
            parts.append(f"Services: {det_services}")

    # Surface in-code domain vocabulary (data-source / enum / status constants)
    # that prose summaries miss — e.g. SOURCE_POE="poe" makes "which repo
    # ingests POE" route here via Stage-1. Placed high so it's always in budget
    # and indexed by both vector + BM25. See indexer/keywords.py.
    keywords = extract_domain_keywords(repo_dir, config) if config is not None else []
    if keywords:
        parts.append(f"Keywords: {', '.join(keywords)}")

    parts.append("")
    summary_max_chars = config.summary_max_chars if config is not None else SUMMARY_MAX_CHARS
    budget = summary_max_chars - len("\n".join(parts))

    for summary_file in _SUMMARY_FILES:
        fpath = repo_dir / summary_file
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue

        section = f"## {summary_file}\n{content}\n"
        if len(section) > budget:
            section = section[:budget]
            parts.append(section)
            break
        parts.append(section)
        budget -= len(section)
        if budget <= 100:
            break

    # Secondary crawl: docs/notes often hold the richest description of what a
    # repo does (e.g. OAuth flow docs). If summary budget remains, append the
    # head of a few markdown files from docs/, notes/, doc/.
    budget = summary_max_chars - len("\n".join(parts))
    if budget > 100:
        doc_files: list[Path] = []
        for doc_dir in _SUMMARY_DOC_DIRS:
            dpath = repo_dir / doc_dir
            if dpath.is_dir():
                doc_files.extend(sorted(dpath.glob("*.md")))
        for fpath in doc_files[:_MAX_SUMMARY_DOC_FILES]:
            if budget <= 100:
                break
            try:
                content = fpath.read_text(errors="replace")
            except OSError:
                continue
            rel = fpath.relative_to(repo_dir)
            section = f"## {rel}\n{content}\n"
            if len(section) > budget:
                section = section[:budget]
            parts.append(section)
            budget -= len(section)

    return Chunk(
        id=f"{installation}::summary",
        installation=installation,
        installation_path=installation_path,
        team=team,
        chunk_type="summary",
        file_path="",
        content="\n".join(parts),
        description=gl_description,
        topics=gl_topics,
        languages=gl_languages,
        last_activity=gl_last_activity,
        archived=gl_archived,
        web_url=gl_web_url,
        framework=det_framework,
        ci_type=det_ci_type,
        deploy_target=det_deploy_target,
        test_command=det_test_command,
        lint_command=det_lint_command,
        clone_url=det_clone_url,
        services=det_services,
    )


def build_file_chunks(
    repo_dir: Path,
    installation_path: str,
    installation: str,
    team: str,
    config: SparkConfig,
) -> list[Chunk]:
    """Build file-level chunks using sliding windows."""
    chunks = []
    file_count = 0

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [
            d for d in dirs if not _should_exclude_dir(d, config.exclude_dirs)
        ]
        for filename in sorted(files):
            if not _matches_patterns(filename, config.include_patterns):
                continue
            filepath = Path(root) / filename
            try:
                if filepath.stat().st_size > config.max_file_size:
                    continue
            except OSError:
                continue

            relative = filepath.relative_to(repo_dir)
            try:
                content = filepath.read_text(errors="replace")
            except OSError:
                continue

            # Header uses some of our char budget, keep it short
            header = f"# {installation} — {relative}\n"

            windows = _split_into_windows(content, chunk_size=config.chunk_size, overlap=config.chunk_overlap)
            for window_idx, window in enumerate(windows):
                chunks.append(
                    Chunk(
                        id=f"{installation}::{relative}::{window_idx}",
                        installation=installation,
                        installation_path=installation_path,
                        team=team,
                        chunk_type="file",
                        file_path=str(relative),
                        content=header + window,
                    )
                )

            file_count += 1
            if file_count >= config.max_files_per_installation:
                return chunks

    return chunks


def _truncate_to_chunk_size(text: str, chunk_size: int = CHUNK_SIZE) -> str:
    """Truncate text to chunk_size at a word boundary, appending '… [truncated]'."""
    if len(text) <= chunk_size:
        return text
    cut = text[:chunk_size].rsplit(None, 1)[0]  # rsplit on whitespace to find word boundary
    return cut + "… [truncated]"


def build_symbol_chunks(
    repo_dir: Path,
    installation_path: str,
    installation: str,
    team: str,
    config: SparkConfig,
) -> list[Chunk]:
    """Build symbol-level chunks using tree-sitter AST extraction.

    For each eligible source file, attempts to parse and extract top-level
    symbols (functions, classes, methods, Terraform resources, etc.). Each
    symbol becomes one chunk. If a file produces no symbols (unsupported
    extension, parse error, or no top-level symbols), it falls back to
    sliding-window chunks identical to build_file_chunks().
    """
    chunks = []
    file_count = 0

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if not _should_exclude_dir(d, config.exclude_dirs)]
        for filename in sorted(files):
            if not _matches_patterns(filename, config.include_patterns):
                continue
            filepath = Path(root) / filename
            try:
                if filepath.stat().st_size > config.max_file_size:
                    continue
            except OSError:
                continue

            relative = filepath.relative_to(repo_dir)
            symbols = parse_symbols(filepath)

            if symbols:
                for sym in symbols:
                    header = f"# {installation} — {relative} [{sym['type']}: {sym['name']}]\n"
                    content = _truncate_to_chunk_size(header + sym["source_text"], chunk_size=config.chunk_size)
                    chunks.append(Chunk(
                        id=f"{installation}::{relative}::{sym['type']}::{sym['name']}",
                        installation=installation,
                        installation_path=installation_path,
                        team=team,
                        chunk_type="symbol",
                        file_path=str(relative),
                        content=content,
                        symbol_name=sym["name"],
                        symbol_type=sym["type"],
                    ))
            else:
                # Fallback: sliding-window chunks for this file
                try:
                    content = filepath.read_text(errors="replace")
                except OSError:
                    continue
                header = f"# {installation} — {relative}\n"
                windows = _split_into_windows(content, chunk_size=config.chunk_size, overlap=config.chunk_overlap)
                for window_idx, window in enumerate(windows):
                    chunks.append(Chunk(
                        id=f"{installation}::{relative}::{window_idx}",
                        installation=installation,
                        installation_path=installation_path,
                        team=team,
                        chunk_type="file",
                        file_path=str(relative),
                        content=header + window,
                    ))

            file_count += 1
            if file_count >= config.max_files_per_installation:
                return chunks

    return chunks


def build_mr_chunks(
    installation: str,
    installation_path: str,
    team: str,
    merge_requests: list[dict],
    config: SparkConfig | None = None,
) -> list[Chunk]:
    """Build chunks from recent merge requests.

    Groups MRs into chunks that fit the embedding model's char limit.
    Each chunk gets a header with repo context.
    """
    if not merge_requests:
        return []

    header = f"# {installation} — Recent Merge Requests\nTeam: {team}\n\n"
    chunks: list[Chunk] = []
    current_text = header
    chunk_idx = 0

    for mr in merge_requests:
        # Format: title + truncated description + metadata
        desc = mr["description"][:200] if mr["description"] else ""
        merged_at = mr["merged_at"][:10] if mr["merged_at"] else ""
        entry = f"- **{mr['title']}** ({merged_at}, @{mr['author']})\n"
        if desc:
            entry += f"  {desc}\n"

        # If adding this MR would exceed limit, flush current chunk
        mr_chunk_max_chars = config.mr_chunk_max_chars if config is not None else MR_CHUNK_MAX_CHARS
        if len(current_text) + len(entry) > mr_chunk_max_chars and current_text != header:
            chunks.append(Chunk(
                id=f"{installation}::mrs::{chunk_idx}",
                installation=installation,
                installation_path=installation_path,
                team=team,
                chunk_type="merge_request",
                file_path="",
                content=current_text,
            ))
            chunk_idx += 1
            current_text = header

        current_text += entry

    # Flush remaining
    if current_text != header:
        chunks.append(Chunk(
            id=f"{installation}::mrs::{chunk_idx}",
            installation=installation,
            installation_path=installation_path,
            team=team,
            chunk_type="merge_request",
            file_path="",
            content=current_text,
        ))

    return chunks


def chunk_installation(
    repo_dir: Path,
    installation_path: str,
    config: SparkConfig,
    gitlab_project: GitLabProject | None = None,
    detected: DetectedProject | None = None,
    merge_requests: list[dict] | None = None,
) -> list[Chunk]:
    """Produce all chunks for a single installation."""
    installation = repo_dir.name
    team = config.resolve_team(installation_path)

    chunks = [build_summary_chunk(
        repo_dir, installation_path, installation, team, gitlab_project, detected, config,
    )]
    if config.symbol_chunking_enabled:
        chunks.extend(build_symbol_chunks(repo_dir, installation_path, installation, team, config))
    else:
        chunks.extend(build_file_chunks(repo_dir, installation_path, installation, team, config))
    if merge_requests:
        chunks.extend(build_mr_chunks(installation, installation_path, team, merge_requests, config))
    return chunks
