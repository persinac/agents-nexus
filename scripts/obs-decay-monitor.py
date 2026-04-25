# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Obsidian note decay monitor.

Weekly job. Scans active notes (excludes Daily Notes and Archive):
  - 30–89 days since last git commit, ≤1 inbound wikilink → adds status/stale to frontmatter
  - 90+ days since last git commit → moves to Archive/

Inbound wikilinks are the primary "keep alive" signal: a note that's been
referenced recently isn't truly stale even if untouched.

Run with --dry-run to preview without writing anything.
"""

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

VAULT_DIR = Path.home() / "obs-garner" / "Garner"
ARCHIVE_SUBDIR = "Archive"
DEFAULT_STALE_DAYS = 30
DEFAULT_ARCHIVE_DAYS = 180

SKIP_DIRS = {"Archive", "Daily Notes", ".git", ".trash"}


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def git_last_commit(vault: Path, rel: str) -> datetime | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(vault), "log", "-1", "--format=%at", "--", rel],
            capture_output=True, text=True, timeout=5,
        )
        ts = result.stdout.strip()
        if ts:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        pass
    return None


def last_modified(vault: Path, path: Path) -> datetime:
    # Prefer mtime — git commits are often bulk (tidy runs, sync) which would
    # reset all dates to the commit time, making decay useless. Mtime reflects
    # actual local edits. git is kept only as a cross-check sanity fallback
    # for files that don't exist on disk (shouldn't happen in normal operation).
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        rel = str(path.relative_to(vault))
        return git_last_commit(vault, rel) or datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Note collection
# ---------------------------------------------------------------------------

def collect_notes(vault: Path) -> list[Path]:
    notes = []
    for md in vault.rglob("*.md"):
        parts = md.relative_to(vault).parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        notes.append(md)
    return sorted(notes)


# ---------------------------------------------------------------------------
# Inbound wikilink index
# ---------------------------------------------------------------------------

def build_inbound_index(notes: list[Path]) -> dict[str, int]:
    """Return {note_stem: inbound_link_count} across all notes."""
    counts: dict[str, int] = {}
    pattern = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?]]")
    for note in notes:
        try:
            text = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in pattern.finditer(text):
            stem = m.group(1).strip()
            counts[stem] = counts.get(stem, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def read_note(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    fm: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = text[end + 5:]
    return fm, body


def write_note(path: Path, fm: dict, body: str) -> None:
    fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
    path.write_text(f"---\n{fm_str}\n---\n{body}", encoding="utf-8")


def set_status_tag(path: Path, status: str) -> None:
    fm, body = read_note(path)
    tags = fm.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    tags = [t for t in tags if not t.startswith("status/")]
    tags.append(f"status/{status}")
    fm["tags"] = sorted(tags)
    write_note(path, fm, body)


def clear_status_tag(path: Path) -> None:
    fm, body = read_note(path)
    tags = fm.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    cleaned = [t for t in tags if not t.startswith("status/")]
    if cleaned != tags:
        fm["tags"] = cleaned
        write_note(path, fm, body)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_note(vault: Path, path: Path) -> Path:
    archive_dir = vault / ARCHIVE_SUBDIR
    archive_dir.mkdir(exist_ok=True)
    dest = archive_dir / path.name
    # Avoid name collision
    if dest.exists():
        i = 1
        while dest.exists():
            dest = archive_dir / f"{path.stem}-{i}{path.suffix}"
            i += 1
    shutil.move(str(path), str(dest))
    return dest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Obsidian note decay monitor")
    parser.add_argument("--vault", type=Path, default=VAULT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    parser.add_argument("--archive-days", type=int, default=DEFAULT_ARCHIVE_DAYS)
    args = parser.parse_args()

    now = datetime.now(tz=timezone.utc)
    notes = collect_notes(args.vault)
    inbound = build_inbound_index(notes)

    archived: list[tuple[Path, int, int, Path]] = []
    flagged_stale: list[tuple[Path, int, int]] = []
    cleared_stale: list[tuple[Path, int, int]] = []

    for note in notes:
        age_days = (now - last_modified(args.vault, note)).days
        refs = inbound.get(note.stem, 0)
        rel = note.relative_to(args.vault)

        if age_days >= args.archive_days and refs == 0:
            # Notes with inbound wikilinks are protected from archiving —
            # being referenced means someone still depends on finding them here.
            if not args.dry_run:
                dest = archive_note(args.vault, note)
            else:
                dest = args.vault / ARCHIVE_SUBDIR / note.name
            archived.append((rel, age_days, refs, dest))

        elif age_days >= args.stale_days:
            if not args.dry_run:
                set_status_tag(note, "stale")
            flagged_stale.append((rel, age_days, refs))

        else:
            # Note is fresh — clear any leftover stale tag
            fm, _ = read_note(note)
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]
            if any(t == "status/stale" for t in tags):
                if not args.dry_run:
                    clear_status_tag(note)
                cleared_stale.append((rel, age_days, refs))

    # --- Report ---
    tag = "[DRY RUN] " if args.dry_run else ""
    print(f"\n=== Obsidian Decay Report — {now.strftime('%Y-%m-%d')} ===")
    print(f"Scanned {len(notes)} active notes (Daily Notes + Archive excluded)")
    print(f"Thresholds: stale={args.stale_days}d  archive={args.archive_days}d\n")

    if archived:
        print(f"Archived — {len(archived)} notes ({args.archive_days}+ days old, 0 inbound refs):")
        for rel, age, refs, dest in archived:
            dest_rel = dest.relative_to(args.vault)
            print(f"  {tag}{rel}  →  {dest_rel}  ({age}d old, {refs} inbound refs)")

    if flagged_stale:
        print(f"\nFlagged stale — {len(flagged_stale)} notes ({args.stale_days}+ days old, or {args.archive_days}d+ with inbound refs):")
        for rel, age, refs in flagged_stale:
            print(f"  {tag}{rel}  ({age}d old, {refs} inbound refs)")

    if cleared_stale:
        print(f"\nCleared stale tag — {len(cleared_stale)} notes (now fresh again):")
        for rel, age, refs in cleared_stale:
            print(f"  {tag}{rel}  ({age}d old, {refs} inbound refs)")

    if not archived and not flagged_stale and not cleared_stale:
        print("Nothing to do — all active notes are within decay thresholds.")

    print()


if __name__ == "__main__":
    main()
