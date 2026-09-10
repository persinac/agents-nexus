#!/usr/bin/env python3
"""PostToolUse hook: push back when an edit adds too many code-comment lines.

Enforces the "Code comments — default to zero" section of ~/.claude/CLAUDE.md at
edit time. An instruction given at the start of a long session decays; this does
not.

Counts only ADDED comment lines, so reformatting or deleting comments never trips
it. Edit/MultiEdit subtract what old_string already had. Write subtracts the
pre-write file, snapshotted by the PreToolUse comment-budget-snapshot.py — without
that snapshot a whole-file rewrite scores as 100% added and a rewrite that DELETES
comments is reported as adding them.

Fails OPEN, always. Any unexpected condition exits 0 with no output — a broken
hook must never be able to stop Claude editing files.

Tune without editing this file:
  CC_COMMENT_MAX_ADDED   total added comment lines allowed per call (default 4)
  CC_COMMENT_MAX_BLOCK   longest contiguous comment run allowed    (default 3)
  CC_COMMENT_OFF=1       disable entirely
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

MAX_ADDED = int(os.environ.get("CC_COMMENT_MAX_ADDED", "4"))
MAX_BLOCK = int(os.environ.get("CC_COMMENT_MAX_BLOCK", "3"))

# Line-comment marker(s) and whether the language has triple-quote docstrings.
HASH = ("#",)
SLASH = ("//",)
DASH = ("--",)

LANGS: dict[str, tuple[tuple[str, ...], bool, bool]] = {
    # ext: (line markers, has /* */ blocks, has python docstrings)
    ".py": (HASH, False, True),
    ".pyi": (HASH, False, True),
    ".ts": (SLASH, True, False),
    ".tsx": (SLASH, True, False),
    ".js": (SLASH, True, False),
    ".jsx": (SLASH, True, False),
    ".mjs": (SLASH, True, False),
    ".cjs": (SLASH, True, False),
    ".go": (SLASH, True, False),
    ".rs": (SLASH, True, False),
    ".java": (SLASH, True, False),
    ".kt": (SLASH, True, False),
    ".swift": (SLASH, True, False),
    ".scala": (SLASH, True, False),
    ".c": (SLASH, True, False),
    ".h": (SLASH, True, False),
    ".cpp": (SLASH, True, False),
    ".hpp": (SLASH, True, False),
    ".cs": (SLASH, True, False),
    ".sql": (DASH, True, False),
    ".sh": (HASH, False, False),
    ".bash": (HASH, False, False),
    ".zsh": (HASH, False, False),
    ".rb": (HASH, False, False),
    ".tf": (HASH, False, False),
}

# Not product code. Tooling and scratch are documentation-heavy by design.
SKIP_SUBSTRINGS = (
    "/node_modules/", "/.venv/", "/venv/", "/__pycache__/", "/dist/", "/build/",
    "/vendor/", "/.git/", "/site-packages/",
    "/.claude/", "/tmp/", "/private/tmp/",
)


def comment_lines(text: str, markers: tuple[str, ...], block: bool, docstr: bool) -> list[int]:
    """Return the length of each contiguous comment run in `text`."""
    runs: list[int] = []
    cur = 0
    in_block = False
    in_doc: str | None = None

    for raw in text.splitlines():
        s = raw.strip()
        is_comment = False

        if in_doc is not None:
            is_comment = True
            if in_doc in s:
                in_doc = None
        elif in_block:
            is_comment = True
            if "*/" in s:
                in_block = False
        elif docstr and (s.startswith('"""') or s.startswith("'''")):
            q = s[:3]
            is_comment = True
            # Same-line close (""" one liner """) does not open a block.
            if not (len(s) > 5 and s.endswith(q)):
                in_doc = q
        elif block and s.startswith("/*"):
            is_comment = True
            if "*/" not in s:
                in_block = True
        elif any(s.startswith(m) for m in markers):
            is_comment = True

        if is_comment:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


def pre_write_content(file_path: str) -> str:
    """Consume the PreToolUse snapshot of `file_path`, or "" for a genuinely new file.

    Falls back to the committed version, since a missing snapshot would otherwise
    score the whole rewrite as added. Both are best-effort: no baseline is still
    better than refusing to run.
    """
    key = hashlib.sha256(os.path.realpath(file_path).encode()).hexdigest()[:32]
    snap = os.path.join(tempfile.gettempdir(), "cc-comment-budget", f"{key}.snap")
    try:
        with open(snap, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        os.unlink(snap)
        return content
    except OSError:
        pass

    import subprocess

    try:
        d = os.path.dirname(file_path) or "."
        rel = subprocess.run(
            ["git", "-C", d, "ls-files", "--full-name", "--", os.path.basename(file_path)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not rel:
            return ""
        out = subprocess.run(
            ["git", "-C", d, "show", f"HEAD:{rel}"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def main() -> int:
    if os.environ.get("CC_COMMENT_OFF") == "1":
        return 0

    payload = json.load(sys.stdin)
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}

    path = (
        ti.get("file_path")
        or ti.get("path")
        or ((payload.get("tool_response") or {}).get("filePath") if isinstance(payload.get("tool_response"), dict) else None)
        or ""
    )
    if not path:
        return 0

    lower = path.lower()
    if any(sub in lower for sub in SKIP_SUBSTRINGS):
        return 0

    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    spec = LANGS.get(ext)
    if spec is None:
        return 0
    markers, has_block, has_doc = spec

    added: list[str] = []
    removed: list[str] = []
    if tool == "Write":
        added.append(ti.get("content") or "")
        removed.append(pre_write_content(path))
    elif tool == "Edit":
        added.append(ti.get("new_string") or "")
        removed.append(ti.get("old_string") or "")
    elif tool == "MultiEdit":
        for e in ti.get("edits") or []:
            if isinstance(e, dict):
                added.append(e.get("new_string") or "")
                removed.append(e.get("old_string") or "")
    else:
        return 0

    new_runs = [r for t in added for r in comment_lines(t, markers, has_block, has_doc)]
    old_total = sum(r for t in removed for r in comment_lines(t, markers, has_block, has_doc))

    net = sum(new_runs) - old_total
    longest = max(new_runs) if new_runs else 0

    over_total = net > MAX_ADDED
    over_block = longest > MAX_BLOCK
    if not (over_total or over_block):
        return 0

    why = []
    if over_total:
        why.append(f"{net} net comment lines added (budget {MAX_ADDED})")
    if over_block:
        why.append(f"a {longest}-line contiguous comment block (budget {MAX_BLOCK})")

    reason = (
        f"COMMENT BUDGET — {os.path.basename(path)}: " + "; ".join(why) + ".\n\n"
        "Per ~/.claude/CLAUDE.md 'Code comments — default to zero': a comment earns its "
        "place only if it answers a why that the code, the type signature, the test name, "
        "and git log cannot. If it describes what the line does, delete it. Docstrings are "
        "one line unless documenting a caller-visible contract.\n\n"
        "Re-read what you just wrote and delete the comments that fail that test. Move any "
        "real rationale to the commit message or MR description. If a comment genuinely "
        "records a why an incident proved unrecoverable from the code, keep it and say so."
    )

    json.dump(
        {
            "decision": "block",
            "reason": reason,
            "systemMessage": f"comment budget: {'; '.join(why)} in {os.path.basename(path)}",
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open — never block an edit because this hook broke.
        sys.exit(0)
