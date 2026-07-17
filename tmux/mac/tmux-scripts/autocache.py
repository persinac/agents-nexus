#!/usr/bin/env python3
"""Auto-cache: snapshot the tail of the active Claude conversation to disk.

Called from hook-autocache.sh (chained by hook-stop.sh) after every assistant
turn.  Reads the most recent conversation JSONL for the current project,
extracts the last few user+assistant text exchanges, and writes them to
~/.tmux/cache/<project>.md.  On next session start, open-claude.sh injects
this file so the new agent can pick up where the interrupted one left off.
"""

import json
import os
import sys
import time
from pathlib import Path

MAX_EXCHANGES = 5
MAX_MSG_CHARS = 2000


def find_project_dir(cwd: str) -> Path | None:
    claude_projects = Path.home() / ".claude" / "projects"
    project_key = cwd.replace("/", "-")
    project_path = claude_projects / project_key
    if project_path.is_dir():
        return project_path
    return None


def find_latest_conversation(project_dir: Path) -> Path | None:
    jsonl_files = list(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    return max(jsonl_files, key=lambda f: f.stat().st_mtime)


def extract_exchanges(conversation_file: Path) -> list[dict]:
    messages = []
    with open(conversation_file) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            msg = obj.get("message", {})
            role = msg.get("role") or obj["type"]

            content = msg.get("content", [])
            if isinstance(content, str):
                text_parts = [content]
            else:
                text_parts = []
                for block in content:
                    if isinstance(block, str):
                        text_parts.append(block)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])

            if not text_parts:
                continue

            text = "\n".join(text_parts)
            if len(text) > MAX_MSG_CHARS:
                text = text[:MAX_MSG_CHARS] + "\n[...truncated]"

            messages.append({"role": role, "text": text})

    return messages[-(MAX_EXCHANGES * 2):]


def write_cache(project_slug: str, messages: list[dict]):
    cache_dir = Path.home() / ".tmux" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{project_slug}.md"

    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    lines = [
        f"# Auto-Cache — {project_slug} — {now}",
        "",
        "Below is the tail of your previous session's conversation.",
        "",
    ]

    for msg in messages:
        label = "User" if msg["role"] in ("user", "human") else "Assistant"
        lines.append(f"**{label}:**")
        lines.append(msg["text"])
        lines.append("")

    cache_file.write_text("\n".join(lines))


def main():
    cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    project_slug = os.path.basename(cwd)

    project_dir = find_project_dir(cwd)
    if not project_dir:
        return

    conversation = find_latest_conversation(project_dir)
    if not conversation:
        return

    messages = extract_exchanges(conversation)
    if not messages:
        return

    write_cache(project_slug, messages)


if __name__ == "__main__":
    main()
