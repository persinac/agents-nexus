"""CLI for 127 Guilty Spark.

'Reclaimer! We must act quickly!'
"""
from __future__ import annotations

import logging
from pathlib import Path

import click

from spark.config import SparkConfig


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.option("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING)", show_default=True)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, log_level: str) -> None:
    """127 Guilty Spark — Installation Index Manager."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ctx.ensure_object(dict)
    ctx.obj["config"] = SparkConfig.load(config_path)


@cli.command()
@click.option("--path-filter", "-p", default=None, help="Only index repos under this path prefix (e.g., 'search', 'platform/infra')")
@click.pass_context
def reclaim(ctx: click.Context, path_filter: str | None) -> None:
    """Full index rebuild. Discovers all installations and builds the Index.

    Use --path-filter to index only a subset:
      spark reclaim -p search
      spark reclaim -p platform/infra
    """
    from spark.indexer.builder import reclaim as do_reclaim

    config = ctx.obj["config"]
    do_reclaim(config, verbose=True, path_filter=path_filter)


@cli.command()
@click.argument("repo_name")
@click.pass_context
def activate(ctx: click.Context, repo_name: str) -> None:
    """Re-index a single installation by name."""
    from spark.indexer.builder import activate_installation, find_installation

    config = ctx.obj["config"]

    result = find_installation(config, repo_name)
    if result is None:
        click.echo(f"Installation '{repo_name}' not found.")
        raise SystemExit(1)

    repo_dir, rel_path = result
    activate_installation(config, repo_dir, rel_path, verbose=True)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Classify only — skip activation, prune, and metadata write")
@click.option("--path-filter", "-p", default=None, help="Only operate on installations under this path prefix (e.g., 'shared/libs')")
@click.pass_context
def sync(ctx: click.Context, dry_run: bool, path_filter: str | None) -> None:
    """Incremental re-index driven by per-installation git deltas.

    Walks every installation under installations_path, fetches each one's
    origin and compares the resulting HEAD timestamp against the last index
    timestamp stored in <index_path>/installations.json. Only repos whose
    HEAD has moved are re-embedded. New repos are full-indexed; removed
    repos have their chunks pruned from LanceDB.

    First run treats every repo as new (equivalent to a reclaim) since
    there's no prior metadata. Use `spark reclaim` for schema migrations
    or disaster recovery.
    """
    from spark.indexer.builder import sync_installations

    config = ctx.obj["config"]
    counts = sync_installations(config, dry_run=dry_run, path_filter=path_filter)
    summary = " ".join(f"{k}={v}" for k, v in counts.items())
    click.echo(summary)


@cli.command()
@click.argument("query")
@click.option("--top-k", "-k", default=10, help="Number of results")
@click.option("--team", "-t", default=None, help="Filter by team name")
@click.option("--deep", is_flag=True, help="Two-stage: match repos via summaries, then search files within them")
@click.option("--flat", "flat", is_flag=True, help="Search file+symbol content across ALL repos (best for 'where is X')")
@click.option("--include-archived", is_flag=True, help="Include archived/deprecated repos (excluded by default)")
@click.pass_context
def query(
    ctx: click.Context,
    query: str,
    top_k: int,
    team: str | None,
    deep: bool,
    flat: bool,
    include_archived: bool,
) -> None:
    """Query the installation index.

    Default searches repo summaries ("which repo does X"). Use --flat to search
    file/symbol content across all repos ("where is X"), or --deep for a
    two-stage repo->files search. All modes use hybrid (vector + keyword) search
    with cross-encoder reranking and exclude archived repos by default.
    """
    import lancedb

    from spark import search as sx
    from spark.indexer.builder import TABLE_NAME
    from spark.indexer.embedder import embed_single

    config = ctx.obj["config"]
    db = lancedb.connect(str(config.index_path))
    table = db.open_table(TABLE_NAME)
    query_vector = embed_single(query, config)

    file_types = '(chunk_type = "file" OR chunk_type = "symbol" OR chunk_type = "merge_request")'

    if flat:
        # Flat: file/symbol/MR content across all repos — best for "where is X".
        where = f'{file_types} AND team = "{team}"' if team else file_types
        where = sx.with_archived_filter(where, include_archived)
        rows = sx.hybrid_search(table, query, query_vector, where, sx.fetch_k(top_k, config), config)
        rows = sx.maybe_rerank(query, rows, config)[:top_k]
    elif deep:
        # Stage 1: find relevant installations via summaries (hybrid + reranked)
        where = 'chunk_type = "summary"'
        if team:
            where += f' AND team = "{team}"'
        where = sx.with_archived_filter(where, include_archived)
        k1 = config.spark_deep_stage1_k
        summary_rows = sx.hybrid_search(table, query, query_vector, where, sx.fetch_k(k1, config), config)
        summary_rows = sx.maybe_rerank(query, summary_rows, config)[:k1]
        if not summary_rows:
            click.echo("No matching installations found.")
            return
        click.echo("\n--- Stage 1: Matching installations ---")
        installations = []
        for i, row in enumerate(summary_rows):
            score = row.get("_relevance_score", 1 - row.get("_distance", 0))
            click.echo(f"  [{i + 1}] {row['installation']} ({row['team']}) score={score:.3f}")
            installations.append(row["installation"])
        # Stage 2: file + MR chunks within those repos (hybrid + reranked)
        install_filter = " OR ".join(f'installation = "{name}"' for name in installations)
        file_where = f'{file_types} AND ({install_filter})'
        rows = sx.hybrid_search(table, query, query_vector, file_where, sx.fetch_k(top_k, config), config)
        rows = sx.maybe_rerank(query, rows, config)[:top_k]
        click.echo("\n--- Stage 2: Matching files/MRs ---")
    else:
        # Single-stage: repo summaries — "which repo does X" (hybrid + reranked)
        where = 'chunk_type = "summary"'
        if team:
            where += f' AND team = "{team}"'
        where = sx.with_archived_filter(where, include_archived)
        rows = sx.hybrid_search(table, query, query_vector, where, sx.fetch_k(top_k, config), config)
        rows = sx.maybe_rerank(query, rows, config)[:top_k]

    if not rows:
        click.echo("No results found.")
        return

    for i, row in enumerate(rows):
        score = row.get("_relevance_score", 1 - row.get("_distance", 0))
        click.echo(f"\n[{i + 1}] {row['installation']} (score: {score:.3f})")
        click.echo(f"    Team: {row['team']} | Type: {row['chunk_type']}")
        click.echo(f"    Path: {row['installation_path']}")
        if row.get("file_path"):
            click.echo(f"    File: {row['file_path']}")
        if row.get("symbol_name"):
            click.echo(f"    Symbol: {row['symbol_type']} {row['symbol_name']}")
        # GitLab metadata
        if row.get("description"):
            click.echo(f"    Desc: {row['description'][:100]}")
        if row.get("topics"):
            click.echo(f"    Topics: {row['topics']}")
        if row.get("languages"):
            click.echo(f"    Languages: {row['languages']}")
        gl_extras = []
        if row.get("last_activity"):
            gl_extras.append(f"Last active: {row['last_activity']}")
        if row.get("archived"):
            gl_extras.append("ARCHIVED")
        if gl_extras:
            click.echo(f"    {' | '.join(gl_extras)}")
        # Detected metadata
        det_parts = []
        if row.get("framework"):
            det_parts.append(f"Framework: {row['framework']}")
        if row.get("deploy_target"):
            det_parts.append(f"Deploy: {row['deploy_target']}")
        if det_parts:
            click.echo(f"    {' | '.join(det_parts)}")
        if row.get("web_url"):
            click.echo(f"    GitLab: {row['web_url']}")
        preview = row["content"][:200].replace("\n", " ")
        click.echo(f"    {preview}...")


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show index status and stats."""
    import os
    import time
    import lancedb

    from spark.indexer.builder import TABLE_NAME

    config = ctx.obj["config"]
    index_path = config.index_path

    if not index_path.exists():
        click.echo("[Guilty Spark] The Index has not been constructed yet.")
        click.echo("Run 'spark reclaim' to build it.")
        return

    db = lancedb.connect(str(index_path))
    try:
        table = db.open_table(TABLE_NAME)
    except Exception:
        click.echo("[Guilty Spark] The Index exists but the table is missing.")
        click.echo("Run 'spark reclaim' to rebuild.")
        return

    total = table.count_rows()
    summaries = table.count_rows('chunk_type = "summary"')
    files = table.count_rows('chunk_type = "file"')
    symbols = table.count_rows('chunk_type = "symbol"')
    mrs = table.count_rows('chunk_type = "merge_request"')
    decisions = table.count_rows('chunk_type = "decision"')

    # Index age — newest mtime of any file inside the LanceDB directory
    newest_mtime = 0.0
    for root, _dirs, fnames in os.walk(index_path):
        for fname in fnames:
            try:
                mtime = os.path.getmtime(os.path.join(root, fname))
                if mtime > newest_mtime:
                    newest_mtime = mtime
            except OSError:
                pass
    if newest_mtime:
        age_secs = time.time() - newest_mtime
        if age_secs < 3600:
            age_str = f"{int(age_secs / 60)}m ago"
        elif age_secs < 86400:
            age_str = f"{age_secs / 3600:.1f}h ago"
        else:
            age_str = f"{age_secs / 86400:.1f}d ago"
        import datetime
        built_str = datetime.datetime.fromtimestamp(newest_mtime).strftime("%Y-%m-%d %H:%M:%S")
        last_built = f"{built_str} ({age_str})"
    else:
        last_built = "unknown"

    # Per-team repo counts (from summary chunks only)
    team_counts: dict[str, int] | None = None
    try:
        from collections import Counter
        rows = table.search().where('chunk_type = "summary"').select(["team"]).to_arrow()
        team_counts = dict(Counter(rows["team"].to_pylist()).most_common())
    except Exception:
        pass

    # Recent pipeline errors
    log_path = Path(__file__).parent.parent.parent / "logs" / "pipeline.log"
    recent_errors: list[str] = []
    if log_path.exists():
        try:
            lines = log_path.read_text(errors="replace").splitlines()
            recent_errors = [l for l in lines[-200:] if " ERROR " in l or " WARNING " in l][-5:]
        except OSError:
            pass

    click.echo("[127 Guilty Spark] Index Status")
    click.echo(f"  Location:      {index_path}")
    click.echo(f"  Last built:    {last_built}")
    from spark.indexer.embedder import describe_embedder
    click.echo(f"  Embedder:      {describe_embedder(config)}")
    click.echo()
    click.echo(f"  Installations: {summaries}")
    code_str = f"{symbols} symbols" if symbols else f"{files} files"
    dec_str = f", {decisions} decisions" if decisions else ""
    mr_str = f", {mrs} MRs" if mrs else ""
    click.echo(f"  Chunks:        {total:,} total ({summaries} summaries, {code_str}{mr_str}{dec_str})")

    if team_counts:
        click.echo()
        click.echo("  By team:")
        for team, count in team_counts.items():
            click.echo(f"    {team:<35} {count} repos")

    click.echo()
    if recent_errors:
        click.echo("  Recent pipeline issues:")
        for line in recent_errors:
            click.echo(f"    {line}")
    else:
        click.echo("  Recent pipeline errors: none")


