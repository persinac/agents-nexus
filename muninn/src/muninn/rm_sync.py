import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

XOCHITL_PATH = "/home/root/.local/share/remarkable/xochitl"


def check_ssh(cfg: dict) -> bool:
    """Return True if an SSH connection to the rM1 succeeds."""
    ssh = cfg["ssh"]
    host = ssh["host"]
    port = ssh.get("port", 22)
    username = ssh.get("username", "root")
    identity_file = str(Path(ssh["identity_file"]).expanduser())
    timeout = ssh.get("timeout", 10)

    cmd = [
        "ssh",
        "-i",
        identity_file,
        "-p",
        str(port),
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        f"{username}@{host}",
        "echo ok",
    ]

    log.debug("SSH check: %s@%s:%s", username, host, port)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("SSH connection timed out after %ds", timeout)
        return False
    except Exception as exc:
        log.error("SSH check failed: %s", exc)
        return False


def pull(cfg: dict, staging_dir: Path) -> bool:
    """Mirror the rM1 xochitl directory to staging_dir via rclone over SSH.

    Returns True on success, False if the device was unreachable or rclone failed.
    Does not modify any local state — safe to call before DB writes.
    """
    ssh = cfg["ssh"]
    host = ssh["host"]
    port = ssh.get("port", 22)
    username = ssh.get("username", "root")
    identity_file = str(Path(ssh["identity_file"]).expanduser())
    timeout = ssh.get("timeout", 10)
    rclone_bin = cfg.get("sync", {}).get("rclone_bin", "rclone")

    staging_dir.mkdir(parents=True, exist_ok=True)

    # rclone sftp remote is specified inline as `:sftp:<path>`
    source = f":sftp:{XOCHITL_PATH}/"

    cmd = [
        rclone_bin,
        "copy",
        source,
        str(staging_dir) + "/",
        "--sftp-host",
        host,
        "--sftp-port",
        str(port),
        "--sftp-user",
        username,
        "--sftp-key-file",
        identity_file,
        "--contimeout",
        f"{timeout}s",
        "--log-level",
        "INFO",
    ]

    log.info("Pulling %s@%s:%s → %s", username, host, XOCHITL_PATH, staging_dir)
    # rM1's WiFi power-save aggressively sleeps the radio between probes.
    # A successful SSH check (~1s) often wakes the radio just long enough to
    # answer, then it goes back to sleep before rclone can establish its
    # session — manifests as `connect: no route to host` partway through.
    # One retry after a brief delay usually catches the radio while still awake.
    max_attempts = 2
    backoff_seconds = 3
    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 300,  # generous budget for large vaults
            )
        except subprocess.TimeoutExpired:
            log.error("rclone timed out — device unreachable or transfer stalled")
            return False
        except FileNotFoundError:
            log.error(
                "rclone not found at %r. Install rclone and set sync.rclone_bin in config.toml",
                rclone_bin,
            )
            return False

        if result.returncode == 0:
            if attempt > 1:
                log.info("Pull complete on attempt %d", attempt)
            else:
                log.info("Pull complete")
            return True

        stderr = result.stderr.strip()
        if attempt < max_attempts and _looks_transient(stderr):
            log.warning(
                "rclone attempt %d/%d failed (%s); retrying in %ds",
                attempt,
                max_attempts,
                _short_error(stderr),
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
            continue

        log.error("rclone exited %d: %s", result.returncode, stderr)
        return False
    return False


_TRANSIENT_PATTERNS = (
    "no route to host",
    "connection refused",
    "connection reset",
    "connection timed out",
    "i/o timeout",
    "EOF",
    "broken pipe",
    "network is unreachable",
    "ssh: handshake failed",
)


def _looks_transient(stderr: str) -> bool:
    """Return True if the rclone stderr looks like a recoverable network blip."""
    lower = stderr.lower()
    return any(p.lower() in lower for p in _TRANSIENT_PATTERNS)


def _short_error(stderr: str) -> str:
    """Pull a one-line summary out of rclone's multi-line stderr for logging."""
    for line in stderr.splitlines():
        line = line.strip()
        if "CRITICAL" in line or "ERROR" in line:
            return line[:120]
    return stderr[:120] if stderr else "(no error output)"


def detect_changes(staging_dir: Path, conn) -> tuple[list[str], list[str], list[str]]:
    """Compare staged notebooks against DB state.

    Returns (new_uuids, modified_uuids, unchanged_uuids).
    """
    staged_uuids = {p.stem for p in staging_dir.glob("*.metadata")}

    new_uuids: list[str] = []
    modified_uuids: list[str] = []
    unchanged_uuids: list[str] = []

    for uuid in sorted(staged_uuids):
        current_hash = _hash_notebook(staging_dir, uuid)
        row = conn.execute("SELECT file_hash FROM notebooks WHERE uuid = ?", (uuid,)).fetchone()

        if row is None:
            log.debug("New notebook: %s", uuid)
            new_uuids.append(uuid)
        elif row["file_hash"] != current_hash:
            log.debug("Modified notebook: %s", uuid)
            modified_uuids.append(uuid)
        else:
            log.debug("Unchanged notebook: %s", uuid)
            unchanged_uuids.append(uuid)

    return new_uuids, modified_uuids, unchanged_uuids


def notebook_title(staging_dir: Path, uuid: str) -> str:
    """Return the human-readable title from a notebook's .metadata file."""
    meta_path = staging_dir / f"{uuid}.metadata"
    try:
        data = json.loads(meta_path.read_text())
        return data.get("visibleName", uuid)
    except Exception:
        return uuid


def notebook_folder(
    staging_dir: Path, uuid: str, folder_map: dict[str, str] | None = None
) -> str:
    """Return the resolved folder path for a notebook (e.g., "Work/Garner/Chatbot").

    Returns an empty string for notebooks at the root. Trashed notebooks resolve
    to a path starting with "trash/" (or exactly "trash" when the notebook itself
    was trashed without being inside a folder), so callers can filter with
    `not path.lower().startswith("trash")`.
    """
    if folder_map is None:
        folder_map = build_folder_map(staging_dir)
    meta_path = staging_dir / f"{uuid}.metadata"
    try:
        data = json.loads(meta_path.read_text())
    except Exception:
        return ""
    parent = data.get("parent", "")
    if parent == "":
        return ""
    if parent == "trash":
        return "trash"
    return folder_map.get(parent, parent)


def build_folder_map(staging_dir: Path) -> dict[str, str]:
    """Walk all CollectionType .metadata files and return a UUID → resolved-path map.

    Resolves nested folders into slash-separated paths (e.g. "Work/Garner/Chatbot").
    Folders inside the trash carry a "trash/" prefix.
    """
    raw: dict[str, tuple[str, str]] = {}  # uuid → (visibleName, parent)
    for meta_path in staging_dir.glob("*.metadata"):
        try:
            d = json.loads(meta_path.read_text())
        except Exception:
            continue
        if d.get("type") != "CollectionType":
            continue
        raw[meta_path.stem] = (d.get("visibleName", meta_path.stem), d.get("parent", ""))

    paths: dict[str, str] = {}

    def resolve(uuid: str, stack: set[str]) -> str:
        if uuid in paths:
            return paths[uuid]
        if uuid in stack:  # cycle guard
            paths[uuid] = uuid
            return uuid
        if uuid not in raw:
            return ""
        name, parent = raw[uuid]
        if parent == "":
            result = name
        elif parent == "trash":
            result = f"trash/{name}"
        else:
            stack.add(uuid)
            parent_path = resolve(parent, stack)
            stack.discard(uuid)
            result = f"{parent_path}/{name}" if parent_path else name
        paths[uuid] = result
        return result

    for uuid in raw:
        resolve(uuid, set())
    return paths


def list_page_files(staging_dir: Path, uuid: str) -> list[Path]:
    """Return `.rm` page files for a notebook in display order.

    Reads `<uuid>.content` to get the ordered page UUIDs. Both legacy `pages: [uuid,...]`
    (v5-era notebooks) and v6 `cPages.pages: [{id, deleted?}, ...]` are supported.
    Pages flagged as deleted are excluded. Falls back to alphabetical `.rm` ordering
    if `.content` is unreadable.
    """
    content_path = staging_dir / f"{uuid}.content"
    nb_dir = staging_dir / uuid
    if not nb_dir.is_dir():
        return []

    page_uuids: list[str] = []
    try:
        data = json.loads(content_path.read_text())
        cp = data.get("cPages")
        if cp and isinstance(cp.get("pages"), list):
            page_uuids = [p["id"] for p in cp["pages"] if isinstance(p, dict) and p.get("id") and not p.get("deleted")]
        elif isinstance(data.get("pages"), list):
            page_uuids = list(data["pages"])
    except Exception:
        log.warning("Could not parse %s.content; falling back to alphabetical order", uuid)

    if not page_uuids:
        return sorted(nb_dir.glob("*.rm"))

    paths = []
    for pid in page_uuids:
        p = nb_dir / f"{pid}.rm"
        if p.exists():
            paths.append(p)
    return paths


def _hash_notebook(staging_dir: Path, uuid: str) -> str:
    """Compute a stable SHA-256 over all files belonging to a notebook."""
    h = hashlib.sha256()

    for suffix in (".metadata", ".content"):
        p = staging_dir / f"{uuid}{suffix}"
        if p.exists():
            h.update(p.read_bytes())

    nb_dir = staging_dir / uuid
    if nb_dir.is_dir():
        for page_file in sorted(nb_dir.glob("*.rm")):
            h.update(page_file.name.encode())
            h.update(page_file.read_bytes())

    return h.hexdigest()
