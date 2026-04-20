"""MCP server for agent-memory — exposes memory tools to Claude Code agents.

Reads DATABASE_URL from environment (or .env in the project root).
Reads OPENAI_API_KEY (or LITELLM_API_KEY) for embedding generation.

Tools:
  log_event      — fire-and-forget event logging (tmux hooks, session tracking)
  create_note    — write a curated memory node (decisions, insights, checkpoints)
  query_notes    — search curated notes by project + tags
  search_similar — semantic search over notes using embeddings
  query_entity   — look up an entity and all notes that reference it (backlinks)
  recent_events  — query raw event log by project + time window
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Lazy DB connection ────────────────────────────────────────────────────────
# Pool and store are created on the first tool call, not at startup.
# This keeps MCP initialize + tools/list instant for health checks.

_pool = None
_store = None
_pool_lock = None  # asyncio.Lock — created lazily (no event loop at import time)


def _db_url() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required. "
            "Set it in the project .env or export it before starting the server."
        )
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


async def _get_store():
    """Return the MemoryStore, connecting to Postgres on first call."""
    global _pool, _store, _pool_lock
    import asyncio

    if _pool_lock is None:
        _pool_lock = asyncio.Lock()

    async with _pool_lock:
        if _store is None:
            import psycopg_pool
            from agent_memory.backends.postgres import PostgresMemoryBackend
            from agent_memory.store import MemoryStore

            _pool = psycopg_pool.AsyncConnectionPool(_db_url(), min_size=1, max_size=5, open=False)
            await _pool.open()
            _store = MemoryStore(PostgresMemoryBackend(pool=_pool))
            logger.info("agent-memory: connected to Postgres")
    return _store


async def _get_pool():
    await _get_store()  # ensure pool is initialized
    return _pool


# ── Embedding helper ─────────────────────────────────────────────────────────
# Lazy singleton — only instantiated when embeddings are first requested.
# Graceful degradation: if no API key is set, embedding calls are skipped
# and notes are still created without vectors (tag/text search still works).

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from agent_memory.embeddings import LiteLLMEmbeddingProvider
        _embedder = LiteLLMEmbeddingProvider()
    return _embedder


async def _embed(text: str) -> list[float] | None:
    """Return embedding vector or None if embedding fails.

    Default uses Ollama (nomic-embed-text) which requires no API key.
    Override with EMBEDDING_MODEL env var for cloud providers.
    """
    try:
        return await _get_embedder().embed(text)
    except Exception as exc:
        logger.warning("embedding failed, skipping: %s", exc)
        return None


# ── Server ────────────────────────────────────────────────────────────────────

from fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("agent-memory")


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def log_event(
    event_type: str,
    project: str,
    details: dict[str, Any] | None = None,
    device: str = "",
    repo: str = "",
    branch: str = "",
    agent_slot: str = "",
    session_id: str = "",
) -> str:
    """Log a raw event for a project — session lifecycle, tool use, errors, etc.

    This is fire-and-forget. Use it from tmux hooks or to track agent activity.
    Not for curated notes — use create_note for those.

    event_type: session_start | session_end | tool_use | permission_wait |
                file_write | commit | error | checkpoint
    details: arbitrary key/value payload (tool name, file paths, error message, etc.)
    device: machine identifier (e.g. "mac-laptop", "windows-desktop")
    repo: repository name
    branch: current git branch
    agent_slot: tmux window index (from the agent registry)
    session_id: Claude session ID if available
    """
    pool = await _get_pool()
    event_id = uuid.uuid4().hex[:12]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO agents.memory_events
                (id, project, event_type, device, repo, branch, agent_slot, session_id, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                project,
                event_type,
                device,
                repo,
                branch,
                agent_slot,
                session_id or None,
                json.dumps(details or {}),
            ),
        )
    return f"logged:{event_id}"


@mcp.tool()
async def create_note(
    content: str,
    project: str,
    tags: list[str] | None = None,
    title: str = "",
    links: list[str] | None = None,
    session_id: str = "",
) -> str:
    """Write a curated memory note — a decision, insight, checkpoint, or finding.

    Notes are persistent and searchable. Use them to capture things worth
    remembering across sessions: architectural decisions, discovered constraints,
    completed work summaries, open questions.

    Prefer tags from the controlled vocabulary:
      Domain:  auth, api, database, frontend, backend, infra, testing, deployment
      Action:  bug, fix, refactor, feature, breaking-change, decision, investigation
      Outcome: approved, merged, deployed, reverted, wontfix

    links: explicit entity names to link this note to (file paths, module names, repo names).
           File paths, [[wikilinks]], and @mentions in the note content are also
           extracted and linked automatically. Enables backlink queries like
           "everything that mentions svc-chatbot".

    session_id: optional Claude session ID — used to auto-link notes created in
                the same session with temporal edges, so the session's knowledge
                arc is traversable. Pass CLAUDE_SESSION_ID env var if available.
    """
    from agent_memory.entity_extraction import extract_entities
    from agent_memory.tags import normalize_tags
    from agent_memory.types import MemoryNode

    store = await _get_store()
    node_id = uuid.uuid4().hex[:12]
    node = MemoryNode(
        id=node_id,
        content=content,
        title=title or None,
        tags=normalize_tags(tags or []),
        project=project,
        source_job_id=session_id or None,
    )
    await store.create_node(node)

    # Generate and store embedding asynchronously (best-effort).
    embedding = await _embed(content)
    if embedding:
        pool = await _get_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE agents.memory_nodes SET embedding = %s::vector WHERE id = %s",
                (str(embedding), node_id),
            )

    # Create temporal links to sibling notes from the same session.
    temporal_count = 0
    if session_id:
        siblings = await store.get_session_notes(session_id, project, exclude_id=node_id)
        for sibling in siblings:
            await store.create_link(node_id, sibling.id, "temporal")
            await store.create_link(sibling.id, node_id, "temporal")
            temporal_count += 1

    # Merge explicit links with auto-extracted entity references.
    # entity_name → entity_type; explicit links win on type (None = unknown).
    all_links: dict[str, str | None] = {name: None for name in (links or [])}
    for entity_name, entity_type in extract_entities(content):
        if entity_name not in all_links:
            all_links[entity_name] = entity_type

    for entity_name, entity_type in all_links.items():
        await store.ensure_entity(entity_name, project, entity_type)
        await store.create_link(node_id, entity_name, "mentions")

    auto_count = sum(1 for k in all_links if k not in (links or []))
    embedded = "yes" if embedding else "no"
    return f"created:{node_id} entities:{len(all_links)} (auto:{auto_count}) embedded:{embedded} temporal:{temporal_count}"


@mcp.tool()
async def query_notes(
    project: str,
    tags: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search curated memory notes for a project.

    With tags: returns notes that have ANY of the specified tags (overlap match).
    Without tags: returns the most recent notes for the project.

    Results are ordered most-recent first.
    """
    store = await _get_store()
    pool = await _get_pool()
    if tags:
        nodes = await store.query_by_tags(project, tags, limit=limit)
    else:
        # No tag filter — fetch all notes for project, most recent first
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, content, title, tags, access_count, created_at
                FROM agents.memory_nodes
                WHERE project = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (project, limit),
            )
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0],
                    "content": r[1],
                    "title": r[2] or "",
                    "tags": list(r[3]) if r[3] else [],
                    "access_count": r[4] or 0,
                    "created_at": str(r[5]),
                }
                for r in rows
            ]

    return [
        {
            "id": n.id,
            "content": n.content,
            "title": n.title or "",
            "tags": n.tags,
            "access_count": n.access_count,
            "created_at": n.created_at,
        }
        for n in nodes
    ]