@cli.command("generate-registry")
@click.option("--output", "-o", default=None, help="Output file path (default: {installations_path}/projects.yaml)")
@click.option("--path-filter", "-p", default=None, help="Only include repos under this path prefix")
@click.option("--model", default="claude-sonnet-4-6", help="Default LLM model for minions")
@click.option("--include-archived", is_flag=True, help="Include archived repos")
@click.option("--from-index", is_flag=True, help="Project from installations.json (no walk/detect); fast, always matches the index")
@click.option("--dry-run", is_flag=True, help="Print to stdout instead of writing file")
@click.pass_context
def generate_registry(
    ctx: click.Context,
    output: str | None,
    path_filter: str | None,
    model: str,
    include_archived: bool,
    from_index: bool,
    dry_run: bool,
) -> None:
    """Generate a minions-suite projects.yaml from all installations.

    Default (walk) mode scans every installation, detects
    languages/frameworks/CI/deploy targets, writes the registry, AND persists
    each detected profile back into installations.json so the index and the
    manifest stay in lockstep.

    --from-index skips the walk and projects the registry straight from the
    `detected` blobs already stored in installations.json — fast, no drift,
    and exactly what the index knows.

    Default output: {installations_path}/projects.yaml
    """
    from spark.detector import DetectedProject, detect_project
    from spark.gitlab import parse_gitlab_path
    from spark.indexer.builder import _discover_installations
    from spark.indexer.metadata import InstallationMeta, load_metadata, save_metadata
    from spark.registry import build_registry, serialize_registry

    config = ctx.obj["config"]
    if output is None:
        output = str(config.installations_path / "projects.yaml")

    detected_projects: list[DetectedProject] = []

    if from_index:
        # Projection: read the detector profiles persisted at index time.
        meta = load_metadata(config.metadata_path)
        missing = 0
        for rel_path, entry in sorted(meta.items()):
            if path_filter and not rel_path.startswith(path_filter):
                continue
            if not entry.detected:
                missing += 1
                continue
            detected_projects.append(DetectedProject.from_dict(entry.detected))
        click.echo(
            f"[Guilty Spark] Projecting registry from {config.metadata_path} "
            f"({len(detected_projects)} with detector data)"
        )
        if missing:
            click.echo(
                f"  ⚠ {missing} installs have no detector data yet — run "
                f"`spark generate-registry` (walk mode) or `spark reclaim` to backfill."
            )
    else:
        all_installations = _discover_installations(config)
        if path_filter:
            installations = [(p, r) for p, r in all_installations if r.startswith(path_filter)]
        else:
            installations = all_installations

        click.echo(f"[Guilty Spark] Scanning {len(installations)} installations...")

        # Initialize GitLab client if configured
        gitlab_client = None
        if config.gitlab_enabled:
            from spark.gitlab import GitLabClient
            gitlab_client = GitLabClient(config.gitlab_url, config.gitlab_token)
            click.echo(f"[Guilty Spark] GitLab enrichment: enabled ({config.gitlab_url})")

        detected_by_rel: dict[str, DetectedProject] = {}
        for i, (repo_dir, rel_path) in enumerate(installations):
            gitlab_project = None
            if gitlab_client:
                gl_path = parse_gitlab_path(repo_dir)
                if gl_path:
                    gitlab_project = gitlab_client.get_project(gl_path)

            detected = detect_project(repo_dir, gitlab_project, config.gitlab_url)
            detected_projects.append(detected)
            detected_by_rel[rel_path] = detected

            lang = detected.primary_language or "?"
            fw = detected.framework or "-"
            deploy = detected.deploy_target or "-"
            click.echo(f"  [{i + 1}/{len(installations)}] {detected.name}: {lang}/{fw} -> {deploy}")

        if gitlab_client:
            gitlab_client.close()

        # Persist detector profiles back into installations.json so --from-index,
        # the MCP query_registry tool, and the dashboard all see fresh data.
        # Update only the `detected` field of existing entries (preserve index
        # timestamps); create a stub entry for repos not yet embedded.
        if not dry_run:
            from dataclasses import asdict
            meta = load_metadata(config.metadata_path)
            for rel_path, detected in detected_by_rel.items():
                if rel_path in meta:
                    meta[rel_path].detected = asdict(detected)
                else:
                    meta[rel_path] = InstallationMeta(
                        indexed_at="", last_remote_ts=0,
                        clone_url=detected.clone_url, detected=asdict(detected),
                    )
            try:
                save_metadata(config.metadata_path, meta)
                click.echo(f"  Persisted detector profiles -> {config.metadata_path}")
            except OSError as e:
                click.echo(f"  ⚠ could not persist detector profiles: {e}")

    registry = build_registry(
        detected_projects,
        gitlab_url=config.gitlab_url,
        model=model,
        exclude_archived=not include_archived,
    )

    yaml_output = serialize_registry(registry)
    project_count = len(registry["projects"])
    archived_count = sum(1 for p in detected_projects if p.archived)

    if dry_run:
        click.echo(f"\n--- projects.yaml ({project_count} projects) ---\n")
        click.echo(yaml_output)
    else:
        Path(output).write_text(yaml_output)
        click.echo(f"\n[Guilty Spark] Registry generated!")
        click.echo(f"  Output: {output}")
        click.echo(f"  Projects: {project_count}")
        if archived_count:
            skip_msg = "excluded" if not include_archived else "included"
            click.echo(f"  Archived: {archived_count} ({skip_msg})")


