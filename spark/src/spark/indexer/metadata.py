"""Per-installation indexing metadata persisted as a single JSON file.

Lives at <index_path>/installations.json. Maps relative path -> indexing
record. Read on every `spark sync` run; rewritten atomically at the end.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger("spark.metadata")


@dataclass
class InstallationMeta:
    indexed_at: str               # ISO 8601 UTC timestamp of last successful index
    last_remote_ts: int           # unix epoch seconds of origin/HEAD at index time
    clone_url: str                # captured `git remote get-url origin` at index time
    detected: dict | None = None  # serialized DetectedProject (registry/manifest projection)


def load_metadata(path: Path) -> dict[str, InstallationMeta]:
    """Read installations.json. Returns {} if missing or corrupt."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("metadata file at %s is unreadable (%s); treating as empty", path, e)
        return {}
    out: dict[str, InstallationMeta] = {}
    for rel_path, entry in raw.items():
        try:
            out[rel_path] = InstallationMeta(
                indexed_at=entry["indexed_at"],
                last_remote_ts=int(entry["last_remote_ts"]),
                clone_url=entry.get("clone_url", ""),
                detected=entry.get("detected"),  # absent on pre-manifest indexes
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("skipping malformed metadata entry %s: %s", rel_path, e)
    return out


def save_metadata(path: Path, meta: dict[str, InstallationMeta]) -> None:
    """Atomically replace installations.json with the current state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {k: asdict(v) for k, v in sorted(meta.items())}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serializable, indent=2))
    os.replace(tmp, path)
