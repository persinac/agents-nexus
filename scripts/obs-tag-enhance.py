# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic", "pyyaml"]
# ///
"""Enhance Obsidian notes with structured tags using Claude Haiku.

Nightly job. First run processes all active notes (~325). Subsequent runs
only re-process notes whose mtime changed since the last run.

State is tracked in .obs-tag-state.json (next to this script).
Tags are written to YAML frontmatter — inline #hashtags in note bodies
are left untouched.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import anthropic
import yaml

VAULT_DIR = Path(os.environ.get("VAULT_DIR", Path.home() / "obs-garner" / "Garner"))
STATE_FILE = Path(__file__).parent / ".obs-tag-state.json"
BATCH_SIZE = 8

# Dirs to skip entirely — these notes don't benefit from taxonomy tagging
SKIP_DIRS = {"Archive", ".git", ".trash"}

SYSTEM_PROMPT = """\
You are a knowledge management assistant that tags Obsidian notes.

Taxonomy (use ONLY these prefixes — no others):
  type/   → daily, 1on1, meeting, architecture, til, runbook, interview, review, idea, oncall
  area/   → infra, data, platform, search, dev, mgmt
  person/ → <firstname> for anyone mentioned prominently, e.g. person/drew, person/paul
  project/→ <kebab-name> for specific projects, e.g. project/chatbot, project/agents-nexus

Rules:
- 2–5 tags per note, selective not exhaustive
- Folder name is the strongest hint for type/ (One on Ones → type/1on1, Daily Notes → type/daily, etc.)
- Never return date tags like 2025-01-01
- Return valid JSON only, no prose, no markdown fences"""


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def collect_notes(vault: Path) -> list[Path]:
    notes = []
    for md in vault.rglob("*.md"):
        parts = md.relative_to(vault).parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        notes.append(md)
    return sorted(notes)


def read_note(path: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Parses YAML block if present."""
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


def tag_batch(
    client: anthropic.Anthropic, batch: list[tuple[Path, str, str]]
) -> dict[str, list[str]]:
    """Send up to BATCH_SIZE notes to Claude, return {str(idx): [tags]}."""
    parts = []
    for idx, (path, folder, body) in enumerate(batch):
        snippet = body[:600].replace("\n", " ").strip()
        parts.append(f"[{idx}] Folder: {folder}\nTitle: {path.stem}\nContent: {snippet}")

    user_msg = (
        "Tag each note. Return a JSON object mapping each index (as string) "
        "to a tag array.\n\n" + "\n\n".join(parts)
    )

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


def apply_tags(path: Path, new_tags: list[str]) -> bool:
    """Write taxonomy tags to frontmatter. Returns True if the file changed."""
    fm, body = read_note(path)

    existing = fm.get("tags", [])
    if isinstance(existing, str):
        existing = [existing]

    # Preserve non-taxonomy tags already in frontmatter (e.g. status/)
    taxonomy_prefixes = ("type/", "area/", "person/", "project/")
    preserved = [t for t in existing if not any(t.startswith(p) for p in taxonomy_prefixes)]

    merged = sorted(set(preserved + new_tags))
    if merged == sorted(existing):
        return False

    fm["tags"] = merged
    write_note(path, fm, body)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enhance Obsidian notes with tags via Claude Haiku"
    )
    parser.add_argument("--vault", type=Path, default=VAULT_DIR)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print proposed tags without writing"
    )
    parser.add_argument(
        "--all", dest="force_all", action="store_true", help="Re-process all notes"
    )
    parser.add_argument("--limit", type=int, default=0, help="Max notes to process (0=all)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    state = {} if args.force_all else load_state()
    client = anthropic.Anthropic(api_key=api_key)

    notes = collect_notes(args.vault)
    to_process = [
        n for n in notes
        if args.force_all or state.get(str(n.relative_to(args.vault)), 0) < n.stat().st_mtime
    ]
    if args.limit:
        to_process = to_process[: args.limit]

    print(f"Processing {len(to_process)} / {len(notes)} notes  (vault: {args.vault})")

    processed = changed = errors = 0

    for i in range(0, len(to_process), BATCH_SIZE):
        batch_paths = to_process[i : i + BATCH_SIZE]
        batch: list[tuple[Path, str, str]] = []
        for path in batch_paths:
            parts = path.relative_to(args.vault).parts
            folder = parts[0] if len(parts) > 1 else ""
            _, body = read_note(path)
            batch.append((path, folder, body))

        try:
            results = tag_batch(client, batch)
        except (json.JSONDecodeError, anthropic.APIError) as e:
            print(f"  batch {i // BATCH_SIZE + 1} error: {e}", file=sys.stderr)
            errors += len(batch)
            continue

        for idx, (path, folder, body) in enumerate(batch):
            tags = results.get(str(idx), [])
            rel = str(path.relative_to(args.vault))

            if args.dry_run:
                print(f"  {rel}: {tags}")
            else:
                if apply_tags(path, tags):
                    changed += 1
                state[rel] = path.stat().st_mtime

            processed += 1

        if not args.dry_run:
            save_state(state)

        done = min(i + BATCH_SIZE, len(to_process))
        print(f"  [{done}/{len(to_process)}]", end="\r", flush=True)

    print(f"\nDone. processed={processed}  changed={changed}  errors={errors}")


if __name__ == "__main__":
    main()
