"""Decision synthesizer — turns MR data into structured decision records.

'The installation's records would indicate that decision was made for good reason.'
— 127 Guilty Spark
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.config import SparkConfig

logger = logging.getLogger("spark.synthesizer")

_NOTES_MAX_CHARS = 3000

_PROMPT_TEMPLATE = """\
You are a technical documentation assistant. Given a GitLab merge request, produce a concise decision record.

Output ONLY the following four sections with no preamble, no extra text, and no additional headers:

## What changed
<1-3 sentences describing what was implemented or modified>

## Why
<The motivation — what problem was being solved, what requirement or incident drove this>

## Alternatives considered
<What else was discussed or rejected, or "None identified" if nothing was discussed>

## Impact
<What this change affects — services, APIs, data, customers — or "None identified" if unclear>

---
Merge Request: {title}
Repo: {repo_name} (Team: {team})
Author: {author}
Date: {merged_at}

Description:
{description}

Discussion:
{notes}
"""


def _truncate_to_word_boundary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        truncated = truncated[:last_space]
    return truncated + "\n... [truncated]"


def synthesize_decision(
    mr_title: str,
    mr_description: str,
    mr_notes: list[dict],
    repo_name: str,
    team: str,
    merged_at: str,
    author: str,
    config: SparkConfig,
) -> str:
    """Synthesize a structured decision record from MR data.

    Returns a markdown string with four sections (What changed, Why,
    Alternatives considered, Impact). Returns "" if decisions_enabled is
    False, if the LLM call fails, or if the response is empty.
    Never raises.
    """
    if not config.decisions_enabled:
        return ""

    notes_text = ""
    if mr_notes:
        raw_notes = "\n".join(
            f"@{n['author']}: {n['body']}" for n in mr_notes
        )
        notes_text = _truncate_to_word_boundary(raw_notes, _NOTES_MAX_CHARS)

    prompt = _PROMPT_TEMPLATE.format(
        title=mr_title,
        repo_name=repo_name,
        team=team,
        author=author,
        merged_at=merged_at[:10] if merged_at else "",
        description=mr_description or "(no description provided)",
        notes=notes_text or "(no discussion)",
    )

    try:
        import litellm
        response = litellm.completion(
            model=config.chat_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
        )
        content = response.choices[0].message.content or ""
        content = content.strip()
        if not content:
            logger.warning(f"[synthesizer] Empty response for {repo_name} — {mr_title!r}")
            return ""
        return content
    except Exception as e:
        logger.warning(f"[synthesizer] LLM call failed for {repo_name} — {mr_title!r}: {e}")
        return ""
