## Context

Spark today spans 3 serving surfaces, 3 indexes, and 2 codebases (see proposal). The live
local MCP (`/usr/local/bin/spark` → `guilty-spark`) serves a complete 768d nomic index; the
container serves 384d fastembed; the intended host-native 384d build stalled on an OOM (now
fixed); and a validated Bedrock 1024d index exists but is unused. Config resolves through three
layers (`.env` loaded by `config.load()` > `config.yaml` > wrapper `cd`), which silently
redirected the index path during investigation. Six agents query Spark continuously, so the
cutover must not drop search availability.

Constraints:
- Vector spaces are not portable: index-time and query-time embedder/dims MUST match.
- The `/usr/local/bin/spark` wrapper is root-owned (sudo to change).
- `guilty-spark` is a no-remote local checkout with its own uncommitted WIP — archive, don't delete.
- The OOM fix (`b750fb6`) and the bedrock embedder + registry tooling already live on `main`.

## Goals / Non-Goals

**Goals:**
- One canonical codebase (`agents-nexus/spark`) served on every surface.
- One canonical index built + queried with **Bedrock Titan v2, 1024d**.
- All serving surfaces (local wrapper, container, nightly) resolve to that one index.
- No search downtime during cutover (build-alongside → verify → atomic repoint).
- Config layering made obvious and consistent.

**Non-Goals:**
- Setting up a scoped IAM key (deferred hardening — SSO is accepted near-term per owner decision).
- Changing `spark-indexing`'s discovery/sync/prune behavior (unchanged).
- Re-architecting the container's webhook/MR-review path beyond pointing it at the new index.
- Deleting `guilty-spark` (archived as fallback).

## Decisions

**D1 — Embedder = Bedrock Titan v2 (1024d), index- and query-time.**
Chosen for best recall + ~22-min full builds (validated this session) over fastembed (free/local,
lower recall) and legacy nomic. Rationale: build speed + quality. Alternative (fastembed 384d, zero
query-time creds) was the migration's original target but is slower and lower-recall; kept as an
opt-in fallback via `SPARK_EMBEDDER`.

**D2 — Credentials: SSO now, IAM key later (explicit owner decision).**
Query-time embedding requires AWS Bedrock access. We use existing SSO creds and *accept* that
query-time embedding may fail when the SSO token rotates (~8–12h); this does not gate the build.
The preflight + `BedrockAuthError` fast-abort (already shipped) make such failures loud, not silent.
Scoped `bedrock:InvokeModel` IAM key is the documented follow-up. Alternative (block on IAM key)
rejected by owner — they want to test now.

**D3 — Single codebase via the wrapper, not code-sync.**
Repoint `/usr/local/bin/spark` to the canonical `agents-nexus/spark` so the local MCP, bare CLI,
and nightly all run one codebase. Alternative (copy/merge canonical into `guilty-spark`) rejected —
perpetuates two diverging trees. `guilty-spark` is archived as fallback.

**D4 — Config single-source.**
The canonical `agents-nexus/spark/.env` (currently `SPARK_EMBEDDER=fastembed`,
`SPARK_INDEX_PATH=~/.spark-index/the-index`) becomes the one place that sets embedder + index +
installations paths, set to the new bedrock index. `config.yaml` defaults are aligned so they're
not misleading. The container sets the same via compose env.

**D5 — Build-alongside cutover.**
Build the new bedrock index at a fresh path while the live 768d index keeps serving, verify parity,
then flip all surfaces in one step and relaunch MCPs. Avoids any window where a surface queries a
mismatched-dim index.

## Risks / Trade-offs

- **SSO rotation breaks query-time search** → Accepted near-term (D2); failures are loud
  (preflight/`BedrockAuthError`); IAM-key follow-up tracked.
- **Dim mismatch mid-cutover (1024d index vs 768/384 query embedder)** → Build-alongside + atomic
  repoint of *both* index path and embedder together; never repoint one without the other.
- **Container needs AWS creds** → Inject via env/credentials mount; if absent, container query-time
  embedding fails loudly (acceptable per D2). Webhook/MR-review path must be re-tested post-cutover.
- **Root-owned wrapper edit** → Back up the 3-line script; hand owner a single reversible sudo line.
- **Nightly `spark sync` first run after cutover is a full reclaim** (no metadata at new path) →
  expected; ~22 min via bedrock; runs off-peak.
- **`guilty-spark` WIP stranded** → Archived intact on disk; recoverable.

## Migration Plan

- **P0 (done)** — Decide embedder (bedrock 1024d) + creds posture (SSO now).
- **P1 — Build alongside.** `spark reclaim` with `SPARK_EMBEDDER=bedrock` → fresh canonical index
  path (reuse/keep `~/.spark-index/bedrock-full` or a new `~/.spark-index/the-index-bedrock`).
  Live 768d index untouched. (Largely already built this session.)
- **P2 — Verify parity.** Counts/dims, semantic `query` spot-checks, `query_registry`,
  `installation_summary` against the new index.
- **P3 — Atomic cutover.** Set canonical `.env` (embedder=bedrock + new index path); repoint
  `/usr/local/bin/spark` (sudo) to `agents-nexus/spark`; update `docker-compose.work.yml` env
  (embedder + index + AWS creds) and recreate `nexus-spark`; relaunch local MCPs.
- **P4 — Retire legacy.** Archive `guilty-spark` checkout; drop the old 768d + partial 384d indexes
  after a soak period; remove dead config layers.
- **P5 — Re-wire nightly.** Confirm `spark sync` (canonical) targets the new index and stays fresh.

**Rollback:** revert the 3-line wrapper + `.env` + compose env to the previous values and relaunch;
the old 768d `guilty-spark` index is retained until the soak period passes.

## Open Questions

- Final canonical index path: reuse `~/.spark-index/bedrock-full` vs a stable name like
  `~/.spark-index/the-index` (rebuilt at 1024d). Leaning toward a stable name + symlink.
- Does the container have a usable AWS credential source (mount `~/.aws` vs env)? Verify before P3.
- Soak duration before deleting the legacy 768d index.