@cli.command("registry")
@click.option("--language", "-l", default=None, help="Primary language or any detected language (e.g. python)")
@click.option("--framework", "-f", default=None, help="Framework substring (e.g. fastify)")
@click.option("--ci", default=None, help="Exact CI type (e.g. gitlab_ci)")
@click.option("--deploy", default=None, help="Deploy target substring")
@click.option("--role", default=None, help="Review role (e.g. backend, devops)")
@click.option("--path-filter", "-p", default=None, help="Only repos under this path prefix")
@click.option("--as-json", is_flag=True, help="Emit JSON instead of a table")
@click.pass_context
def registry(
    ctx: click.Context,
    language: str | None,
    framework: str | None,
    ci: str | None,
    deploy: str | None,
    role: str | None,
    path_filter: str | None,
    as_json: bool,
) -> None:
    """Search the repo manifest (detector profiles in installations.json).

    Exact/structured lookup that complements semantic `query`. Examples:
      spark registry --framework fastify
      spark registry --language python --role backend
      spark registry --deploy crossplane -p search
    """
    import json as _json
    from spark.indexer.metadata import load_metadata

    config = ctx.obj["config"]
    meta = load_metadata(config.metadata_path)
    matches: list[tuple[str, dict]] = []
    for rel, entry in sorted(meta.items()):
        d = entry.detected
        if not d:
            continue
        if path_filter and not rel.startswith(path_filter):
            continue
        langs = [x.lower() for x in (d.get("languages") or [])]
        if language and language.lower() != (d.get("primary_language") or "").lower() and language.lower() not in langs:
            continue
        if framework and framework.lower() not in (d.get("framework") or "").lower():
            continue
        if ci and ci.lower() != (d.get("ci_type") or "").lower():
            continue
        if deploy and deploy.lower() not in (d.get("deploy_target") or "").lower():
            continue
        if role and role.lower() not in [r.lower() for r in (d.get("roles") or [])]:
            continue
        matches.append((rel, d))

    if as_json:
        click.echo(_json.dumps([{"path": rel, **d} for rel, d in matches], indent=2))
        return

    if not matches:
        click.echo(
            "No matching installations. If empty across the board, run "
            "`spark generate-registry` to backfill detector data into installations.json."
        )
        return

    click.echo(f"[Guilty Spark] {len(matches)} match(es):\n")
    for rel, d in matches:
        lang = d.get("primary_language") or "?"
        fw = d.get("framework") or "-"
        dep = d.get("deploy_target") or "-"
        ci_t = d.get("ci_type") or "-"
        roles = ",".join(d.get("roles") or []) or "-"
        click.echo(f"  {rel}")
        click.echo(f"      {lang} | fw={fw} | ci={ci_t} | deploy={dep} | roles={roles}")