@mcp.tool()
async def search_similar(
    query: str,
    project: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Semantic search over memory notes using embeddings.

    Finds notes whose meaning is similar to the query, even if they don't
    share the same keywords. Best for open-ended questions like:
      "What decisions were made about rate limiting?"
      "Notes about authentication failures"
      "What do we know about the billing pipeline?"

    Falls back to recency-sorted results if embeddings are unavailable
    (no OPENAI_API_KEY set, or no notes have been embedded yet).
    """
    embedding = await _embed(query)
    if embedding is None:
        # Graceful degradation: return most recent notes with a warning
        store = await _get_store()
        nodes = await store.query_by_tags(project, [], limit=limit)
        if not nodes:
            pool = await _get_pool()
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, content, title, tags, access_count, created_at FROM agents.memory_nodes WHERE project = %s ORDER BY created_at DESC LIMIT %s",
                    (project, limit),
                )
                rows = await cur.fetchall()
                return [{"warning": "embedding unavailable — showing recent notes", "id": r[0], "content": r[1], "title": r[2] or "", "tags": list(r[3]) if r[3] else [], "score": None, "created_at": str(r[5])} for r in rows]
        return [{"warning": "embedding unavailable — showing recent notes", "id": n.id, "content": n.content, "title": n.title or "", "tags": n.tags, "score": None, "created_at": n.created_at} for n in nodes]

    store = await _get_store()
    nodes = await store.query_by_similarity(project, embedding, limit=limit)
    return [
        {
            "id": n.id,
            "content": n.content,
            "title": n.title or "",
            "tags": n.tags,
            "score": None,  # cosine distance ordering from DB; raw score not exposed by backend
            "created_at": n.created_at,
        }
        for n in nodes
    ]


@mcp.tool()
async def query_entity(
    name: str,
    project: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Look up an entity and all notes that reference it (backlinks).

    Use to answer questions like:
      "What do we know about auth-module?"
      "Which notes mention svc-chatbot?"
      "Everything that references src/billing/invoice.py"

    Returns the entity record (if known) plus all linked notes, newest first.
    An empty notes list means the entity has been seen but no notes reference it yet.
    A null entity means the name has never been recorded — returns an empty notes list.
    """
    # Entity metadata
    store = await _get_store()
    pool = await _get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, name, entity_type, project, first_seen, attributes
            FROM agents.memory_entities
            WHERE name = %s AND project = %s
            """,
            (name, project),
        )
        row = await cur.fetchone()

    entity = None
    if row:
        attrs = row[5]
        if isinstance(attrs, str):
            attrs = json.loads(attrs or "{}")
        entity = {
            "id": row[0],
            "name": row[1],
            "entity_type": row[2] or "",
            "project": row[3],
            "first_seen": str(row[4]),
            "attributes": attrs or {},
        }

    # Backlinked notes
    nodes = await store.get_backlinks(name, project, limit=limit)
    if nodes:
        await store.increment_access(nodes[0].id)

    return {
        "entity": entity,
        "notes": [
            {
                "id": n.id,
                "title": n.title or "",
                "content": n.content,
                "tags": n.tags,
                "created_at": n.created_at,
            }
            for n in nodes
        ],
    }


@mcp.tool()
async def recent_events(
    project: str,
    event_type: str | None = None,
    hours: int = 24,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query the raw event log for a project.

    Useful for understanding recent agent activity:
      recent_events("svc-chatbot", hours=48)
      recent_events("svc-chatbot", event_type="session_start")

    event_type filter is optional. Results are newest first.
    """
    params: list[Any] = [project, hours]
    type_clause = ""
    if event_type:
        type_clause = "AND event_type = %s"
        params.append(event_type)
    params.append(limit)

    pool = await _get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT id, timestamp, event_type, device, repo, branch, agent_slot, session_id, payload
            FROM agents.memory_events
            WHERE project = %s
              AND timestamp > now() - (%s * interval '1 hour')
              {type_clause}
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            params,
        )
        rows = await cur.fetchall()

    return [
        {
            "id": row[0],
            "timestamp": str(row[1]),
            "event_type": row[2],
            "device": row[3] or "",
            "repo": row[4] or "",
            "branch": row[5] or "",
            "agent_slot": row[6] or "",
            "session_id": row[7] or "",
            "details": row[8] if isinstance(row[8], dict) else json.loads(row[8] or "{}"),
        }
        for row in rows
    ]


@mcp.tool()
async def query_session(
    session_id: str,
    project: str,
) -> list[dict[str, Any]]:
    """Return all notes created in a specific session, in chronological order.

    Shows the arc of knowledge produced during one Claude session — useful for
    understanding what was decided, discovered, or completed in a single run.

    session_id: the Claude session ID (CLAUDE_SESSION_ID env var)
    """
    pool = await _get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, title, content, tags, created_at
            FROM agents.memory_nodes
            WHERE source_job_id = %s AND project = %s
            ORDER BY created_at ASC
            """,
            (session_id, project),
        )
        rows = await cur.fetchall()

    return [
        {
            "id": row[0],
            "title": row[1] or "",
            "content": row[2],
            "tags": list(row[3]) if row[3] else [],
            "created_at": str(row[4]),
        }
        for row in rows
    ]


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    """Start the MCP server (stdio transport — Claude Code spawns this process)."""
    # Load .env from project root if present
    _env_path = Path(__file__).parent.parent.parent / ".env"
    if _env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env_path)
        except ImportError:
            pass  # python-dotenv optional — fall back to env vars already set

    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
