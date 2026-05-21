"""Daily-note merge step.

Takes the OCR'd content of a daily-source notebook (e.g. "Daily"), asks
Claude to extract a date + structured sections (TODOs, questions, notes),
carries TODOs forward from the most recent previous daily note with their
ages bumped, and merges into the matching `<target_dir>/<YYYY-MM-DD>.md`
in the vault.

TODO format matches the user's daily-note convention:
  | Thing | Status | Age |
  | --- | --- | --- |
  | item text | Todo | 14d |

Idempotent via marker comments — re-merging the same content is a no-op;
re-merging after edits replaces the muninn-managed block in place.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

from muninn import obsidian

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-7"
START_MARKER_RE = re.compile(
    r"<!--\s*muninn-daily-start\s+rm_hash=(\w+)\s+synced=(\S+?)\s*-->"
)
END_MARKER = "<!-- muninn-daily-end -->"
_DAILY_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_STATUS_NORM = {
    "todo": "Todo",
    "in progress": "In Progress",
    "inprogress": "In Progress",
    "wip": "In Progress",
    "in review": "In Review",
    "review": "In Review",
    "done": "Done",
    "blocked": "Blocked",
}


class TodoItem(BaseModel):
    text: str
    status: Literal["Todo", "In Progress", "In Review", "Done", "Blocked"]
    age_days: int


class DailySections(BaseModel):
    """Structured sections extracted from a daily-source notebook page."""

    date: str  # YYYY-MM-DD
    date_confidence: Literal["high", "low"]
    todos: list[TodoItem]
    questions: list[str]
    notes: str


_PROMPT_TEMPLATE = """\
Today's date is {today}. Below is the OCR'd transcription of a handwritten daily-standup page from a reMarkable tablet:

<transcription>
{content}
</transcription>

{previous_block}

Parse the transcription and produce structured output:

1. date: The date the page documents, in YYYY-MM-DD format. If the page has a date header like "May 19" with no year, assume the current year. Today is {today} — use that to anchor the year.

2. date_confidence: "high" if you found an explicit date in the content; "low" if you inferred or guessed.

3. todos: A merged TODO list combining carry-forwards from the previous daily note (if any) with new items from this transcription. Each item has:
   - text: the action item, phrased clearly. Preserve explicit deadlines ("Have SBC ready for demo on Fri"). Keep it concise — one line, no sub-bullets.
   - status: one of "Todo" (default for new items), "In Progress", "In Review", "Done", "Blocked". Preserve the status from the previous daily note unless the transcription clearly indicates a change.
   - age_days: integer.
     * For items carried over from the previous daily note: previous_age_days + ({today} - previous_date) in days.
     * For new items first appearing in this transcription: 0.

Carry-forward rules:
   - Carry forward every item from the previous daily note UNLESS the transcription says it's done (and even then, mark "Done" rather than dropping — the user removes Done items manually).
   - If a new item from the transcription is semantically the same as a previous one (same intent, paraphrased), keep the previous entry — preserve its age and status. Do NOT add a duplicate.
   - Only add a new item (with age 0) when it truly isn't represented in the previous list.

4. questions: Open questions, things to figure out, decisions pending. Preserve the question form. New questions only — do not carry forward.

5. notes: Everything else from the transcription — context, status updates, meeting notes, 1:1 notes, planning blurbs. Preserve markdown structure (bullets, indentation, arrows). Use → for arrows, ↳ for indented bullets. New notes only — do not carry forward.

If a section has nothing, return an empty list (todos/questions) or empty string (notes).
"""


def extract_sections(
    cfg: dict,
    ocr_text: str,
    previous_todos: list[TodoItem] | None = None,
    previous_date: datetime.date | None = None,
    today: datetime.date | None = None,
) -> DailySections | None:
    """Send OCR'd text to Claude with optional carry-forward context."""
    today = today or datetime.date.today()
    vcfg = cfg.get("vision", {})
    api_key = vcfg.get("api_key")
    if not api_key or api_key == "YOUR_ANTHROPIC_API_KEY":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("vision.api_key not configured and ANTHROPIC_API_KEY not set")
        return None
    model = cfg.get("daily_merge", {}).get("model", vcfg.get("model", DEFAULT_MODEL))
    client = anthropic.Anthropic(api_key=api_key)

    previous_block = _render_previous_for_prompt(previous_todos, previous_date)
    prompt = _PROMPT_TEMPLATE.format(
        today=today.isoformat(),
        content=ocr_text,
        previous_block=previous_block,
    )
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            output_format=DailySections,
        )
    except anthropic.APIStatusError as exc:
        log.error("Claude section extraction failed: %s", exc)
        return None
    except anthropic.APIConnectionError as exc:
        log.error("Claude section extraction connection failed: %s", exc)
        return None
    return resp.parsed_output


