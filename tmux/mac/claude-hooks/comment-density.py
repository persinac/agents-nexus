#!/usr/bin/env python3
"""Stop hook: block finishing while a file this session touched is comment-dense.

comment-budget.py is PostToolUse, so it only ever sees one edit. It cannot enforce
"before you finish any change, re-read your own diff and delete every comment that
fails the test" from ~/.claude/CLAUDE.md, because nothing looks at a finished file.
This does, at the one moment the agent believes it is done.

Measured per file as a ratio to CODE lines, the framing CLAUDE.md quotes:
    inline%    = inline comment lines / code lines
    docstring% = docstring lines      / code lines

A file is reported when it is over target AND the agent made it worse than the
committed version (or it is new). Inheriting someone else's dense file is not this
session's problem; adding to it is.

Files come from the session transcript, so scope is exactly what was edited — no
guessing a diff base, which is unresolvable for a stacked branch.

Tune without editing this file:
  CC_DENSITY_INLINE      inline%    target for source     (default 12)
  CC_DENSITY_DOC         docstring% target for source     (default 25)
  CC_DENSITY_TEST_DOC    docstring% target for tests/     (default 30)
  CC_DENSITY_MIN_CODE    skip files under N code lines    (default 15)
  CC_DENSITY_OFF=1       disable entirely

Fails OPEN, always.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

INLINE_TARGET = float(os.environ.get("CC_DENSITY_INLINE", "12"))
DOC_TARGET = float(os.environ.get("CC_DENSITY_DOC", "25"))
TEST_DOC_TARGET = float(os.environ.get("CC_DENSITY_TEST_DOC", "30"))
MIN_CODE = int(os.environ.get("CC_DENSITY_MIN_CODE", "15"))

HASH = ("#",)
SLASH = ("//",)

LANGS: dict[str, tuple[tuple[str, ...], bool, bool]] = {
    ".py": (HASH, False, True),
    ".pyi": (HASH, False, True),
    ".ts": (SLASH, True, False),
    ".tsx": (SLASH, True, False),
    ".js": (SLASH, True, False),
    ".jsx": (SLASH, True, False),
    ".go": (SLASH, True, False),
    ".rs": (SLASH, True, False),
    ".java": (SLASH, True, False),
    ".kt": (SLASH, True, False),
    ".swift": (SLASH, True, False),
    ".cs": (SLASH, True, False),
}

SKIP_SUBSTRINGS = (
    "/node_modules/", "/.venv/", "/venv/", "/__pycache__/", "/dist/", "/build/",
    "/vendor/", "/.git/", "/site-packages/", "/.claude/", "/tmp/", "/private/tmp/",
    "/migrations/", "/alembic/",
)


def classify(text: str, markers: tuple[str, ...], has_block: bool, has_doc: bool) -> tuple[int, int, int]:
    """Return (code, inline, docstring) non-blank line counts."""
    code = inline = doc = 0
    in_block = False
    in_doc: str | None = None

    for raw in text.splitlines():
        s = raw.strip()
        if in_doc is not None:
            doc += 1
            if in_doc in s:
                in_doc = None
            continue
        if in_block:
            inline += 1
            if "*/" in s:
                in_block = False
            continue
        if not s:
            continue
        if has_doc and (s.startswith('"""') or s.startswith("'''")):
            q = s[:3]
            doc += 1
            if not (len(s) > 5 and s.endswith(q)):
                in_doc = q
            continue
        if has_block and s.startswith("/*"):
            inline += 1
            if "*/" not in s:
                in_block = True
            continue
        if any(s.startswith(m) for m in markers):
            inline += 1
            continue
        code += 1
        # A trailing comment on a code line counts as both.
        for m in markers:
            if f" {m} " in raw:
                inline += 1
                break
    return code, inline, doc


def committed(path: str) -> str | None:
    d = os.path.dirname(path) or "."
    try:
        rel = subprocess.run(
            ["git", "-C", d, "ls-files", "--full-name", "--", os.path.basename(path)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not rel:
            return None
        out = subprocess.run(
            ["git", "-C", d, "show", f"HEAD:{rel}"], capture_output=True, text=True, timeout=5
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def touched_files(transcript_path: str) -> list[str]:
    seen: list[str] = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (rec.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") not in ("Write", "Edit", "MultiEdit"):
                        continue
                    fp = (block.get("input") or {}).get("file_path")
                    if fp and fp not in seen:
                        seen.append(fp)
    except OSError:
        return []
    return seen


def ratios(text: str, spec: tuple[tuple[str, ...], bool, bool]) -> tuple[int, float, float] | None:
    code, inline, doc = classify(text, *spec)
    if code < MIN_CODE:
        return None
    return code, inline / code * 100, doc / code * 100


def main() -> int:
    if os.environ.get("CC_DENSITY_OFF") == "1":
        return 0

    payload = json.load(sys.stdin)
    if payload.get("stop_hook_active"):
        return 0
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return 0

    findings = []
    for path in touched_files(transcript):
        lower = path.lower()
        if any(sub in lower for sub in SKIP_SUBSTRINGS):
            continue
        ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
        spec = LANGS.get(ext)
        if spec is None or not os.path.isfile(path):
            continue

        with open(path, encoding="utf-8", errors="replace") as fh:
            now = ratios(fh.read(), spec)
        if now is None:
            continue
        code, inline_pct, doc_pct = now

        is_test = "/tests/" in lower or "/test/" in lower or os.path.basename(lower).startswith("test_")
        doc_target = TEST_DOC_TARGET if is_test else DOC_TARGET
        if inline_pct <= INLINE_TARGET and doc_pct <= doc_target:
            continue

        old_text = committed(path)
        if old_text is not None:
            before = ratios(old_text, spec)
            # Only this session's contribution is actionable.
            if before is not None and inline_pct <= before[1] + 0.5 and doc_pct <= before[2] + 0.5:
                continue
            was = f"was {before[1]:.0f}%/{before[2]:.0f}%" if before else "new content"
        else:
            was = "untracked"

        over = []
        if inline_pct > INLINE_TARGET:
            over.append(f"inline {inline_pct:.0f}% (target {INLINE_TARGET:.0f}%)")
        if doc_pct > doc_target:
            over.append(f"docstring {doc_pct:.0f}% (target {doc_target:.0f}%)")
        rel = os.path.relpath(path)
        findings.append(f"  {rel} — {', '.join(over)}; {code} code lines, {was}")

    if not findings:
        return 0

    reason = (
        "COMMENT DENSITY — files you edited this session are over target, as a ratio to "
        "code lines:\n\n" + "\n".join(findings) + "\n\n"
        "Per ~/.claude/CLAUDE.md 'Code comments — default to zero': re-read your own diff "
        "and delete every comment that does not answer a why the code, the type signature, "
        "the test name, and git log cannot answer. Budget is at most one comment per "
        "function; zero is the expected value. Docstrings are one line unless they document "
        "a caller-visible contract. Move real rationale to the commit message or MR "
        "description.\n\n"
        "Then finish. If a flagged comment records a why an incident proved unrecoverable "
        "from the code, or a caller-visible contract, keep it and say so in your reply."
    )
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
