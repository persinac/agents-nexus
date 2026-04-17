"""Auto-extract entity references from note content.

Extracts three classes of references:
  file      — paths with known source/config extensions (src/auth.py, config.yaml)
  wikilink  — [[entity-name]] Obsidian-style links
  mention   — @service-name or @module-name inline references

All extractors are regex-only — no external deps, no LLM call.
LLM-based extraction is deferred to 11e (causal inference batch jobs).
"""

from __future__ import annotations

import re

# File paths with common source/config extensions.
# Requires at least one directory separator OR an explicit extension to avoid
# matching bare words that happen to end in a known extension (e.g. "Go").
_FILE_EXTENSIONS = (
    "py|ts|tsx|js|jsx|go|rs|sql|sh|bash|zsh"
    "|yaml|yml|json|toml|md|tf|hcl|proto"
    "|java|kt|swift|rb|php|cs|cpp|c|h"
)

# Matches "some/path/file.ext" or "file.ext" — requires extension
_FILE_EXT_RE = re.compile(
    r'(?<![`\w/\\])(?:[\w.\-]+/)*[\w.\-]+\.(?:' + _FILE_EXTENSIONS + r')(?![`\w/\\])',
    re.IGNORECASE,
)

# Obsidian-style wikilinks: [[entity name]]
_WIKILINK_RE = re.compile(r'\[\[([^\]\n]+)\]\]')

# @mention — service / module names: @svc-chatbot, @auth-module
# Requires at least 2 chars, allows hyphens, no trailing punctuation
_MENTION_RE = re.compile(r'(?<![`\w])@([\w][\w\-]{1,60})(?![`\w])')


def extract_entities(content: str) -> list[tuple[str, str]]:
    """Return a deduplicated list of (entity_name, entity_type) from *content*.

    entity_type is one of: 'file', 'wikilink', 'mention'.

    Order: file paths first, then wikilinks, then @mentions.
    The first occurrence wins on duplicates (entity_type from the first match is kept).
    """
    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    for m in _FILE_EXT_RE.finditer(content):
        name = m.group(0)
        if name not in seen:
            seen.add(name)
            results.append((name, "file"))

    for m in _WIKILINK_RE.finditer(content):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            results.append((name, "wikilink"))

    for m in _MENTION_RE.finditer(content):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            results.append((name, "mention"))

    return results