def _render_previous_for_prompt(
    previous_todos: list[TodoItem] | None, previous_date: datetime.date | None
) -> str:
    if not previous_todos or previous_date is None:
        return (
            "There is no previous daily note to carry items forward from. "
            "Treat every TODO from the transcription as new (age 0)."
        )
    lines = [
        f"The most recent previous daily note is dated {previous_date.isoformat()}. "
        "Its TODO table contains:",
        "",
        "| Thing | Status | Age |",
        "| --- | --- | --- |",
    ]
    for t in previous_todos:
        lines.append(f"| {t.text} | {t.status} | {t.age_days}d |")
    lines.append("")
    return "\n".join(lines)


def render_block(
    sections: DailySections, rm_hash: str, synced_at: str, source_label: str
) -> str:
    """Render the muninn-managed block (markers + sections) as Markdown."""
    lines = [
        f"<!-- muninn-daily-start rm_hash={rm_hash} synced={synced_at} -->",
    ]
    if sections.todos:
        lines.append("# TODO")
        lines.append("")
        lines.append("| Thing | Status | Age |")
        lines.append("| --- | --- | --- |")
        for t in sections.todos:
            text = t.text.replace("|", "\\|")
            lines.append(f"| {text} | {t.status} | {t.age_days}d |")
        lines.append("")
    if sections.questions:
        lines.append("# Questions")
        lines.append("")
        for q in sections.questions:
            lines.append(f"- {q}")
        lines.append("")
    if sections.notes.strip():
        lines.append(f"# Notes (from rM, synced {synced_at[:10]} — {source_label})")
        lines.append("")
        lines.append(sections.notes.rstrip())
        lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines)


def _find_existing_block(content: str) -> tuple[int, int, str] | None:
    """Locate the muninn-managed block in `content`."""
    m = START_MARKER_RE.search(content)
    if not m:
        return None
    end_pos = content.find(END_MARKER, m.end())
    if end_pos == -1:
        log.warning("Found muninn-daily-start marker without matching end marker")
        return None
    return m.start(), end_pos + len(END_MARKER), m.group(1)


def merge_into_file(
    target: Path,
    sections: DailySections,
    rm_hash: str,
    source_label: str,
    *,
    force: bool = False,
) -> Literal["created", "merged", "unchanged"]:
    """Insert or refresh the muninn-managed block in the daily note at `target`."""
    synced_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = render_block(sections, rm_hash, synced_at, source_label)

    if not target.exists():
        new_content = _new_daily_note(sections.date, block)
        obsidian.write_notebook_atomic(target, new_content)
        return "created"

    existing = target.read_text(encoding="utf-8")
    located = _find_existing_block(existing)
    if located is not None:
        start, end, cached_hash = located
        if not force and cached_hash == rm_hash:
            return "unchanged"
        merged = existing[:start] + block + existing[end:]
    else:
        sep = "" if existing.endswith("\n") else "\n"
        merged = f"{existing}{sep}\n{block}\n"

    obsidian.write_notebook_atomic(target, merged)
    return "merged"


def _new_daily_note(date_str: str, block: str) -> str:
    return (
        "---\n"
        "tags:\n"
        "- type/daily\n"
        "---\n"
        "\n"
        f"# {date_str}\n"
        "\n"
        f"{block}\n"
    )


def daily_target_path(cfg: dict, vaults: list[dict], date_str: str) -> Path:
    """Resolve the on-disk path for a given date's daily note."""
    dm = cfg.get("daily_merge", {})
    target_vault_name = dm.get("target_vault")
    target_dir = dm.get("target_dir")
    if not target_vault_name or not target_dir:
        raise ValueError(
            "[daily_merge].target_vault and [daily_merge].target_dir are required"
        )
    matching = [v for v in vaults if v.get("name") == target_vault_name]
    if not matching:
        raise ValueError(
            f"[daily_merge].target_vault {target_vault_name!r} not found in [[vaults]]"
        )
    vault = matching[0]
    return Path(vault["path"]).expanduser() / target_dir / f"{date_str}.md"


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_HEURISTIC_RE = re.compile(
    r"\b(" + "|".join(_MONTHS.keys()) + r")\s+(\d{1,2})\b",
    re.IGNORECASE,
)


def heuristic_date(ocr_text: str, today: datetime.date) -> datetime.date | None:
    """Quickly guess the page date from a 'Month DD' header without an LLM call.

    Used to find the right previous-daily-note for carry-forward context before
    we pay for a Claude extraction. Returns None when no date-like pattern is
    found. When the inferred date would be more than a week in the future,
    assumes the previous year.
    """
    m = _DATE_HEURISTIC_RE.search(ocr_text)
    if not m:
        return None
    month = _MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    try:
        candidate = datetime.date(today.year, month, day)
    except ValueError:
        return None
    if candidate > today + datetime.timedelta(days=7):
        candidate = datetime.date(today.year - 1, month, day)
    return candidate


