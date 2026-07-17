## 1. Schema

- [ ] 1.1 Add migration `mnemon/migrations/004_memory_projections.sql`: idempotent `CREATE TABLE IF NOT EXISTS agents.memory_projections (project TEXT PRIMARY KEY, state JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), source_event_count INTEGER NOT NULL DEFAULT 0, last_event_ts TIMESTAMPTZ)` plus an index on `updated_at`.
- [ ] 1.2 Run the migration against the dev/live DB via `task mnemon:migrate` and confirm the table exists (`\d agents.memory_projections`).

## 2. Reducer

- [ ] 2.1 Inspect real `memory_events` rows to confirm the payload keys carried by `session_start`, `session_end`, `commit`, `file_write`, and `checkpoint` before pinning the reducer contract.
- [ ] 2.2 Create `mnemon/agent_memory/projection.py` with a pure `reduce(events: list[dict]) -> dict` that folds a project's events into `{active_sessions, repo_owners, touched_files, latest_checkpoint, event_counts}` — last-writer-wins on `repo_owners` by timestamp, set-union on `touched_files`, max-by-timestamp on `latest_checkpoint`; ignore unknown `event_type`.
- [ ] 2.3 Add `project_all(store/pool) -> None` and `project_one(project) -> None` that read a project's events, call `reduce`, and UPSERT the row (`INSERT ... ON CONFLICT (project) DO UPDATE`) with `updated_at = now()`, `source_event_count`, and `last_event_ts`.
- [ ] 2.4 Add unit tests in `mnemon/tests/test_projection.py`: idempotent recompute, unknown-event-type ignored, ownership last-writer-wins, latest-checkpoint selection, and rebuild-after-truncation equivalence.

## 3. CLI

- [ ] 3.1 Add a `project` subcommand to `mnemon/agent_memory/cli.py` (`--project NAME` or `--all`) that invokes the reducer and prints per-project row counts written.
- [ ] 3.2 Manually backfill once (`... cli project --all`) and verify rows land for the active projects via `cli inspect`/a psql query.

## 4. MCP tool

- [ ] 4.1 Add a read-only `get_project_state(project)` tool to `mnemon/agent_memory/server/mcp_server.py` returning `{state, updated_at, stale: bool}`; unknown project returns an empty result (no error).
- [ ] 4.2 Smoke-test the tool against `mnemon-mcp` (SSE) and confirm it returns the backfilled projection.

## 5. Projector daemon

- [ ] 5.1 Add the `mnemon-projector` service to `docker-compose.yml` (profile `mnemon`, `docker/mnemon.Dockerfile`, `restart: always`, `command: while true; do uv run python -m agent_memory.cli project --all; sleep ${PROJECTOR_INTERVAL:-90}; done`), with the same `mem_limit`/`memswap_limit` treatment as `mnemon-flush`.
- [ ] 5.2 Add a `mnemon:project` Taskfile target and (optionally) a `PROJECTOR_INTERVAL` entry to `.env.example`.
- [ ] 5.3 Bring the container up under the `mnemon` profile and confirm it reprojects on the interval (logs show per-cycle writes; `updated_at` advances).

## 6. Recall integration

- [ ] 6.1 Update `tmux/mac/tmux-scripts/memory-recall.py` to read `agents.memory_projections` for the project and emit a `## Project State` section ahead of `## Prior Knowledge`, with an age label and a `⚠ stale` marker past the threshold; fail open (missing/unreachable → omit section, no error).
- [ ] 6.2 Port the same change to `tmux/linux/tmux-scripts/memory-recall.py` and `tmux/windows/tmux-scripts/memory-recall.py` (keep the three variants in parity).
- [ ] 6.3 Verify end-to-end: spawn an agent in a project with a projection and confirm the `## Project State` section appears first; spawn in a project with no projection and confirm identical-to-before notes-only output.

## 7. Verification & docs

- [ ] 7.1 Run `openspec validate mnemon-projector` and the mnemon test suite (`task mnemon:test`) green.
- [ ] 7.2 Record a memory-stack note (project `agents-nexus`) capturing the projector design, the deterministic/semantic boundary, and the deferred `mnemon-reflector` follow-up; link the touched files.
