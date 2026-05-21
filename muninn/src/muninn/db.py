import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks (
    uuid        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    rm_folder   TEXT NOT NULL DEFAULT '',
    file_hash   TEXT NOT NULL,
    last_synced TEXT NOT NULL  -- ISO-8601 UTC timestamp
);

CREATE TABLE IF NOT EXISTS pages (
    notebook_uuid     TEXT NOT NULL REFERENCES notebooks(uuid),
    page_index        INTEGER NOT NULL,
    rm_hash           TEXT,           -- SHA-256 of the .rm file; cache key for OCR
    png_hash          TEXT,           -- SHA-256 of the rendered PNG; cache key for vision (NULL if conversion failed)
    ocr_text          TEXT,           -- NULL = not yet processed or failed
    ocr_provider      TEXT,           -- 'claude' | 'myscript'; NULL on legacy rows (treated as 'myscript')
    vision_description TEXT,          -- NULL = not yet processed or failed
    PRIMARY KEY (notebook_uuid, page_index)
);
"""

# Idempotent migrations: each ALTER is wrapped in try/except OperationalError in connect().
_MIGRATIONS = (
    "ALTER TABLE pages ADD COLUMN rm_hash TEXT",
    "ALTER TABLE pages ADD COLUMN ocr_provider TEXT",
)


def upsert_notebook(
    conn: sqlite3.Connection,
    uuid: str,
    title: str,
    rm_folder: str,
    file_hash: str,
) -> None:
    """Insert or update a notebook record, setting last_synced to now (UTC)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO notebooks (uuid, title, rm_folder, file_hash, last_synced)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            title       = excluded.title,
            rm_folder   = excluded.rm_folder,
            file_hash   = excluded.file_hash,
            last_synced = excluded.last_synced
        """,
        (uuid, title, rm_folder, file_hash, now),
    )
    conn.commit()


def upsert_pages_rm(
    conn: sqlite3.Connection,
    notebook_uuid: str,
    rm_paths: list[Path],
) -> None:
    """Insert or update page rows from .rm files (the source of truth).

    When a page's rm_hash changes, ocr_text is cleared so it is re-OCR'd.
    Called before OCR even when PDF/PNG conversion failed (e.g. v6 notebooks).
    """
    for i, path in enumerate(rm_paths):
        rm_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        conn.execute(
            """
            INSERT INTO pages (notebook_uuid, page_index, rm_hash)
            VALUES (?, ?, ?)
            ON CONFLICT(notebook_uuid, page_index) DO UPDATE SET
                rm_hash = excluded.rm_hash,
                ocr_text = CASE
                    WHEN excluded.rm_hash != pages.rm_hash THEN NULL
                    ELSE pages.ocr_text
                END,
                ocr_provider = CASE
                    WHEN excluded.rm_hash != pages.rm_hash THEN NULL
                    ELSE pages.ocr_provider
                END,
                vision_description = CASE
                    WHEN excluded.rm_hash != pages.rm_hash THEN NULL
                    ELSE pages.vision_description
                END
            """,
            (notebook_uuid, i, rm_hash),
        )
    conn.commit()


def upsert_pages_png(
    conn: sqlite3.Connection,
    notebook_uuid: str,
    png_paths: list[Path],
) -> None:
    """Record PNG hashes for pages that successfully converted.

    Page rows must already exist (created by upsert_pages_rm). PNG hash drives
    cache invalidation for the vision step (Phase 5). OCR is unaffected.
    """
    for i, path in enumerate(png_paths):
        png_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        conn.execute(
            """
            UPDATE pages SET
                png_hash = ?,
                vision_description = CASE
                    WHEN ? != COALESCE(pages.png_hash, '') THEN NULL
                    ELSE pages.vision_description
                END
            WHERE notebook_uuid = ? AND page_index = ?
            """,
            (png_hash, png_hash, notebook_uuid, i),
        )
    conn.commit()


def get_cached_ocr(
    conn: sqlite3.Connection,
    notebook_uuid: str,
    page_index: int,
    provider: str,
) -> str | None:
    """Return cached OCR text for a page if it matches `provider`, else None.

    Legacy rows (ocr_provider IS NULL) are treated as MyScript-produced — they
    only return a cache hit when the caller is asking for MyScript output.
    """
    row = conn.execute(
        "SELECT ocr_text, ocr_provider FROM pages WHERE notebook_uuid = ? AND page_index = ?",
        (notebook_uuid, page_index),
    ).fetchone()
    if not row or row["ocr_text"] is None:
        return None
    cached_provider = row["ocr_provider"] or "myscript"
    if cached_provider != provider:
        return None
    return row["ocr_text"]


def update_ocr_text(
    conn: sqlite3.Connection,
    notebook_uuid: str,
    page_index: int,
    text: str | None,
    provider: str,
) -> None:
    """Persist OCR result (text or NULL on failure) and record which provider produced it."""
    stored_provider = provider if text is not None else None
    conn.execute(
        "UPDATE pages SET ocr_text = ?, ocr_provider = ? WHERE notebook_uuid = ? AND page_index = ?",
        (text, stored_provider, notebook_uuid, page_index),
    )
    conn.commit()


def get_cached_vision(
    conn: sqlite3.Connection, notebook_uuid: str, page_index: int
) -> str | None:
    """Return cached drawing description for a page, or None if not cached.

    Cache invalidates automatically when rm_hash changes (see upsert_pages_rm).
    """
    row = conn.execute(
        "SELECT vision_description FROM pages WHERE notebook_uuid = ? AND page_index = ?",
        (notebook_uuid, page_index),
    ).fetchone()
    return row["vision_description"] if row else None


def update_vision_description(
    conn: sqlite3.Connection,
    notebook_uuid: str,
    page_index: int,
    description: str | None,
) -> None:
    """Persist drawing description (text or NULL on failure)."""
    conn.execute(
        "UPDATE pages SET vision_description = ? WHERE notebook_uuid = ? AND page_index = ?",
        (description, notebook_uuid, page_index),
    )
    conn.commit()


def connect(db_path: Path = Path("muninn.db")) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column/table already in expected shape
    conn.commit()
    return conn