@cli.command()
@click.option("--all", "all_repos", is_flag=True, default=False, help="Synthesize decisions for all indexed repos")
@click.option("--repo", "repo_name", default=None, help="Synthesize decisions for a single repo by name")
@click.option("--team", "team_name", default=None, help="Synthesize decisions for all repos belonging to a team")
@click.option("--days", default=90, show_default=True, help="Only process MRs merged within this many days")
@click.pass_context
def synthesize(
    ctx: click.Context,
    all_repos: bool,
    repo_name: str | None,
    team_name: str | None,
    days: int,
) -> None:
    """Retroactively synthesize decision records from historical MR data.

    Requires GitLab to be configured and decisions_enabled: true in config.

    Examples:
      spark synthesize --all --days 180
      spark synthesize --repo svc-chatbot
      spark synthesize --team "Platform - Infrastructure" --days 90
    """
    import time
    from datetime import datetime, timedelta, timezone

    import lancedb

    from spark.gitlab import GitLabClient, parse_gitlab_path
    from spark.indexer.builder import (
        TABLE_NAME,
        _discover_installations,
        ensure_decision_columns,
        ensure_services_columns,
    )
    from spark.indexer.chunker import Chunk
    from spark.indexer.embedder import embed_single
    from spark.synthesizer import synthesize_decision

    config = ctx.obj["config"]

    if not (all_repos or repo_name or team_name):
        click.echo("Error: provide at least one scope flag: --all, --repo <name>, or --team <name>")
        raise SystemExit(1)

    if not config.decisions_enabled:
        click.echo("Decision synthesis is disabled. Set decisions_enabled: true in config.yaml.")
        raise SystemExit(0)

    if not config.gitlab_enabled:
        click.echo("Error: GitLab is not configured. Set GITLAB_URL and GITLAB_TOKEN.")
        raise SystemExit(1)

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    all_installations = _discover_installations(config)

    # Filter to requested scope
    if repo_name:
        installations = [(p, r) for p, r in all_installations if p.name == repo_name]
        if not installations:
            click.echo(f"Repo '{repo_name}' not found in installations.")
            raise SystemExit(1)
    elif team_name:
        installations = [(p, r) for p, r in all_installations if config.resolve_team(r) == team_name]
        if not installations:
            click.echo(f"No installations found for team '{team_name}'.")
            raise SystemExit(1)
    else:
        installations = all_installations

    click.echo(f"[Guilty Spark] Synthesizing decisions for {len(installations)} installation(s), last {days} days...")

    client = GitLabClient(config.gitlab_url, config.gitlab_token)
    db = lancedb.connect(str(config.index_path))
    table = db.open_table(TABLE_NAME)
    ensure_decision_columns(table)
    ensure_services_columns(table)

    total_success, total_skip, total_fail = 0, 0, 0

    for repo_dir, rel_path in installations:
        gl_path = parse_gitlab_path(repo_dir)
        if not gl_path:
            click.echo(f"  {rel_path}: no git remote, skipping")
            total_skip += 1
            continue

        mrs = client.get_recent_merge_requests(gl_path, limit=50)
        # Filter by date window
        filtered_mrs = []
        for mr in mrs:
            merged_at = mr.get("merged_at", "")
            if not merged_at:
                continue
            try:
                # GitLab returns ISO 8601: "2024-01-15T10:30:00.000Z"
                mr_date = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                if mr_date >= cutoff:
                    filtered_mrs.append(mr)
            except ValueError:
                filtered_mrs.append(mr)

        if not filtered_mrs:
            click.echo(f"  {repo_dir.name}: no MRs in window")
            total_skip += 1
            continue

        click.echo(f"  {repo_dir.name}: {len(filtered_mrs)} MR(s)...")
        repo_success, repo_fail = 0, 0
        team = config.resolve_team(rel_path)

        for mr in filtered_mrs:
            # get_recent_merge_requests doesn't return mr_iid — need to fetch full MR
            # The title is available; use search to find iid via full MR fetch
            # Actually get_recent_merge_requests returns iid if we add it — but currently it doesn't.
            # We need the iid to call get_mr_full. Let's fetch it from the MR list directly.
            # The GitLab MR list endpoint returns iid in the response.
            # Our current get_recent_merge_requests doesn't include it — we need to extend it.
            # For now, skip mr_iid-dependent calls and use the data we have.
            # (See note below — we'll use the data from get_recent_merge_requests directly.)
            mr_iid = mr.get("iid")
            if not mr_iid:
                repo_fail += 1
                continue

            notes = client.get_mr_notes(gl_path, mr_iid)
            full_mr = client.get_mr_full(gl_path, mr_iid)
            if not full_mr:
                repo_fail += 1
                continue

            content = synthesize_decision(
                mr_title=full_mr["title"],
                mr_description=full_mr["description"],
                mr_notes=notes,
                repo_name=repo_dir.name,
                team=team,
                merged_at=full_mr["merged_at"],
                author=full_mr["author"],
                config=config,
            )
            if not content:
                repo_fail += 1
                continue

            chunk_id = f"{repo_dir.name}::decision::{mr_iid}"
            chunk = Chunk(
                id=chunk_id,
                installation=repo_dir.name,
                installation_path=rel_path,
                team=team,
                chunk_type="decision",
                file_path="",
                content=f"# Decision: {full_mr['title']}\nRepo: {repo_dir.name} | Team: {team}\n\n{content}",
                decision_date=full_mr["merged_at"][:10] if full_mr["merged_at"] else "",
                decision_author=full_mr["author"],
                mr_url=full_mr["web_url"],
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
            try:
                table.delete(f'id = "{chunk_id}"')
                table.add([record])
                repo_success += 1
            except Exception as e:
                click.echo(f"    Warning: failed to index decision for !{mr_iid}: {e}")
                repo_fail += 1

        status_parts = [f"    +{repo_success} indexed"]
        if repo_fail:
            status_parts.append(f"{repo_fail} failed")
        click.echo(", ".join(status_parts))
        total_success += repo_success
        total_fail += repo_fail

    client.close()
    click.echo(f"\n[Guilty Spark] Done! Indexed: {total_success}, Skipped: {total_skip}, Failed: {total_fail}")


@cli.command()
@click.option("--transport", "-t", default="stdio", help="Transport: stdio, sse, streamable-http")
@click.option("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
@click.option("--port", default=8343, type=int, help="Port to bind (default: 8343)")
@click.pass_context
def serve(ctx: click.Context, transport: str, host: str, port: int) -> None:
    """Start the MCP server (for Claude Code integration)."""
    from spark.server.mcp_server import mcp

    if transport == "stdio":
        mcp.run(transport=transport)
    else:
        mcp.run(transport=transport, host=host, port=port)


@cli.command("webhook-test")
@click.argument("repo_name")
@click.option("--group", default="my-group", help="GitLab group/namespace for the simulated payload")
@click.option("--url", default="http://localhost:8343", help="Spark server URL")
@click.pass_context
def webhook_test(ctx: click.Context, repo_name: str, group: str, url: str) -> None:
    """Simulate a GitLab MR merge webhook for testing.

    Sends a fake webhook payload to the running Spark server to trigger
    a reindex of the specified repo.
    """
    import httpx

    config = ctx.obj["config"]
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": "merge",
            "target_branch": "main",
        },
        "project": {
            "name": repo_name,
            "path_with_namespace": f"{group}/{repo_name}",
        },
    }

    headers = {"X-Gitlab-Event": "Merge Request Hook"}
    if config.webhook_secret:
        headers["X-Gitlab-Token"] = config.webhook_secret

    try:
        resp = httpx.post(f"{url}/webhook/gitlab", json=payload, headers=headers, timeout=10)
        click.echo(f"Response ({resp.status_code}): {resp.json()}")
    except Exception as e:
        click.echo(f"Error: {e}")
        raise SystemExit(1)