def find_previous_daily_note(
    target_dir: Path, target_date: datetime.date
) -> tuple[Path, datetime.date] | None:
    """Return (path, date) of the most recent YYYY-MM-DD.md before `target_date`."""
    if not target_dir.exists():
        return None
    candidates: list[tuple[Path, datetime.date]] = []
    for f in target_dir.glob("*.md"):
        m = _DAILY_NAME_RE.match(f.name)
        if not m:
            continue
        try:
            d = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d < target_date:
            candidates.append((f, d))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]


def extract_current_muninn_todos(target_path: Path) -> list[TodoItem]:
    """Return TODOs from the muninn block of `target_path`, or [] if absent.

    Used for same-day re-merge: when sync runs twice on the same date (e.g.
    morning standup, then again after lunch), the second run should treat
    the morning's block — including any manual status edits the user made
    in Obsidian — as the carry-forward source rather than walking back to
    yesterday's table.
    """
    if not target_path.exists():
        return []
    try:
        content = target_path.read_text(encoding="utf-8")
    except OSError:
        return []
    block_info = _find_existing_block(content)
    if block_info is None:
        return []
    start, end, _ = block_info
    return extract_todo_table(content[start:end])


def resolve_carry_forward_source(
    target_dir: Path, target_date: datetime.date
) -> tuple[datetime.date | None, list[TodoItem]]:
    """Pick the best carry-forward source for a merge targeting `target_date`.

    Same-day re-merge → prefer the existing muninn block in target_date's
    own file (preserves manual status edits). Otherwise walk back to the
    most recent prior daily note with a non-empty TODO table. Returns
    `(None, [])` when no history exists at all.
    """
    target_path = target_dir / f"{target_date.isoformat()}.md"
    current = extract_current_muninn_todos(target_path)
    if current:
        return target_date, current
    state = find_previous_todo_state(target_dir, target_date)
    if state is not None:
        _, prev_date, prev_todos = state
        return prev_date, prev_todos
    return None, []


def carry_forward(prev_todos: list[TodoItem], days_elapsed: int) -> list[TodoItem]:
    """Bump each TODO's age by `days_elapsed`, preserving text and status."""
    return [
        TodoItem(text=t.text, status=t.status, age_days=t.age_days + days_elapsed)
        for t in prev_todos
    ]


def find_previous_todo_state(
    target_dir: Path, target_date: datetime.date, *, max_lookback_days: int = 90
) -> tuple[Path, datetime.date, list[TodoItem]] | None:
    """Walk back from `target_date` and return the first daily note with a non-empty TODO table.

    Most recent intermediate daily notes often skip the TODO table entirely
    (review-queue-only, runbook content, etc.), so carry-forward needs to look
    past them to the last note that actually had a list.
    """
    if not target_dir.exists():
        return None
    cutoff = target_date - datetime.timedelta(days=max_lookback_days)
    candidates: list[tuple[Path, datetime.date]] = []
    for f in target_dir.glob("*.md"):
        m = _DAILY_NAME_RE.match(f.name)
        if not m:
            continue
        try:
            d = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d < target_date and d >= cutoff:
            candidates.append((f, d))
    candidates.sort(key=lambda x: x[1], reverse=True)
    for path, d in candidates:
        try:
            todos = extract_todo_table(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if todos:
            return path, d, todos
    return None


def extract_todo_table(content: str) -> list[TodoItem]:
    """Parse the first `# TODO` (or ## TODO) markdown table out of `content`.

    Returns an empty list when no TODO table is found. Normalizes common
    status spellings (case-insensitive) to the canonical Literal values.
    """
    lines = content.split("\n")
    in_todo = False
    items: list[TodoItem] = []
    for line in lines:
        stripped = line.strip()
        if not in_todo:
            if re.match(r"^#+\s+TODO\s*$", stripped):
                in_todo = True
            continue
        # In TODO section.
        if stripped.startswith("#"):  # next header → section ended
            break
        if not stripped.startswith("|"):
            # Skip blank lines between header and table; non-table content ends it.
            if items:  # already saw rows, blank line probably ends the table
                if not stripped:
                    continue
                break
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) != 3:
            continue
        text, status, age = cells
        # Header row or separator row.
        if text.lower() == "thing":
            continue
        if all(c == "" or set(c) <= set("-: ") for c in cells):
            continue
        age_match = re.match(r"(\d+)", age)
        age_days = int(age_match.group(1)) if age_match else 0
        norm_status = _STATUS_NORM.get(status.lower().strip(), status.strip())
        if norm_status not in ("Todo", "In Progress", "In Review", "Done", "Blocked"):
            log.debug("Unknown TODO status %r — falling back to Todo", status)
            norm_status = "Todo"
        items.append(
            TodoItem(text=text, status=norm_status, age_days=age_days)
        )
    return items
