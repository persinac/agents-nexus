#!/usr/bin/env python3
"""PostToolUse hook: reject ticket and requirement codes written into comments.

A reader hitting `FC-1920` or `OPEN-009` in a docstring cannot resolve it. The
tracker may be closed, renamed, or invisible to them, and the code carries none of
the reasoning it points at. Either the comment states the why itself, or it does
not need to exist. The reference belongs in the commit message, the MR
description, or the ticket.

Scope is comments only. A code in a string literal, a URL, an identifier, or a
test name is untouched — those are usually load-bearing.

Counts only ADDED lines, so an edit that leaves an existing reference alone never
trips. Edit/MultiEdit subtract what old_string already had, so moving a line does
not read as adding one.

Fails OPEN, always. Any unexpected condition exits 0 with no output — a broken
hook must never be able to stop Claude editing files.

Tune without editing this file:
  CC_TICKET_ALLOW        comma-separated extra prefixes to permit (e.g. RFC,ADR)
  CC_TICKET_OFF=1        disable entirely
"""
from __future__ import annotations

import json
import os
import re
import sys

# PREFIX-123 / PREFIX-0042. Two-plus letters keeps single-letter matches out, but
# D-13-style spec codes are real here, so a lone letter is allowed when the whole
# token sits inside a comment we already decided to inspect.
CODE = re.compile(r"\b([A-Z][A-Z0-9]{0,7})-(\d{1,6})\b")

# Standards, encodings and formats that LOOK like tickets. These name a public
# spec a reader can actually look up, which is the opposite of the problem.
ALLOW_PREFIXES = {
    "ISO", "RFC", "PEP", "UTF", "SHA", "AES", "RSA", "CVE", "CWE", "ANSI",
    "IEEE", "ECMA", "HTTP", "TLS", "SSL", "X", "IPV4", "IPV6", "BCP", "FIPS",
    "NIST", "OWASP", "HL7", "ICD", "CPT", "NDC", "LOINC", "SNOMED",
}
ALLOW_PREFIXES |= {
    p.strip().upper()
    for p in (os.environ.get("CC_TICKET_ALLOW") or "").split(",")
    if p.strip()
}

HASH = ("#",)
SLASH = ("//",)
DASH = ("--",)

LANGS: dict[str, tuple[tuple[str, ...], bool, bool]] = {
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

# Not product code. Migrations are the deliberate exception below.
SKIP_SUBSTRINGS = (
    "/node_modules/", "/.venv/", "/venv/", "/__pycache__/", "/dist/", "/build/",
    "/vendor/", "/.git/", "/site-packages/",
    "/.claude/", "/tmp/", "/private/tmp/",
)

# An alembic revision id is machine-read and a changelog entry names the change it
# shipped. Both legitimately carry the code.
SKIP_PATH_PARTS = ("/migrations/versions/", "/alembic/versions/")
SKIP_BASENAMES = ("changelog.md", "changelog.rst", "changes.md")


def comment_segments(text: str, markers: tuple[str, ...], block: bool, docstr: bool) -> list[str]:
    """Return the comment/docstring text in `text`, one entry per line.

    Trailing comments are included from the marker onward, so a code in the code
    part of the line is not scanned.
    """
    out: list[str] = []
    in_block = False
    in_doc: str | None = None

    for raw in text.splitlines():
        s = raw.strip()

        if in_doc is not None:
            out.append(s)
            if in_doc in s:
                in_doc = None
            continue
        if in_block:
            out.append(s)
            if "*/" in s:
                in_block = False
            continue

        if docstr and (s.startswith('"""') or s.startswith("'''")):
            q = s[:3]
            out.append(s)
            if not (len(s) > 5 and s.endswith(q)):
                in_doc = q
            continue
        if block and s.startswith("/*"):
            out.append(s)
            if "*/" not in s:
                in_block = True
            continue
        if any(s.startswith(m) for m in markers):
            out.append(s)
            continue

        # Trailing comment. Naive on purpose: a marker inside a string literal
        # yields a scan of harmless text, never a missed real comment.
        for m in markers:
            i = raw.find(m)
            if i > 0:
                out.append(raw[i:])
                break

    return out


def codes_in(segments: list[str]) -> list[str]:
    found: list[str] = []
    for seg in segments:
        for m in CODE.finditer(seg):
            if m.group(1).upper() in ALLOW_PREFIXES:
                continue
            found.append(m.group(0))
    return found


def main() -> int:
    if os.environ.get("CC_TICKET_OFF") == "1":
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

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
    if any(part in lower for part in SKIP_PATH_PARTS):
        return 0
    if os.path.basename(lower) in SKIP_BASENAMES:
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

    new_codes = codes_in([s for t in added for s in comment_segments(t, markers, has_block, has_doc)])
    if not new_codes:
        return 0

    old_codes = codes_in([s for t in removed for s in comment_segments(t, markers, has_block, has_doc)])
    for c in old_codes:
        if c in new_codes:
            new_codes.remove(c)
    if not new_codes:
        return 0

    uniq = sorted(set(new_codes))
    listed = ", ".join(uniq[:6]) + (f", and {len(uniq) - 6} more" if len(uniq) > 6 else "")

    reason = (
        f"TICKET REFS IN COMMENTS — {os.path.basename(path)}: {listed}.\n\n"
        "A reader cannot resolve a tracker code from inside the source. The ticket may be "
        "closed, renamed, or invisible to them, and the code carries none of the reasoning "
        "it points at.\n\n"
        "Rewrite each comment to state the why directly, or delete it. Put the reference in "
        "the commit message or the MR description, where it belongs and where it is "
        "clickable. Per ~/.claude/CLAUDE.md, reasoning goes in the commit message, never the "
        "source.\n\n"
        "If the code names a public standard rather than an internal ticket, add its prefix "
        "to CC_TICKET_ALLOW."
    )

    json.dump(
        {
            "decision": "block",
            "reason": reason,
            "systemMessage": f"ticket refs in comments: {listed} in {os.path.basename(path)}",
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
