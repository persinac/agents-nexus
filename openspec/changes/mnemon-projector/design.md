## Context

mnemon today is append-only in practice. `create_note` inserts into `agents.memory_nodes` with `ON CONFLICT (id) DO NOTHING` (only `access_count`/`embedding` are ever mutated), and `log_event` appends to `agents.memory_events`. Reads are lazy read-repair: `tmux/*/tmux-scripts/memory-recall.py` runs at spawn (raw psycopg, before Claude starts) and injects a `## Prior Knowledge` block = the 15 notes ordered by `COALESCE(last_accessed, created_at) DESC, access_count DESC`. Running agents can pull more via the `search_similar` / `query_notes` / `query_entity` MCP tools. Nothing reduces these writes into a canonical state, so convergence only happens when enough agents re-read enough notes.

Constraints that shape this design:
- The host is RAM-tight (recent swap-exhaustion freeze; langfuse/ollama capped). A new always-on component must be cheap — no embeddings, no LLM in the hot loop.
- Host systemd units have proven fragile here (spark-nightly silently 78 days stale on a PATH break). Scheduled work should live in a container, not a host timer.
- There are two read entry points with different lifecycles: `memory-recall.py` (pre-Claude, plain psycopg, fail-open) and the SSE MCP server (`mnemon-mcp`). Both must be able to read the projection.
- Writes can originate on multiple hosts, but all land in one shared Postgres (`agents` schema).

## Goals / Non-Goals

**Goals:**
- Add an explicit **reduce** step: a deterministic projector that materializes one canonical `PROJECT_STATE` per project from the append-only event log.
- Make the projection a disposable CQRS read-model — rebuildable from `memory_events` at any time; the log stays authoritative.
- Surface the projection ahead of raw notes at boot, and on demand via MCP, so agents read a converged view instead of re-deriving one.
- Keep it cheap (pure-code fold, tunable cadence) and low-risk (strictly additive; fail-open).

**Non-Goals:**
- **Semantic reconciliation of natural-language claims** ("we chose X" vs later "we chose not-X"). That cannot be lattice-merged and needs an LLM reduce + supersession edges + temporal validity — deferred to a future `mnemon-reflector` change. This change ships only the mechanically-mergeable layer.
- Changing how notes/events are written, the embedding path, or spark indexing.
- Incremental/streaming projection. Phase 1 does full recompute per project (see Decisions).
- A UI. The projection is consumed by `memory-recall.py` and the MCP tool; visualizing it can come later.

## Decisions

### 1. Read-model in a new Postgres table, not a materialized view, Redis, or a file
`agents.memory_projections` — one row per `(project)`, holding a JSONB `state` blob plus `updated_at`, `source_event_count`, and `last_event_ts` watermarks. Rationale: both read entry points already speak Postgres; the fold logic (reducing JSONB event payloads into ownership maps / file sets / latest-checkpoint) is application logic a PG matview can't express cleanly; and the read-model must survive restarts to be canonical.
- *Alternatives:* **Redis L2 tuplespace** (already exists) — rejected: L2 is the ephemeral tier; the projection is durable canonical state. **Flat file per project** — rejected: no concurrent-safe multi-writer, no cross-host sharing. **Postgres MATERIALIZED VIEW** — rejected: the reduction isn't expressible as one SQL query and needs versioned app logic.

### 2. Deterministic reducer only — no LLM (the mechanical/semantic split)
The projector is a pure function `reduce(events) -> state`. It handles only mechanically-mergeable facts: session/agent liveness, repo→owner (last-writer-wins by timestamp), touched-file sets (union), latest checkpoint (max by timestamp), event counts. Rationale: keeps it cheap and RAM-safe, makes it unit-testable as a pure function, and draws a clean boundary the reflector can build on later.
- *Alternative:* fold semantic note-contradictions now — rejected: not deterministic, not lattice-mergeable, and would drag an LLM into the hot loop on a RAM-tight box.

