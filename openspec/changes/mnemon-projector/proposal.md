## Why

Across long, multi-agent, multi-session projects, mnemon only has an **append** path — never a **reduce**. Agents write notes (`memory_nodes`) and events (`memory_events`), but nothing folds those scattered writes into a canonical "current state." Convergence is therefore *lazy read-repair*: each agent boots, `memory-recall.py` injects the 15 most-recent/most-accessed notes, and the agent re-derives its own view. The system only converges once enough agents happen to re-read enough of each other's notes — the round-robin brute-force that motivated this change. There is no anti-entropy protocol and no authoritative reducer.

## What Changes

- Add a **deterministic projector**: a scheduled reducer that folds a project's `memory_events` (and structured note metadata) into a single canonical `PROJECT_STATE` read-model per project — active sessions/agents, repo→owner map, recently-touched files, latest checkpoint, and open work signals. Pure code, no LLM.
- Add a `memory_projections` table holding one materialized row per project. It is a **CQRS read-model**: disposable and fully rebuildable from the append-only event log at any time (the log stays the source of truth).
- Run the projector as a **profile-gated daemon container** (`mnemon-projector`), modeled on the existing `mnemon-flush` loop — reproject every N seconds, cheaply and incrementally.
- Surface the projection to agents at boot: `memory-recall.py` emits a `## Project State` section **first** (the reduced, canonical view), then falls back to the existing `## Prior Knowledge` raw-notes section for the long tail. Add a `get_project_state` MCP tool so running agents can pull the same projection on demand.
- **Non-goal (explicitly deferred to a future `mnemon-reflector` change):** semantic contradiction resolution, note supersession edges, and temporal validity of natural-language claims. Those require an LLM reduce and cannot be lattice-merged; this change ships only the deterministic, mechanically-mergeable layer. See `design.md` for the boundary.

## Capabilities

### New Capabilities

- `memory-projection`: The projector contract — a projection is a deterministic, rebuildable reduction of a project's append-only event log into one canonical current-state record; it MUST be safe to recompute from scratch, MUST never be the sole source of truth, and MUST be surfaced to agents ahead of raw notes at recall time. Covers the reducer's inputs, the read-model's freshness/rebuild guarantees, and the recall precedence rule.

### Modified Capabilities

<!-- None. mnemon has no existing capability spec, and this change adds a reduce
     path without altering how notes/events are written or how spark indexes. The
     new memory-projection capability fully covers the projector's behavior. -->

## Impact

- **Schema**: new migration `mnemon/migrations/004_memory_projections.sql` (idempotent, additive) creating `agents.memory_projections`. No change to `memory_nodes`/`memory_events`/`memory_links`/`memory_entities`.
- **Code**: new `agent_memory/projection.py` (the reducer) + a `project` CLI subcommand in `agent_memory/cli.py`; one new `get_project_state` tool in `server/mcp_server.py`.
- **Infra**: new `mnemon-projector` service in `docker-compose.yml` (profile `mnemon`, reuses `docker/mnemon.Dockerfile`); a `mnemon:project` Taskfile target.
- **Boot path**: `tmux/{mac,linux,windows}/tmux-scripts/memory-recall.py` gains a project-state section ahead of the notes section (fail-open — a missing/stale projection never blocks spawn).
- **Read semantics**: agents boot from a reduced view instead of re-deriving one; recall load drops because the canonical state replaces re-reading the note tail.
