"""FastMCP Server — the Spark interface.

'I am 127 Guilty Spark. I will be happy to assist you.'

Search uses a two-stage retrieval strategy:
  Stage 1 (coarse): Search summary chunks to identify relevant installations
  Stage 2 (fine):   Search file chunks within those installations for specific code
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import lancedb
from fastmcp import FastMCP

from spark.config import SparkConfig
from spark.indexer import reranker as _reranker_mod
from spark.indexer.builder import TABLE_NAME
from spark.indexer.embedder import embed_single

_LOG_FILE = Path(__file__).resolve().parents[3] / "spark-mcp.log"
logger = logging.getLogger("spark.server")
logger.setLevel(logging.INFO)
_fh = logging.FileHandler(_LOG_FILE)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s — %(message)s", datefmt="%m/%d/%y %H:%M:%S"))
logger.addHandler(_fh)

config = SparkConfig.load()
db = lancedb.connect(str(config.index_path))

mcp = FastMCP(
    "127 Guilty Spark",
    instructions=(
        "I am 127 Guilty Spark, the Monitor of the installation index. "
        "I can search across all repositories to find relevant code, services, "
        "terraform modules, and documentation. "
        "Use 'spark' for broad queries (which repo does X?). "
        "Use 'spark_deep' when you need to find specific code within repos. "
        "Use 'recent_changes' to see what's been changing in a specific repo. "
        "Use 'installation_summary' when you know the repo name and want details. "
        "Use 'search_decisions' for 'why' questions: why was X built this way, "
        "what was decided about Y, what alternatives were considered for Z."
    ),
)


def _maybe_rerank(query: str, rows: list[dict]) -> list[dict]:
    if config.reranker_enabled:
        return _reranker_mod.rerank(query, rows, config.reranker_model)
    return rows


def _search(table, query: str, query_vector: list, where: str, limit: int) -> list[dict]:
    """Run hybrid (vector + FTS) search with RRF, falling back to vector-only."""
    if config.hybrid_search_enabled:
        try:
            return (
                table.search(query_type="hybrid")
                .vector(query_vector)
                .text(query)
                .where(where, prefilter=False)
                .rerank()
                .limit(limit)
                .to_list()
            )
        except Exception as exc:
            logger.warning("hybrid search failed, falling back to vector-only: %s", exc)
    return table.search(query_vector).where(where).limit(limit).to_list()


def _format_results(rows: list[dict], include_content: bool = True) -> str:
    if not rows:
        return "No results found."

    output = []
    for i, row in enumerate(rows):
        if "_relevance_score" in row:
            score = row["_relevance_score"]
        else:
            score = 1 - row.get("_distance", 0)
        header = f"### [{i + 1}] {row['installation']} (score: {score:.3f})"
        meta = f"Team: {row['team']} | Type: {row['chunk_type']} | Path: {row['installation_path']}"
        if row.get("file_path"):
            meta += f" | File: {row['file_path']}"
        if row.get("symbol_name"):
            meta += f" | Symbol: {row['symbol_type']} {row['symbol_name']}"

        # GitLab metadata (only present on summary chunks)
        gl_meta = ""
        if row.get("description"):
            gl_meta += f"\nDescription: {row['description']}"
        if row.get("topics"):
            gl_meta += f"\nTopics: {row['topics']}"
        if row.get("languages"):
            gl_meta += f"\nLanguages: {row['languages']}"
        # Detected project metadata
        det_parts = []
        if row.get("framework"):
            det_parts.append(f"Framework: {row['framework']}")
        if row.get("ci_type"):
            det_parts.append(f"CI: {row['ci_type']}")
        if row.get("deploy_target"):
            det_parts.append(f"Deploy: {row['deploy_target']}")
        if det_parts:
            gl_meta += "\n" + " | ".join(det_parts)
        if row.get("services"):
            gl_meta += f"\nServices: {row['services']}"
        if row.get("last_activity"):
            gl_meta += f"\nLast active: {row['last_activity']}"
        if row.get("archived"):
            gl_meta += "\nStatus: ARCHIVED"

        if include_content:
            content = row["content"][:2000]
            if len(row["content"]) > 2000:
                content += "\n... [truncated]"
            output.append(f"{header}\n{meta}{gl_meta}\n\n{content}")
        else:
            output.append(f"{header}\n{meta}{gl_meta}")

    return "\n\n---\n\n".join(output)


@mcp.tool()
def spark(
    query: str,
    top_k: int = 10,
    team: str | None = None,
) -> str:
    """Search for relevant repos/installations by semantic similarity.

    This searches repo SUMMARIES (monitor-logs) to answer questions like
    'which repo handles X?' or 'what terraform module manages Y?'.

    Args:
        query: Natural language query (e.g., 'EKS cluster provisioning',
               'chatbot zendesk integration', 'S3 bucket terraform')
        top_k: Number of results to return (default 10)
        team: Optional team filter (e.g., 'Platform - Infrastructure', 'Search - Concierge')
    """
    logger.info(f"spark: query={query!r} top_k={top_k} team={team}")
    table = db.open_table(TABLE_NAME)
    query_vector = embed_single(query, config)

    where = 'chunk_type = "summary"'
    if team:
        where += f' AND team = "{team}"'
    fetch_k = top_k * config.reranker_top_k_multiplier if config.reranker_enabled else top_k
    rows = _search(table, query, query_vector, where, fetch_k)
    rows = _maybe_rerank(query, rows)[:top_k]

    logger.info(f"spark: returned {len(rows)} results")
    return _format_results(rows)


@mcp.tool()
def spark_deep(
    query: str,
    installations: list[str] | None = None,
    team: str | None = None,
    top_k: int = 10,
) -> str:
    """Two-stage deep search — finds specific code/files across repos.

    Stage 1: If no installations specified, searches summaries to find the
             top 5 most relevant repos.
    Stage 2: Searches file-level chunks within those repos.

    Use this when you need to find specific implementations, not just
    which repo to look in.

    Args:
        query: Natural language query (e.g., 'handoff to zendesk',
               'VPC CIDR configuration', 'database migration')
        installations: Optional list of repo names to search within.
                       If omitted, auto-discovers via Stage 1.
        team: Optional team filter for Stage 1 discovery.
        top_k: Number of file-level results to return (default 10)
    """
    logger.info(f"spark_deep: query={query!r} installations={installations} team={team} top_k={top_k}")
    table = db.open_table(TABLE_NAME)
    query_vector = embed_single(query, config)

    # Stage 1: Discover relevant installations
    if not installations:
        where = 'chunk_type = "summary"'
        if team:
            where += f' AND team = "{team}"'
        summary_rows = _search(table, query, query_vector, where, config.spark_deep_stage1_k)

        if not summary_rows:
            return "No matching installations found."

        installations = [row["installation"] for row in summary_rows]
        stage1_info = "**Stage 1 — Matching installations:**\n"
        for row in summary_rows:
            score = 1 - row.get("_distance", 0)
            stage1_info += f"  - {row['installation']} ({row['team']}) score={score:.3f}\n"
        stage1_info += "\n"
    else:
        stage1_info = f"**Searching within:** {', '.join(installations)}\n\n"

    # Stage 2: Search file + MR chunks within those installations
    # Match on both installation name and installation_path so queries like
    # "flashback fleet" can match repos nested under flashback-fleet/
    install_clauses = []
    for name in installations:
        install_clauses.append(f'installation = "{name}"')
    install_filter = " OR ".join(install_clauses)
    file_where = f'(chunk_type = "file" OR chunk_type = "symbol" OR chunk_type = "merge_request") AND ({install_filter})'

    fetch_k = top_k * config.reranker_top_k_multiplier if config.reranker_enabled else top_k
    file_rows = _search(table, query, query_vector, file_where, fetch_k)
    file_rows = _maybe_rerank(query, file_rows)[:top_k]

    stage2_info = _format_results(file_rows)

    logger.info(f"spark_deep: searched {installations}, returned {len(file_rows)} file/MR results")
    return stage1_info + stage2_info


@mcp.tool()
def list_installations(
    team: str | None = None,
    exclude_archived: bool = False,
) -> str:
    """List all indexed installations, optionally filtered by team.

    Args:
        team: Optional team filter (e.g., 'Platform - Infrastructure')
        exclude_archived: If True, hide archived/inactive repos
    """
    logger.info(f"list_installations: team={team} exclude_archived={exclude_archived}")
    table = db.open_table(TABLE_NAME)

    where = 'chunk_type = "summary"'
    if team:
        where += f' AND team = "{team}"'
    if exclude_archived:
        where += " AND archived = false"

    rows = (
        table.search()
        .where(where)
        .select(["installation", "installation_path", "team", "description", "archived"])
        .limit(500)
        .to_list()
    )

    if not rows:
        return "No installations found."

    by_team: dict[str, list[str]] = {}
    for row in rows:
        t = row["team"]
        label = f"  - {row['installation']} ({row['installation_path']})"
        desc = row.get("description")
        if desc:
            label += f" — {desc[:80]}"
        if row.get("archived"):
            label += " [ARCHIVED]"
        by_team.setdefault(t, []).append(label)

    output = []
    for t in sorted(by_team):
        output.append(f"## {t}")
        output.extend(sorted(by_team[t]))
        output.append("")

    return "\n".join(output)


@mcp.tool()
def installation_summary(repo_name: str) -> str:
    """Get the full monitor-log (summary) for a specific installation.

    Args:
        repo_name: The installation name (e.g., 'svc-chatbot', 'tf-aws-eks')
    """
    logger.info(f"installation_summary: repo_name={repo_name!r}")
    table = db.open_table(TABLE_NAME)
    rows = (
        table.search()
        .where(f'installation = "{repo_name}" AND chunk_type = "summary"')
        .limit(1)
        .to_list()
    )

    if not rows:
        logger.info(f"installation_summary: '{repo_name}' not found")
        return f"Installation '{repo_name}' not found in the Index."

    row = rows[0]
    parts = [f"# {row['installation']}", f"Team: {row['team']}", f"Path: {row['installation_path']}"]
    if row.get("description"):
        parts.append(f"Description: {row['description']}")
    if row.get("topics"):
        parts.append(f"Topics: {row['topics']}")
    if row.get("languages"):
        parts.append(f"Languages: {row['languages']}")
    if row.get("last_activity"):
        parts.append(f"Last active: {row['last_activity']}")
    if row.get("archived"):
        parts.append("Status: ARCHIVED")
    if row.get("framework") or row.get("deploy_target"):
        det = []
        if row.get("framework"):
            det.append(f"Framework: {row['framework']}")
        if row.get("ci_type"):
            det.append(f"CI: {row['ci_type']}")
        if row.get("deploy_target"):
            det.append(f"Deploy: {row['deploy_target']}")
        parts.append(" | ".join(det))
    if row.get("test_command") or row.get("lint_command"):
        cmd = []
        if row.get("test_command"):
            cmd.append(f"Test: {row['test_command']}")
        if row.get("lint_command"):
            cmd.append(f"Lint: {row['lint_command']}")
        parts.append(" | ".join(cmd))
    if row.get("web_url"):
        parts.append(f"URL: {row['web_url']}")
    parts.append("")
    parts.append(row["content"])
    return "\n".join(parts)


@mcp.tool()
def recent_changes(
    repo_name: str,
    limit: int = 10,
) -> str:
    """Show recent merge requests for an installation.

    Returns indexed MR titles, descriptions, and authors. Useful for
    understanding what's actively changing in a repo.

    Args:
        repo_name: The installation name (e.g., 'svc-chatbot', 'tf-aws-eks')
        limit: Max results (default 10)
    """
    logger.info(f"recent_changes: repo_name={repo_name!r} limit={limit}")
    table = db.open_table(TABLE_NAME)
    rows = (
        table.search()
        .where(f'installation = "{repo_name}" AND chunk_type = "merge_request"')
        .limit(limit)
        .to_list()
    )

    if not rows:
        logger.info(f"recent_changes: no MR data for '{repo_name}'")
        return f"No merge request data for '{repo_name}'. Re-index with GitLab enabled."

    return _format_results(rows)


@mcp.tool()
def search_decisions(
    query: str,
    team: str | None = None,
    top_k: int = 10,
) -> str:
    """Search synthesized decision records across all repos.

    Optimized for 'why' questions: 'why did we switch to X?',
    'what was the decision around Y?', 'what alternatives were considered for Z?'

    Args:
        query: Natural language query about a decision or rationale
        team: Optional team filter (e.g., 'Platform - Infrastructure')
        top_k: Number of results to return (default 10)
    """
    logger.info(f"search_decisions: query={query!r} team={team} top_k={top_k}")
    table = db.open_table(TABLE_NAME)
    query_vector = embed_single(query, config)

    where = 'chunk_type = "decision"'
    if team:
        where += f' AND team = "{team}"'

    fetch_k = top_k * config.reranker_top_k_multiplier if config.reranker_enabled else top_k
    rows = _search(table, query, query_vector, where, fetch_k)
    rows = _maybe_rerank(query, rows)[:top_k]

    if not rows:
        logger.info("search_decisions: no results")
        return "No decision records found."

    output = []
    for i, row in enumerate(rows):
        if "_relevance_score" in row:
            score = row["_relevance_score"]
        else:
            score = 1 - row.get("_distance", 0)
        header = f"### [{i + 1}] {row['installation']} (score: {score:.3f})"
        meta = f"Team: {row['team']}"
        if row.get("decision_date"):
            meta += f" | Date: {row['decision_date']}"
        if row.get("decision_author"):
            meta += f" | Author: @{row['decision_author']}"
        if row.get("mr_url"):
            meta += f" | MR: {row['mr_url']}"
        content = row["content"][:3000]
        if len(row["content"]) > 3000:
            content += "\n... [truncated]"
        output.append(f"{header}\n{meta}\n\n{content}")

    logger.info(f"search_decisions: returned {len(rows)} results")
    return "\n\n---\n\n".join(output)


# --- Webhook endpoints ---

# Lazy-initialized dispatcher (created when first webhook arrives)
_dispatcher = None


def _get_dispatcher():
    global _dispatcher
    if _dispatcher is None:
        from spark.webhook import create_default_dispatcher
        _dispatcher = create_default_dispatcher(config)
    return _dispatcher


@mcp.custom_route("/webhook/gitlab", methods=["POST"])
async def gitlab_webhook(request):
    """Receive GitLab merge request webhook events.

    Dispatches to registered handlers based on MR action:
      - merge → reindex the repo
      - open  → (add your own handlers via dispatcher.on())
    """
    from starlette.responses import JSONResponse

    from spark.webhook import MergeRequestEvent

    event_type = request.headers.get("X-Gitlab-Event", "")
    logger.info(f"[webhook] Incoming: {event_type} from {request.client.host}")

    # Verify webhook secret
    if config.webhook_secret:
        token = request.headers.get("X-Gitlab-Token", "")
        if token != config.webhook_secret:
            logger.warning("[webhook] Rejected — invalid secret token")
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        logger.warning("[webhook] Rejected — invalid JSON body")
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # Only process merge request events
    if event_type != "Merge Request Hook":
        logger.info(f"[webhook] Ignored event type: {event_type}")
        return JSONResponse({"status": "ignored", "reason": f"event type: {event_type}"})

    event = MergeRequestEvent.from_payload(payload)

    if not event.repo_name:
        logger.warning("[webhook] Rejected — missing project name in payload")
        return JSONResponse({"error": "missing project name"}, status_code=400)

    labels_str = f" labels={event.labels}" if event.labels else ""
    logger.info(
        f"[webhook] MR {event.action}: {event.gitlab_path} !{event.mr_iid} "
        f"'{event.mr_title}' by @{event.author}{labels_str}"
    )

    # Dispatch to all registered handlers for this action
    dispatcher = _get_dispatcher()
    triggered = dispatcher.dispatch(event)
    logger.info(f"[webhook] Dispatched: action={event.action} handlers={triggered}")

    if not triggered:
        return JSONResponse({
            "status": "ignored",
            "reason": f"no handlers for action: {event.action}",
        })

    return JSONResponse({
        "status": "dispatched",
        "action": event.action,
        "installation": event.repo_name,
        "handlers": triggered,
    })


@mcp.custom_route("/webhook/status", methods=["GET"])
async def webhook_status(request):
    """Check webhook dispatcher status and registered handlers."""
    from starlette.responses import JSONResponse

    dispatcher = _get_dispatcher()
    return JSONResponse({
        "handlers": dispatcher.registered_actions,
    })
