# Plan: Decision Synthesis Pipeline

> Turn merged MRs into searchable decision records — automatically, with no process burden on engineers.

## Problem

Business context and technical decisions are implicit in GitLab MR descriptions and discussion threads, then lost. There's no systematic process to capture them, and retroactive documentation never happens. We can't wait for humans to write things down.

## Approach

Extend the existing merge webhook pipeline: when an MR merges, synthesize a structured decision record from the MR description + discussion thread using an LLM, index it as a new `chunk_type = "decision"`, and optionally write it to Confluence. Add a retroactive backfill CLI command to cover historical MRs.

The human burden becomes light review, not authorship.

---

## Architecture Overview

```
MR merges
    → webhook fires (already works)
    → handle_reindex (already works)  ← existing
    → handle_decision_synthesis       ← NEW
          → fetch full MR description + notes (GitLab API)
          → LLM synthesis (LiteLLM chat model)
          → write decision chunk to LanceDB
          → optionally write to Confluence
```

New pieces:
- `src/spark/gitlab.py` — two new methods: `get_mr_full` and `get_mr_notes`
- `src/spark/synthesizer.py` — LLM prompt + structured output
- `src/spark/webhook.py` — new `handle_decision_synthesis` handler
- `src/spark/server/mcp_server.py` — new `search_decisions` MCP tool
- `src/spark/cli.py` — new `spark synthesize` CLI command for backfill

---

## Phase 1: Richer MR data from GitLab

The current `get_recent_merge_requests` fetches title, 200-char description, merged_at, and author. That's too thin for synthesis.

**Changes to `gitlab.py`:**

Add `get_mr_full(project_path, mr_iid)`:
- Returns full description (no truncation)
- Returns diff stats (files changed, insertions, deletions) for context
- Returns labels, milestone

Add `get_mr_notes(project_path, mr_iid)`:
- Fetches system-filtered notes (exclude system events, keep human comments)
- Returns list of `{author, body, created_at}` — the actual discussion
- This is where "why did we do it this way?" lives

Both methods already fit the existing `GitLabClient` pattern with rate limiting.

**Note:** The webhook `MergeRequestEvent` already has `mr_iid` and `gitlab_path` — no schema changes needed.

---

## Phase 2: Decision synthesizer

New file: `src/spark/synthesizer.py`

```python
def synthesize_decision(
    mr_title: str,
    mr_description: str,
    mr_notes: list[dict],
    repo_name: str,
    team: str,
    merged_at: str,
    author: str,
    chat_model: str,
) -> str:
    """Call LLM to produce a structured decision record from MR data."""
```

The synthesizer assembles a prompt from:
- MR title, full description
- Discussion thread (human comments only, concatenated)
- Repo + team context

And asks the LLM to extract:

```markdown
# Decision: <title>

**Repo:** <repo> | **Team:** <team> | **Date:** <merged_at> | **Author:** <author>

## What changed
<1-3 sentences on what was implemented or modified>

## Why
<The motivation — problem being solved, requirement, incident, product direction>

## Alternatives considered
<What else was discussed or rejected, if anything>

## Impact
<What this affects — services, APIs, data, customers>
```

Use LiteLLM with a chat model (configurable — default `ollama/llama3.2` for local, or any API model). LiteLLM is already a dependency. If synthesis fails, log and skip — never block the merge pipeline.

**New config fields in `config.yaml`:**
```yaml
chat_model: ollama/llama3.2   # model for decision synthesis
decisions_enabled: true        # toggle synthesis on/off
```

---

## Phase 3: Webhook handler + indexing

**New `Chunk` fields:**
```python
decision_date: str = ""    # ISO date of MR merge
decision_author: str = ""  # MR author username
mr_url: str = ""           # link back to the GitLab MR
```

These are sparse — only populated on `chunk_type = "decision"` chunks. Existing chunks are unaffected (LanceDB handles sparse columns fine with default empty strings).

**New handler in `webhook.py`:**

```python
def handle_decision_synthesis(event: MergeRequestEvent, config: SparkConfig) -> None:
    """Synthesize a decision record from a merged MR and index it."""
```

- Runs alongside `handle_reindex` on the `merge` action
- Fetches full MR data + notes via `GitLabClient`
- Calls `synthesize_decision()`
- Upserts a decision chunk into LanceDB
- Logs success/failure; never raises

Register in `create_default_dispatcher`:
```python
dispatcher.on("merge", handle_reindex)
dispatcher.on("merge", handle_decision_synthesis)   # NEW
```

---

## Phase 4: New MCP tool

Add `search_decisions` to `mcp_server.py`:

```python
@mcp.tool()
def search_decisions(
    query: str,
    team: str | None = None,
    top_k: int = 10,
) -> str:
    """Search synthesized decision records across all repos.

    Optimized for 'why' questions: 'why did we switch to X?',
    'what was the decision around Y?', 'what alternatives were considered for Z?'
    """
```

Filters `chunk_type = "decision"`. Returns the full synthesized content including the
"why" and "alternatives considered" sections. This is the thing that makes the
business context actually queryable.

Update MCP server instructions string to mention this tool.

---

## Phase 5: Retroactive backfill CLI

New command: `spark synthesize`

```
spark synthesize --all --days 180        # all repos, last 6 months
spark synthesize --repo svc-chatbot      # single repo, all history
spark synthesize --team "Platform - Infrastructure" --days 90
```

Implementation:
1. For each target repo, call `get_recent_merge_requests` with a high limit
2. For each MR, call `get_mr_full` + `get_mr_notes`
3. Call `synthesize_decision()`
4. Upsert decision chunk
5. Progress bar via `rich` (already used in CLI)

This is a one-time operation but re-runnable (upsert is idempotent by `id`).
The chunk `id` format: `{installation}::decision::{mr_iid}`.

---

## What this does NOT do (scope boundary)

- **Confluence write**: Left for a follow-on. The index is the primary store; Confluence can be a sync target later once the content pipeline is validated.
- **Slack/Linear**: Different connectors, different plan.
- **Automatic quality review of synthesized content**: LLM output goes straight in. Accuracy improves with better MR descriptions over time (virtuous cycle).

---

## Implementation order

1. Phase 1 — GitLab API enrichment (no LLM, no schema changes, lowest risk)
2. Phase 2 — Synthesizer (pure function, easy to test in isolation)
3. Phase 3 — Chunk schema extension + webhook handler (touches LanceDB schema)
4. Phase 4 — MCP tool (read-only, trivial once chunks exist)
5. Phase 5 — Backfill CLI (wraps everything above)

Each phase is independently mergeable and testable.