@cli.command("webhook-register")
@click.argument("webhook_url")
@click.option("--path-filter", "-p", default=None, help="Only register for repos under this path prefix")
@click.option("--dry-run", is_flag=True, help="Show what would be registered without doing it")
@click.pass_context
def webhook_register(
    ctx: click.Context, webhook_url: str, path_filter: str | None, dry_run: bool
) -> None:
    """Register GitLab webhooks on all indexed projects.

    Registers a merge-request webhook on each project via the GitLab API.
    Skips projects that already have the same webhook URL.

    Example:
      spark webhook-register https://spark.example.com/webhook/gitlab
      spark webhook-register http://localhost:8343/webhook/gitlab -p search --dry-run
    """
    from spark.gitlab import GitLabClient, parse_gitlab_path
    from spark.indexer.builder import _discover_installations

    config = ctx.obj["config"]

    if not config.gitlab_enabled:
        click.echo("Error: GitLab is not configured. Set GITLAB_URL and GITLAB_TOKEN.")
        raise SystemExit(1)

    if not config.webhook_secret:
        click.echo("Warning: SPARK_WEBHOOK_SECRET is not set. Webhooks will have no secret token.")
        if not dry_run:
            click.confirm("Continue without a secret?", abort=True)

    all_installations = _discover_installations(config)
    if path_filter:
        installations = [(p, r) for p, r in all_installations if r.startswith(path_filter)]
    else:
        installations = all_installations

    click.echo(f"[Guilty Spark] Registering webhooks on {len(installations)} projects...")
    click.echo(f"  Webhook URL: {webhook_url}")

    if dry_run:
        click.echo(f"  (dry run — no webhooks will be created)\n")

    client = GitLabClient(config.gitlab_url, config.gitlab_token)
    created, skipped, failed = 0, 0, 0

    for i, (repo_dir, rel_path) in enumerate(installations):
        gl_path = parse_gitlab_path(repo_dir)
        if not gl_path:
            click.echo(f"  [{i + 1}/{len(installations)}] {rel_path}: no git remote, skipping")
            skipped += 1
            continue

        if dry_run:
            click.echo(f"  [{i + 1}/{len(installations)}] {gl_path}: would register")
            continue

        result = client.add_project_webhook(gl_path, webhook_url, config.webhook_secret)
        if not result or result["status"] == "failed":
            error = result.get("error", "unknown") if result else "unknown"
            click.echo(f"  [{i + 1}/{len(installations)}] {gl_path}: FAILED ({error})")
            failed += 1
        elif result["status"] == "already_exists":
            click.echo(f"  [{i + 1}/{len(installations)}] {gl_path}: already registered")
            skipped += 1
        else:
            click.echo(f"  [{i + 1}/{len(installations)}] {gl_path}: created (id={result['id']})")
            created += 1

    client.close()

    if not dry_run:
        click.echo(f"\n[Guilty Spark] Done! Created: {created}, Skipped: {skipped}, Failed: {failed}")


if __name__ == "__main__":
    cli()
