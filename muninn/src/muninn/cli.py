import datetime
import logging
import os
import sys
from pathlib import Path

import click

from muninn import config, conversion, daily, db, obsidian, ocr, rm_format, rm_sync, vision

log = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )


@click.group()
@click.option("--config-file", default="config.toml", show_default=True, help="Path to config.toml")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx, config_file, verbose):
    """Muninn — sync and digitize reMarkable 1 notebooks into Obsidian."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_file"] = Path(config_file)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Detect changes without writing files or updating DB")
@click.option(
    "--folder",
    "folders",
    multiple=True,
    metavar="PREFIX",
    help="Only process notebooks whose folder path starts with PREFIX (case-insensitive). Repeatable.",
)
@click.option(
    "--include-trash",
    is_flag=True,
    help="Include notebooks in the rM trash (excluded by default).",
)
@click.pass_context
def sync(ctx, dry_run, folders, include_trash):
    """Pull notebooks from rM1, process them, and write to Obsidian."""
    try:
        cfg = config.load(ctx.obj["config_file"])
    except config.ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    staging_dir = Path(cfg.get("sync", {}).get("staging_dir", "~/.muninn/staging")).expanduser()
    conn = db.connect()

    log.info("Checking SSH connectivity...")
    if not rm_sync.check_ssh(cfg):
        click.echo("Device unreachable — aborting sync without modifying state.", err=True)
        sys.exit(1)

    if dry_run:
        # For dry-run we still need to pull to know what changed, but we won't
        # update the DB or write any output files.
        click.echo("Dry-run mode: pulling to detect changes...")

    if not rm_sync.pull(cfg, staging_dir):
        click.echo("Pull failed — aborting sync without modifying state.", err=True)
        sys.exit(1)

    new_uuids, modified_uuids, unchanged_uuids = rm_sync.detect_changes(staging_dir, conn)
    pending = new_uuids + modified_uuids

    if not pending:
        click.echo("Nothing new — all notebooks up to date.")
        return

    folder_map = rm_sync.build_folder_map(staging_dir)
    folder_prefixes = tuple(p.lower().rstrip("/") for p in folders) if folders else ()

    def folder_for(uuid: str) -> str:
        return rm_sync.notebook_folder(staging_dir, uuid, folder_map)

    def keep(uuid: str) -> bool:
        path = folder_for(uuid).lower()
        if not include_trash and (path == "trash" or path.startswith("trash/")):
            return False
        if folder_prefixes and not any(
            path == p or path.startswith(f"{p}/") for p in folder_prefixes
        ):
            return False
        return True

    new_uuids = [u for u in new_uuids if keep(u)]
    modified_uuids = [u for u in modified_uuids if keep(u)]
    pending = new_uuids + modified_uuids

    if not pending:
        scope = (
            f" matching folder filter {list(folders)}" if folders else ""
        ) + ("" if include_trash else " (trash excluded)")
        click.echo(f"No pending notebooks{scope}.")
        return

    if dry_run:
        click.echo(f"\nWould process {len(pending)} notebook(s):\n")
        for uuid in new_uuids:
            title = rm_sync.notebook_title(staging_dir, uuid)
            path = folder_for(uuid) or "(root)"
            click.echo(f"  [new]      {path}/{title} ({uuid})")
        for uuid in modified_uuids:
            title = rm_sync.notebook_title(staging_dir, uuid)
            path = folder_for(uuid) or "(root)"
            click.echo(f"  [modified] {path}/{title} ({uuid})")
        click.echo(f"\n{len(unchanged_uuids)} unchanged notebook(s) skipped.")
        return

    click.echo(f"Found {len(pending)} notebook(s) to process ({len(unchanged_uuids)} unchanged).")

    success = 0
    errors = 0
    skipped = 0
    for uuid in pending:
        title = rm_sync.notebook_title(staging_dir, uuid)
        rm_folder = folder_for(uuid)
        file_hash = rm_sync._hash_notebook(staging_dir, uuid)
        log.info("[%s] Processing %r", uuid[:8], title)

        try:
            rm_paths = rm_sync.list_page_files(staging_dir, uuid)
            if not rm_paths:
                # No .rm pages = rM folder (organizational container) or an
                # empty notebook. Neither is an error — count separately so
                # the summary distinguishes "nothing to do" from real failures.
                log.info("[%s] %r — no .rm pages (folder or empty notebook), skipping", uuid[:8], title)
                skipped += 1
                continue

            # Persist notebook + page rows up front so OCR caching works even
            # if PDF/PNG conversion fails downstream (e.g. v6 .rm files, which
            # rmrl can't render).
            db.upsert_notebook(conn, uuid, title, rm_folder, file_hash)
            db.upsert_pages_rm(conn, uuid, rm_paths)

            # Phase 3: convert .rm → PDF → PNGs (best-effort; v6 notebooks may
            # fail here but OCR still proceeds)
            pdf_path = conversion.to_pdf(staging_dir, uuid)
            png_paths: list[Path] = []
            if pdf_path is not None:
                png_paths = conversion.to_pngs(pdf_path)
                if png_paths:
                    db.upsert_pages_png(conn, uuid, png_paths)

            # Phase 4: OCR each page (Claude vision by default, MyScript optional)
            ocr_results = _ocr_pages(cfg, conn, staging_dir, uuid, rm_paths)
            ocr_count = sum(1 for r in ocr_results if r is not None and r != "")

            # Phase 5: drawing descriptions (only when [vision].enabled)
            vision_results = _describe_pages(cfg, conn, staging_dir, uuid, rm_paths)
            vision_count = sum(1 for r in vision_results if r is not None and r != "")

            # Phase 7: render and write the Markdown into the matching vault.
            try:
                target = _write_to_vault(cfg, conn, uuid, title, rm_folder)
                vault_note = f", wrote {target.name}"
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] Vault write failed for %r: %s", uuid[:8], title, exc)
                vault_note = ", vault write FAILED"

            # Phase 8: daily-note merge (when notebook matches the configured source).
            daily_note = _maybe_merge_daily(cfg, conn, uuid, title)

            click.echo(
                f"  [{uuid[:8]}] {title!r} — {len(rm_paths)} page(s), "
                f"{len(png_paths)} rendered, {ocr_count} transcribed, "
                f"{vision_count} described{vault_note}{daily_note}"
            )
            success += 1

        except Exception as exc:
            log.error("[%s] Failed to process %r: %s", uuid[:8], title, exc)
            errors += 1

    click.echo(
        f"\nDone: {success} processed, {errors} failed, {skipped} skipped, "
        f"{len(unchanged_uuids)} unchanged."
    )


VALID_PROVIDERS = ("claude", "myscript")


def _ocr_provider(cfg: dict) -> str:
    p = cfg.get("ocr", {}).get("provider", "claude")
    if p not in VALID_PROVIDERS:
        raise ValueError(f"Unknown ocr.provider {p!r}; expected one of {VALID_PROVIDERS}")
    return p


def _ocr_pages(
    cfg: dict,
    conn,
    staging_dir: Path,
    uuid: str,
    rm_paths: list[Path],
) -> list[str | None]:
    """Run OCR for each .rm page, honoring config flag, provider, and the SQLite cache.

    Cache key is (rm_hash, ocr_provider). Switching provider invalidates the
    cache so the new provider gets a fresh transcription.
    """
    if not cfg.get("ocr", {}).get("enabled", False):
        return [None] * len(rm_paths)
    provider = _ocr_provider(cfg)

    results: list[str | None] = []
    for i, rm_path in enumerate(rm_paths):
        cached = db.get_cached_ocr(conn, uuid, i, provider)
        if cached is not None:
            log.debug("[%s] page %d: OCR cache hit (%s)", uuid[:8], i, provider)
            results.append(cached)
            continue
        text = _transcribe_one(cfg, provider, rm_path, uuid, i)
        db.update_ocr_text(conn, uuid, i, text, provider)
        results.append(text)
    return results


def _transcribe_one(
    cfg: dict, provider: str, rm_path: Path, uuid: str, page_index: int
) -> str | None:
    """Dispatch a single page to the configured OCR provider."""
    if provider == "claude":
        try:
            png_bytes = rm_format.render_to_png(rm_path)
        except (ValueError, Exception) as exc:  # noqa: BLE001 — render uses PIL too
            log.error("[%s] page %d: stroke render failed: %s", uuid[:8], page_index, exc)
            return None
        return vision.transcribe_handwriting(cfg, png_bytes)

    # myscript
    try:
        strokes = ocr.extract_strokes(rm_path)
    except ValueError as exc:
        log.error("[%s] page %d: stroke parse failed: %s", uuid[:8], page_index, exc)
        return None
    return ocr.transcribe_strokes(cfg, strokes)


def _write_to_vault(
    cfg: dict, conn, uuid: str, title: str, rm_folder: str
) -> Path:
    """Render the notebook to Markdown and write it into the matching vault.

    Returns the absolute path of the written file. Raises on failure.
    """
    vaults = cfg.get("vaults", [])
    vault = obsidian.pick_vault(rm_folder, vaults)
    pages = obsidian.fetch_pages(conn, uuid)
    last_synced_row = conn.execute(
        "SELECT last_synced FROM notebooks WHERE uuid = ?", (uuid,)
    ).fetchone()
    last_synced = last_synced_row["last_synced"] if last_synced_row else ""
    md = obsidian.build_markdown(
        title=title,
        rm_folder=rm_folder,
        notebook_uuid=uuid,
        last_synced=last_synced,
        pages=pages,
    )
    target = obsidian.vault_dir(vault) / f"{obsidian.sanitize_filename(title)}.md"
    obsidian.write_notebook_atomic(target, md)
    return target


def _maybe_merge_daily(cfg: dict, conn, uuid: str, title: str) -> str:
    """Run the daily-merge step for this notebook if it matches the config.

    Returns a short status string suitable for appending to the per-notebook
    progress line (or empty string if the step doesn't apply).
    """
    dm = cfg.get("daily_merge", {})
    if not dm.get("enabled", False):
        return ""
    if title != dm.get("source_notebook"):
        return ""
    try:
        return ", " + _run_daily_merge(cfg, conn, uuid, title)
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] Daily merge failed for %r: %s", uuid[:8], title, exc)
        return ", daily merge FAILED"


def _run_daily_merge(
    cfg: dict, conn, uuid: str, title: str, *, force: bool = False
) -> str:
    """Read OCR text from DB, find previous TODOs, extract via Claude, merge.

    Returns "daily: <status>" where status is created / merged / unchanged / skipped / FAILED.
    """
    pages = obsidian.fetch_pages(conn, uuid)
    ocr_text = "\n\n".join((p.get("ocr_text") or "").strip() for p in pages).strip()
    if not ocr_text:
        return "daily: skipped (no OCR text)"

    vaults = cfg.get("vaults", [])
    today = datetime.date.today()
    # The target dir is the parent of the per-date file; we need it to find the
    # most recent previous daily note for carry-forward context.
    target_dir = daily.daily_target_path(cfg, vaults, "x").parent
    # Use a heuristic date from the OCR text so we look back from the page's
    # date, not today's date — otherwise rerunning May 19's content on May 20
    # would treat the just-merged May 19 file as "previous" instead of May 18.
    guess = daily.heuristic_date(ocr_text, today) or today
    # Resolve the carry-forward source: today's existing block (same-day re-merge,
    # preserves manual status edits) → most recent prior daily note → nothing.
    previous_date, previous_todos = daily.resolve_carry_forward_source(target_dir, guess)

    sections = daily.extract_sections(
        cfg, ocr_text, previous_todos=previous_todos, previous_date=previous_date
    )
    if sections is None:
        return "daily: FAILED (extraction)"
    if sections.date_confidence == "low":
        log.warning(
            "[%s] daily merge: low-confidence date %s — proceeding anyway",
            uuid[:8],
            sections.date,
        )

    target = daily.daily_target_path(cfg, vaults, sections.date)
    import hashlib

    content_hash = hashlib.sha256(ocr_text.encode("utf-8")).hexdigest()[:16]
    status = daily.merge_into_file(
        target, sections, content_hash, source_label=title, force=force
    )
    return f"daily: {status} → {target.name}"


def _describe_pages(
    cfg: dict,
    conn,
    staging_dir: Path,
    uuid: str,
    rm_paths: list[Path],
) -> list[str | None]:
    """Generate drawing descriptions for each page, honoring [vision].enabled.

    Cache invalidates when rm_hash changes (see db.upsert_pages_rm). Empty
    notebooks short-circuit to empty strings without an API call.
    """
    if not cfg.get("vision", {}).get("enabled", False):
        return [None] * len(rm_paths)

    results: list[str | None] = []
    for i, rm_path in enumerate(rm_paths):
        cached = db.get_cached_vision(conn, uuid, i)
        if cached is not None:
            log.debug("[%s] page %d: vision cache hit", uuid[:8], i)
            results.append(cached)
            continue

        try:
            strokes = rm_format.parse_strokes(rm_path)
        except ValueError as exc:
            log.error("[%s] page %d: stroke parse failed: %s", uuid[:8], i, exc)
            db.update_vision_description(conn, uuid, i, None)
            results.append(None)
            continue

        if not strokes:
            # Empty page — skip the API call, store empty string to mark "checked, nothing to describe".
            db.update_vision_description(conn, uuid, i, "")
            results.append("")
            continue

        try:
            png_bytes = rm_format.render_to_png(rm_path)
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] page %d: stroke render failed: %s", uuid[:8], i, exc)
            db.update_vision_description(conn, uuid, i, None)
            results.append(None)
            continue

        description = vision.describe_page(cfg, png_bytes)
        db.update_vision_description(conn, uuid, i, description)
        results.append(description)
    return results


@cli.command(name="backfill-todos")
@click.option(
    "--from",
    "start_str",
    required=True,
    metavar="YYYY-MM-DD",
    help="First date to backfill (inclusive).",
)
@click.option(
    "--to",
    "end_str",
    required=True,
    metavar="YYYY-MM-DD",
    help="Last date to backfill (inclusive).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be written without modifying files.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Skip the marker hash short-circuit and rewrite blocks even when unchanged.",
)
@click.pass_context
def backfill_todos(ctx, start_str, end_str, dry_run, force):
    """Propagate the TODO table forward across a date range.

    For each date in [from, to] that has a daily note (YYYY-MM-DD.md) without
    a muninn-managed TODO block, find the most recent previous TODO state
    and carry it forward with ages bumped by the elapsed days. No LLM calls —
    pure carry-forward, no new items.
    """
    import hashlib

    try:
        cfg = config.load(ctx.obj["config_file"])
    except config.ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    try:
        start = datetime.date.fromisoformat(start_str)
        end = datetime.date.fromisoformat(end_str)
    except ValueError as exc:
        click.echo(f"Invalid date: {exc}", err=True)
        sys.exit(1)
    if end < start:
        click.echo("--to must be on or after --from", err=True)
        sys.exit(1)

    vaults = cfg.get("vaults", [])
    target_dir = daily.daily_target_path(cfg, vaults, "x").parent

    day = start
    while day <= end:
        target = target_dir / f"{day.isoformat()}.md"
        if not target.exists():
            click.echo(f"  [skip] {day} — no daily note file at {target}")
            day += datetime.timedelta(days=1)
            continue

        # Don't clobber a file that's already been merged from rM content
        # (rich block with new items + notes). Require --force to overwrite.
        if not force:
            existing = target.read_text(encoding="utf-8")
            if daily.START_MARKER_RE.search(existing):
                click.echo(
                    f"  [skip] {day} — muninn block already present (use --force to replace)"
                )
                day += datetime.timedelta(days=1)
                continue

        state = daily.find_previous_todo_state(target_dir, day)
        if state is None:
            click.echo(f"  [skip] {day} — no previous TODO state to carry forward")
            day += datetime.timedelta(days=1)
            continue

        _, prev_date, prev_todos = state
        days_elapsed = (day - prev_date).days
        carried = daily.carry_forward(prev_todos, days_elapsed)

        sections = daily.DailySections(
            date=day.isoformat(),
            date_confidence="high",
            todos=carried,
            questions=[],
            notes="",
        )
        rendered = "|".join(f"{t.text}|{t.status}|{t.age_days}" for t in carried)
        content_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]

        if dry_run:
            click.echo(
                f"  [dry-run] {day} ← carry from {prev_date} "
                f"(+{days_elapsed}d, {len(carried)} todos)"
            )
        else:
            status = daily.merge_into_file(
                target, sections, content_hash, source_label="backfill", force=force
            )
            click.echo(f"  [{status}] {day} ← carry from {prev_date} (+{days_elapsed}d)")

        day += datetime.timedelta(days=1)


@cli.command(name="merge-daily")
@click.option(
    "--notebook",
    "notebook_override",
    metavar="TITLE",
    help="Override [daily_merge].source_notebook for this run (e.g. test on a different notebook).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Run extraction and print where the merge would land, but don't write.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Skip the rm_hash short-circuit and rewrite the muninn block even when content hasn't changed (use after prompt or format changes).",
)
@click.pass_context
def merge_daily(ctx, notebook_override, dry_run, force):
    """Merge the daily-source notebook's OCR'd content into the dated daily note.

    Reads OCR text from the DB, asks Claude for structured sections + a date,
    then merges into <target_vault>/<target_dir>/<YYYY-MM-DD>.md. Idempotent
    via marker comments — re-running is safe.
    """
    try:
        cfg = config.load(ctx.obj["config_file"])
    except config.ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    if not cfg.get("daily_merge", {}).get("enabled", False):
        click.echo("daily_merge.enabled = false — nothing to do.", err=True)
        sys.exit(1)

    source_title = notebook_override or cfg["daily_merge"]["source_notebook"]
    conn = db.connect()

    notebooks = conn.execute(
        "SELECT uuid, title, rm_folder FROM notebooks WHERE title = ? ORDER BY rm_folder",
        (source_title,),
    ).fetchall()

    if not notebooks:
        click.echo(f"No notebooks found with title {source_title!r}.")
        return

    click.echo(f"Merging {len(notebooks)} notebook(s) titled {source_title!r}...\n")

    vaults = cfg.get("vaults", [])
    today = datetime.date.today()
    target_dir = daily.daily_target_path(cfg, vaults, "x").parent

    for n in notebooks:
        pages = obsidian.fetch_pages(conn, n["uuid"])
        ocr_text = "\n\n".join((p.get("ocr_text") or "").strip() for p in pages).strip()
        if not ocr_text:
            click.echo(f"  [skip] {n['rm_folder']}/{n['title']} — no OCR text")
            continue

        guess = daily.heuristic_date(ocr_text, today) or today
        previous_date, previous_todos = daily.resolve_carry_forward_source(
            target_dir, guess
        )

        sections = daily.extract_sections(
            cfg,
            ocr_text,
            previous_todos=previous_todos,
            previous_date=previous_date,
        )
        if sections is None:
            click.echo(f"  [FAIL] {n['rm_folder']}/{n['title']} — extraction failed")
            continue

        target = daily.daily_target_path(cfg, vaults, sections.date)
        if dry_run:
            prev_label = (
                f"prev={previous_date.isoformat()} ({len(previous_todos)} todos), "
                if previous_date
                else "no prev, "
            )
            click.echo(
                f"  [dry-run] {n['rm_folder']}/{n['title']} → {target}\n"
                f"            date={sections.date} ({sections.date_confidence}), "
                f"{prev_label}"
                f"todos={len(sections.todos)}, questions={len(sections.questions)}, "
                f"notes={len(sections.notes)} chars"
            )
            continue

        import hashlib

        content_hash = hashlib.sha256(ocr_text.encode("utf-8")).hexdigest()[:16]
        status = daily.merge_into_file(
            target,
            sections,
            content_hash,
            source_label=n["title"],
            force=force,
        )
        click.echo(f"  [{status}] {n['rm_folder']}/{n['title']} → {target}")


@cli.command(name="write-vault")
@click.option(
    "--folder",
    "folders",
    multiple=True,
    metavar="PREFIX",
    help="Only write notebooks whose folder path starts with PREFIX (case-insensitive). Repeatable.",
)
@click.option(
    "--vault",
    "vault_name",
    metavar="NAME",
    help="Only write notebooks routed to the named vault.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List notebooks that would be written without writing files.",
)
@click.pass_context
def write_vault(ctx, folders, vault_name, dry_run):
    """Render all notebooks in the DB as Markdown into their matching vaults.

    Useful for backfilling after enabling features (OCR provider switch,
    vision descriptions) or recreating a vault from scratch. Overwrites
    existing files atomically.
    """
    try:
        cfg = config.load(ctx.obj["config_file"])
    except config.ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    vaults = cfg.get("vaults", [])
    conn = db.connect()

    notebooks = conn.execute(
        "SELECT uuid, title, rm_folder FROM notebooks ORDER BY rm_folder, title"
    ).fetchall()

    if folders:
        prefixes = tuple(p.lower().rstrip("/") for p in folders)
        notebooks = [
            n
            for n in notebooks
            if any(
                n["rm_folder"].lower() == p or n["rm_folder"].lower().startswith(f"{p}/")
                for p in prefixes
            )
        ]

    if vault_name:
        notebooks = [
            n
            for n in notebooks
            if obsidian.pick_vault(n["rm_folder"], vaults).get("name") == vault_name
        ]

    if not notebooks:
        click.echo("No notebooks match the filter.")
        return

    click.echo(f"Writing {len(notebooks)} notebook(s)...\n")

    success = 0
    errors = 0
    for n in notebooks:
        target_vault = obsidian.pick_vault(n["rm_folder"], vaults)
        path = obsidian.vault_dir(target_vault) / f"{obsidian.sanitize_filename(n['title'])}.md"
        path_display = f"{target_vault.get('name', '?')}:{path}"

        if dry_run:
            click.echo(f"  [dry-run] {n['rm_folder'] or '(root)'}/{n['title']} → {path_display}")
            continue

        try:
            written = _write_to_vault(cfg, conn, n["uuid"], n["title"], n["rm_folder"])
            click.echo(f"  [ok] {n['rm_folder'] or '(root)'}/{n['title']} → {written}")
            success += 1
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to write %s: %s", n["title"], exc)
            click.echo(f"  [FAIL] {n['rm_folder'] or '(root)'}/{n['title']} ({exc})")
            errors += 1

    if not dry_run:
        click.echo(f"\nDone: {success} written, {errors} failed.")


@cli.command(name="reocr")
@click.option(
    "--folder",
    "folders",
    multiple=True,
    metavar="PREFIX",
    help="Only re-OCR pages whose folder path starts with PREFIX (case-insensitive). Repeatable.",
)
@click.option(
    "--all",
    "force_all",
    is_flag=True,
    help="Re-OCR every page, including those already produced by the current provider.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List pages that would be re-OCR'd without making API calls or DB writes.",
)
@click.pass_context
def reocr(ctx, folders, force_all, dry_run):
    """Re-run OCR on pages whose stored ocr_provider doesn't match the current config.

    Use after switching providers (e.g. myscript → claude) to refresh existing
    rows. By default skips pages already produced by the current provider;
    pass --all to force a full re-run.
    """
    try:
        cfg = config.load(ctx.obj["config_file"])
    except config.ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    if not cfg.get("ocr", {}).get("enabled", False):
        click.echo("ocr.enabled = false — nothing to do.", err=True)
        sys.exit(1)

    try:
        provider = _ocr_provider(cfg)
    except ValueError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    staging_dir = Path(cfg.get("sync", {}).get("staging_dir", "~/.muninn/staging")).expanduser()
    conn = db.connect()

    rows = conn.execute(
        """
        SELECT n.title, n.rm_folder, n.uuid, p.page_index, p.ocr_provider
        FROM pages p
        JOIN notebooks n ON n.uuid = p.notebook_uuid
        WHERE p.rm_hash IS NOT NULL
        ORDER BY n.rm_folder, n.title, p.page_index
        """
    ).fetchall()

    if folders:
        prefixes = tuple(p.lower().rstrip("/") for p in folders)
        rows = [
            r
            for r in rows
            if any(
                r["rm_folder"].lower() == prefix
                or r["rm_folder"].lower().startswith(f"{prefix}/")
                for prefix in prefixes
            )
        ]

    if not force_all:
        # NULL ocr_provider is treated as 'myscript' (legacy rows).
        rows = [r for r in rows if (r["ocr_provider"] or "myscript") != provider]

    if not rows:
        click.echo(f"All pages already OCR'd with provider {provider!r}.")
        return

    click.echo(f"Re-OCR'ing {len(rows)} page(s) with provider {provider!r}...\n")

    if dry_run:
        for r in rows:
            path = r["rm_folder"] or "(root)"
            current = r["ocr_provider"] or "myscript (legacy)"
            click.echo(f"  {path}/{r['title']} — page {r['page_index']}  ({current} → {provider})")
        return

    success = 0
    errors = 0
    for r in rows:
        rm_paths = rm_sync.list_page_files(staging_dir, r["uuid"])
        if r["page_index"] >= len(rm_paths):
            log.warning(
                "[%s] page %d: .rm file missing in staging — skipping",
                r["uuid"][:8],
                r["page_index"],
            )
            errors += 1
            continue
        rm_path = rm_paths[r["page_index"]]
        text = _transcribe_one(cfg, provider, rm_path, r["uuid"], r["page_index"])
        db.update_ocr_text(conn, r["uuid"], r["page_index"], text, provider)
        path = r["rm_folder"] or "(root)"
        status = "ok" if text else ("blank" if text == "" else "FAILED")
        click.echo(f"  [{status}] {path}/{r['title']} — page {r['page_index']}")
        if text is None:
            errors += 1
        else:
            success += 1

    click.echo(f"\nDone: {success} re-OCR'd, {errors} failed.")


@cli.command(name="ocr-compare")
@click.option(
    "--folder",
    "folders",
    multiple=True,
    metavar="PREFIX",
    help="Only compare pages in notebooks whose folder path starts with PREFIX (case-insensitive). Repeatable.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of pages compared.",
)
@click.option(
    "--save-pngs",
    is_flag=True,
    help="Save rendered PNGs to ~/.muninn/ocr-compare/ for inspection.",
)
@click.option(
    "--model",
    "models",
    multiple=True,
    metavar="MODEL_ID",
    help="Claude model to use. Repeat to compare multiple (e.g. --model claude-opus-4-7 --model claude-sonnet-4-6). Default: claude-opus-4-7.",
)
@click.pass_context
def ocr_compare(ctx, folders, limit, save_pngs, models):
    """Run Claude vision OCR on pages already transcribed by MyScript and print side-by-side.

    Read-only — does not modify the database. Renders strokes to PNG locally
    (works for v6 notebooks that rmrl can't convert).
    """
    try:
        cfg = config.load(ctx.obj["config_file"])
    except config.ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    cfg_key = cfg.get("vision", {}).get("api_key")
    if (not cfg_key or cfg_key == "YOUR_ANTHROPIC_API_KEY") and not os.environ.get(
        "ANTHROPIC_API_KEY"
    ):
        click.echo(
            "Set [vision].api_key in config.toml or export ANTHROPIC_API_KEY.",
            err=True,
        )
        sys.exit(1)

    staging_dir = Path(cfg.get("sync", {}).get("staging_dir", "~/.muninn/staging")).expanduser()
    conn = db.connect()

    rows = conn.execute(
        """
        SELECT n.title, n.rm_folder, n.uuid, p.page_index, p.ocr_text
        FROM pages p
        JOIN notebooks n ON n.uuid = p.notebook_uuid
        WHERE p.ocr_text IS NOT NULL AND p.ocr_text != ''
        ORDER BY n.rm_folder, n.title, p.page_index
        """
    ).fetchall()

    if folders:
        prefixes = tuple(p.lower().rstrip("/") for p in folders)
        rows = [
            r
            for r in rows
            if any(
                r["rm_folder"].lower() == prefix
                or r["rm_folder"].lower().startswith(f"{prefix}/")
                for prefix in prefixes
            )
        ]

    if limit:
        rows = rows[:limit]

    if not rows:
        click.echo("No pages with MyScript OCR text to compare.")
        return

    models = models or ("claude-opus-4-7",)
    click.echo(f"Comparing {len(rows)} page(s) across models: {', '.join(models)}\n")

    out_dir: Path | None = None
    if save_pngs:
        out_dir = staging_dir.parent / "ocr-compare"
        out_dir.mkdir(parents=True, exist_ok=True)

    for r in rows:
        rm_paths = rm_sync.list_page_files(staging_dir, r["uuid"])
        if r["page_index"] >= len(rm_paths):
            log.warning(
                "[%s] page %d: .rm file missing in staging — skipping",
                r["uuid"][:8],
                r["page_index"],
            )
            continue
        rm_path = rm_paths[r["page_index"]]

        try:
            png_bytes = rm_format.render_to_png(rm_path)
        except Exception as exc:
            log.error(
                "[%s] page %d: stroke render failed: %s",
                r["uuid"][:8],
                r["page_index"],
                exc,
            )
            continue

        if out_dir:
            out_path = out_dir / f"{r['uuid'][:8]}_p{r['page_index']:02d}.png"
            out_path.write_bytes(png_bytes)

        path = r["rm_folder"] or "(root)"
        click.echo(f"=== {path}/{r['title']} — page {r['page_index']} ===")
        click.echo("-- MyScript --")
        click.echo(r["ocr_text"])
        click.echo()
        for m in models:
            text = vision.transcribe_handwriting(cfg, png_bytes, model=m)
            display = "<API error>" if text is None else (text or "<blank>")
            click.echo(f"-- {m} --")
            click.echo(display)
            click.echo()


@cli.command()
@click.pass_context
def check(ctx):
    """Verify SSH, S3, MyScript, and vision API connectivity."""
    try:
        cfg = config.load(ctx.obj["config_file"])
    except config.ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    # SSH
    click.echo("Checking SSH...", nl=False)
    ok = rm_sync.check_ssh(cfg)
    click.echo(" ok" if ok else " FAILED")

    # OCR — provider-specific
    ocr_cfg = cfg.get("ocr", {})
    if not ocr_cfg.get("enabled", False):
        click.echo("Checking OCR... skipped (ocr.enabled = false)")
    else:
        try:
            provider = _ocr_provider(cfg)
        except ValueError as exc:
            click.echo(f"Checking OCR... FAILED ({exc})")
            return
        click.echo(f"Checking OCR ({provider})...", nl=False)
        if provider == "myscript":
            click.echo(" ok" if ocr.check(cfg) else " FAILED")
        else:  # claude
            # Smoke-test by transcribing a tiny blank PNG. We only care that
            # auth + reachability work, not what the text comes back as.
            from io import BytesIO

            from PIL import Image

            buf = BytesIO()
            Image.new("RGB", (200, 200), (255, 255, 255)).save(buf, format="PNG")
            result = vision.transcribe_handwriting(cfg, buf.getvalue())
            click.echo(" ok" if result is not None else " FAILED")

    # Vision (drawing descriptions)
    if not cfg.get("vision", {}).get("enabled", False):
        click.echo("Checking Vision (drawings)... skipped (vision.enabled = false)")
    else:
        click.echo("Checking Vision (drawings)...", nl=False)
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (200, 200), (255, 255, 255)).save(buf, format="PNG")
        result = vision.describe_page(cfg, buf.getvalue())
        click.echo(" ok" if result is not None else " FAILED")

    # Placeholders for later phases
    click.echo("Checking S3...           (not yet implemented)")
