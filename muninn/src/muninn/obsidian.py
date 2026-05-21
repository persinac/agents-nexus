"""Render OCR'd notebook content into Obsidian-compatible Markdown.

One `.md` file per notebook, named after the (sanitized) notebook title and
written into the matching vault's subfolder. Multi-vault routing uses the
notebook's `rm_folder` against each vault's `folders` prefix list; first
match wins, default vault catches the rest.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid sqlite3.Row leaking into the public signatures
    import sqlite3

log = logging.getLogger(__name__)

# Characters that aren't safe in filenames on macOS/Linux/Windows.
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(title: str) -> str:
    """Replace filesystem-unsafe characters with underscores; collapse runs."""
    name = _UNSAFE_FILENAME_CHARS.sub("_", title.strip())
    name = re.sub(r"_+", "_", name)
    name = name.strip(". _")
    return name or "untitled"


def pick_vault(rm_folder: str, vaults: list[dict]) -> dict:
    """Return the first vault whose `folders` prefix matches `rm_folder`.

    Matching is case-insensitive on path segments (`"work"` matches `"Work/Garner"`).
    Falls back to the vault marked `default = true` when nothing matches.
    """
    folder = (rm_folder or "").lower().rstrip("/")
    for vault in vaults:
        for prefix in vault.get("folders") or []:
            p = prefix.lower().rstrip("/")
            if folder == p or folder.startswith(f"{p}/"):
                return vault
    for vault in vaults:
        if vault.get("default"):
            return vault
    raise RuntimeError("No default vault configured")


def vault_dir(vault: dict) -> Path:
    """Resolve `vault.path` (+ optional `subfolder`) into an absolute Path."""
    base = Path(vault["path"]).expanduser()
    sub = vault.get("subfolder")
    return (base / sub) if sub else base


def build_markdown(
    *,
    title: str,
    rm_folder: str,
    notebook_uuid: str,
    last_synced: str,
    pages: list[dict],
) -> str:
    """Render a notebook into a single Markdown document.

    `pages` is an ordered list of dicts with keys `page_index`, `ocr_text`,
    `vision_description`. Either content field may be None (failed) or ""
    (empty); both are treated as "absent" for rendering. Pages with neither
    field populated render the `*No content detected*` placeholder.
    """
    fm = ["---", f'title: "{_escape_yaml(title)}"']
    if rm_folder:
        fm.append(f'rm_folder: "{_escape_yaml(rm_folder)}"')
    fm.extend(
        [
            f"notebook_id: {notebook_uuid}",
            f"last_synced: {last_synced}",
            f"pages: {len(pages)}",
            "---",
            "",
            f"# {title}",
            "",
        ]
    )

    parts: list[str] = ["\n".join(fm)]
    for p in pages:
        ocr = (p.get("ocr_text") or "").strip()
        vis = (p.get("vision_description") or "").strip()
        parts.append(f"## Page {p['page_index'] + 1}")
        parts.append("")
        if not ocr and not vis:
            parts.append("*No content detected*")
            parts.append("")
            continue
        if ocr:
            parts.append("### Transcription")
            parts.append("")
            parts.append(ocr)
            parts.append("")
        if vis:
            parts.append("### Drawing")
            parts.append("")
            parts.append(vis)
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def write_notebook_atomic(target: Path, content: str) -> None:
    """Write `content` to `target` atomically (tmp file + rename).

    Errors clearly if the vault root (target.parent.parent) doesn't exist —
    we'll create the `subfolder` automatically, but never silently create a
    missing vault path.
    """
    vault_root = target.parent.parent if target.parent.name else target.parent
    if not vault_root.exists():
        raise FileNotFoundError(
            f"Vault path does not exist: {vault_root}. "
            "Create the directory or update [[vaults]].path in config.toml."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def fetch_pages(conn: "sqlite3.Connection", notebook_uuid: str) -> list[dict]:
    """Pull ordered page rows for a notebook out of the DB."""
    rows = conn.execute(
        """
        SELECT page_index, ocr_text, vision_description
        FROM pages
        WHERE notebook_uuid = ?
        ORDER BY page_index
        """,
        (notebook_uuid,),
    ).fetchall()
    return [dict(r) for r in rows]


def _escape_yaml(s: str) -> str:
    """Minimal YAML double-quoted-string escaping (backslash + double-quote)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