### 3. Full recompute per project, not incremental deltas (phase 1)
Each cycle reads the project's events and folds from scratch. Rationale: per-project event volume is modest; a full fold is idempotent and eliminates incremental-state drift bugs. The `last_event_ts` / `source_event_count` watermarks are stored now so a later switch to incremental needs no schema change.
- *Alternative:* incremental fold — deferred as a premature optimization.

### 4. Projector as a profile-gated daemon container, modeled on `mnemon-flush`
New `mnemon-projector` service in `docker-compose.yml` (profile `mnemon`, reusing `docker/mnemon.Dockerfile`, `restart: always`), running `while true; do uv run python -m agent_memory.cli project --all; sleep ${INTERVAL}; done`. Rationale: mirrors the proven flush loop; containers dodge the host-systemd PATH fragility that silently broke spark-nightly; `memswap_limit` caps apply like the other mnemon containers.
- *Alternatives:* **host systemd timer** — rejected (fragility above). **Fold into the flush loop** — rejected: keeps flush single-responsibility and lets projector cadence differ.

### 5. Recall precedence: projection first, notes second, fail-open
`memory-recall.py` reads `memory_projections` for the project and emits a `## Project State` section *before* the existing `## Prior Knowledge` notes section. If the row is missing, the store is unreachable, or the query errors, it silently omits the section — today's notes path is unchanged. Rationale: strictly additive; a broken projector can never regress spawn behavior. The MCP `get_project_state` tool exposes the same row to running agents.

### 6. Staleness is surfaced, not hidden
The projection row carries `updated_at`. Recall labels the section with its age ("_as of 3m ago_"). If older than a threshold (projector likely down), it still shows the state but marks it `⚠ stale` — a stale canonical view beats none, and the label prevents silent over-trust.

## Risks / Trade-offs

- **Projection disagrees with a raw note** → The `## Project State` section is explicitly labeled *derived, as of T*; notes stay visible below it. Genuine contradiction resolution is the reflector's job (phase 2), not the projector's.
- **Projector down → stale state trusted silently** → `updated_at` + age label + stale marker at recall; the section is additive so the worst case degrades to exactly today's behavior.
- **Event-payload schema drift breaks the fold** → reducer is defensive: unknown `event_type` values are ignored, missing payload keys default; full recompute means a fix reprocesses the whole history cleanly; unit tests pin the payload shapes actually consumed.
- **RAM pressure** → no embeddings/LLM; a cycle is a couple of indexed `memory_events` queries + an in-memory dict fold; cadence is tunable via env; the container gets the same `mem_limit`/`memswap_limit` treatment as `mnemon-flush`.
- **Concurrent projectors (multi-host)** → the fold is deterministic over shared Postgres, so two projectors converge to the same row value; last-writer-wins on a single row is safe because the written value is identical (idempotent).

## Migration Plan

1. Ship migration `004_memory_projections.sql` (idempotent, additive — `CREATE TABLE IF NOT EXISTS`). Safe to run ahead of any code; the table is inert until written.
2. Land `agent_memory/projection.py` + the `project` CLI subcommand + unit tests. Backfill once by running `cli project --all` manually; verify rows for active projects.
3. Add the `get_project_state` MCP tool (read-only; no behavior change to existing tools).
4. Add the `mnemon-projector` compose service + `mnemon:project` Taskfile target; bring it up under the `mnemon` profile.
5. **Last:** update `memory-recall.py` (all three OS variants) to emit the `## Project State` section. This is the only agent-visible change and it is fail-open.

**Rollback:** stop/remove the `mnemon-projector` container and revert the `memory-recall.py` section. The `memory_projections` table can be left in place (inert if unread) or dropped — no other component depends on it.

## Open Questions

- **Event set for phase 1.** Start with `session_start`, `session_end`, `commit`, `file_write`, `checkpoint`; treat high-volume `tool_use` as aggregate counts only. Confirm the payload keys each carries against real rows before pinning the reducer contract.
- **Cadence.** Proposed 90s (flush is 120s). Tune against observed projector cost once live.
- **Note-derived state.** Phase 1 leans events-only plus note titles for "latest checkpoint." Whether to fold structured note `attributes` (e.g. a `decision` note's status) into the projection, or leave all note semantics to the reflector, is deferred — lean events-only for now.
