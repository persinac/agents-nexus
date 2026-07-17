## Context

Spark indexes ~437 git installations into a LanceDB vector store at `/app/data/the-index` inside the `nexus-spark` Docker container. Installations are discovered by walking `installations_path` (mounted from the host at `/repos`). Today the nightly `spark-pipeline.sh` runs `spark reclaim`, which re-discovers every installation, re-chunks every file, re-embeds every chunk via Ollama `nomic-embed-text`, and rewrites the LanceDB table from scratch. On this work laptop a full reclaim measured 14,586 seconds (~4 h) tonight — ~2.8 chunks/sec sustained against Ollama. The bottleneck is embedding, not LanceDB.

Each chunk row already carries a `Path` column (the installation's relative path). `_discover_installations()` and `activate_installation()` are existing helpers in `spark/src/spark/indexer/builder.py` — they support single-repo re-indexing today, but nothing currently uses them in the nightly path.

Stakeholders: the nightly cron (`com.agents-nexus.guilty-spark.nightly`, fires 02:00) and the MCP search clients that hit `/app/data/the-index`. Both can tolerate the index being momentarily stale; neither can tolerate a 4 h lockout where chunks for active repos are missing or being rewritten.

Constraints:
- Must run inside the existing container — uv venv at `/app/.venv`, no host filesystem assumptions beyond the `/repos` mount.
- No new Python dependencies. Git fetch/log via subprocess.
- LanceDB delete-by-filter needs to be hit through the existing client (it supports `table.delete("Path = '...'")`).
- `git fetch --filter=blob:none` requires partial-clone support on the remote. GitHub and GitLab both support it, but a remote that doesn't (rare on-prem older servers) should not break the sync.

## Goals / Non-Goals

**Goals:**
- Reduce nightly indexing time from ~4 h to seconds-to-minutes on a typical day (when only a handful of repos changed).
- Detect three classes of installation drift each run: changed (HEAD moved), new (added to `/repos`), and removed (gone from `/repos`).
- Preserve existing CLI ergonomics: `spark reclaim` keeps working for schema migrations and disaster recovery; `spark activate <rel_path>` still re-indexes one repo.
- Make the change observable: the sync run should log a per-installation outcome line (`changed`, `new`, `removed`, `up-to-date`, `fetch-failed`) so the `nightly-spark.log` is informative without `-v`.

**Non-Goals:**
- Replacing Ollama or `nomic-embed-text` (separate decision).
- Replacing LanceDB with TurboVec or anything else.
- Linux systemd-side parity for the new pipeline script (the nightly cron still runs on Mac).
- Sub-file granular re-indexing (per-changed-file). We re-index the whole installation when its HEAD has moved — simpler, and embedding cost is already ~zero for unchanged files via the existing chunker dedup if present.
- Multi-host coordination: two machines running `spark sync` against the same index is undefined. The current setup has one Spark instance per machine.

## Decisions

### 1. Compare local HEAD timestamp; do NOT fetch from within the spark container

**Why (revised during apply):** The original design called for `git fetch` from inside the spark container. Discovered during verification that `/repos` is mounted **read-only** and the container has no ssh keys / git credentials — fetching is architecturally not its job. Spark is an indexer; keeping working trees current is the host's responsibility (the existing `nightly-repo-sync` cron already does this, see `linux-mac-parity.md`).

**Implementation:** `_git_head_ts(repo_dir)` runs `git -C <dir> log -1 --format=%ct HEAD` and returns the commit timestamp, or `None` on failure. Sync compares this against the previously-stored `last_remote_ts` (field name retained for metadata-file backward compat; semantically it's "HEAD timestamp observed at last index").

**Alternatives considered:**
- *Fetch from inside the container* — original design. Rejected after discovering credential/mount constraints.
- *Mount `/repos` read-write and seed credentials* — possible but couples the container to the host's git auth and broadens its blast radius. The host already owns repo state; let it stay that way.
- *Have `pipeline.sh` run `git fetch` per repo on the host before invoking `spark sync`* — viable, but the user has a separate `nightly-repo-sync` job that already handles this. Adding another fetch loop in pipeline.sh would be redundant. Documented as a follow-up if `nightly-repo-sync` ever stops running.

**Mitigation for the dependency on host-side fetching:** if `nightly-repo-sync` is broken, `spark sync` will keep classifying repos as `up-to-date` against stale HEADs and silently fail to re-index. The summary line's `up-to-date=N` count makes this visible by counting consecutive zero-changed runs, but it requires operator attention. Acceptable for v1; long-term, sync could optionally include a "HEAD is older than X days" warning.

### 2. Store metadata as a single atomic-write JSON file, not in LanceDB

**Why:** The metadata is small (~437 entries × a few hundred bytes = under 200 KB), changes on every sync, and is operationally simpler when it's editable/inspectable. A JSON file colocated with the LanceDB directory (`<index_path>/installations.json`) keeps a clean coupling: blow away the index, blow away the metadata, you're back to a clean reclaim. Atomic write via `tmp` + `rename` keeps it crash-safe.

**Alternatives considered:**
- *A second LanceDB table* — overkill for a flat key-value map; would require the same atomic-write care; harder to inspect.
- *SQLite* — adds a dependency surface and operational footprint for what is essentially a key-value file.

### 3. Detect deletions by set-difference, not by checking each metadata entry's existence

**Why:** One pass: `installations_now = set(_discover_installations())`, `installations_known = set(metadata.keys())`. New = `now - known`. Removed = `known - now`. The third bucket (changed) is the intersection where `remote_ts > indexed_at`. This pattern is faster to reason about than per-entry branches and naturally handles renames as "remove + new" (which we accept as the simpler behavior for v1).

**Trade-off:** A repo renamed on disk re-indexes from scratch. Acceptable — renames are rare and the failure mode is more work, not wrong results.

### 4. Activate one installation at a time, sequentially, in the same process

**Why:** `activate_installation()` already handles chunking + embedding + LanceDB writes for a single repo. Embedding throughput is bottlenecked by Ollama, not by Python concurrency, so parallel activations would mostly contend on the embedder. Sequential keeps the log readable and progress observable.

**Future:** if Ollama is replaced with a batchable embedder (FastEmbed, OpenAI), we'd revisit by activating in batches sharing a single embedding pool. Out of scope here.

### 5. Treat fetch failures as soft (log + skip), not fatal

**Why:** A flaky network shouldn't tank the whole sync. If `git fetch` fails for installation X, log the failure, don't update X's metadata, leave its existing index untouched, and continue. Next run picks it up.

**Trade-off:** A repo whose origin is permanently dead will log a failure every night. Acceptable — the noise is the signal that operator attention is needed.

## Risks / Trade-offs

- **First post-deploy sync looks like a full reclaim** → mitigation: that's expected and a one-time cost; subsequent runs are incremental. Document this in the proposal's Impact section (done) and in the command's `--help`.
- **`/repos` mount is incomplete on this machine** (e.g., 437 installations on indexed laptop vs. 50 on a second host) → mitigation: sync only operates on what's actually present locally. The metadata file is per-machine, so each Spark instance owns its own state. If we ever want a shared index, this needs to become a serialized lock or shared metadata — out of scope.
- **Renames re-index from scratch** → already discussed. Accepted v1 behavior.
- **A removed installation that comes back later** → it'll be re-detected as "new" on a later sync and full-indexed. Equivalent to a manual `spark activate`. Acceptable.
- **`installations.json` corrupted mid-write** → atomic `tmp + rename` makes this near-impossible. If somehow corrupted (disk-fill mid-write?), the next sync sees a bad JSON, falls back to "no known metadata" → behaves as if all repos are new, which is recoverable (slow but correct).
- **Concurrent `spark sync` invocations** → not protected. The container's launchd cron is the only thing that fires it. If a human runs `spark sync` while the cron also fires, two writers could race the metadata file. Mitigation: keep the operation single-host for now; add a flock if this becomes a real concern.
- **`activate_installation()` failure for one repo aborts the whole sync** → wrap each activation in try/except, log the failure, continue. Same pattern as fetch failures.

## Migration Plan

1. Ship the code (`sync` command, metadata module, helpers, pipeline.sh swap).
2. On first run: every installation looks "new" → effectively a full reclaim (~4 h). This is the cost of populating the metadata.
3. From the second run onward: only changed repos re-index (typically seconds-to-minutes).
4. `spark reclaim` is still available for schema migrations. After a schema bump, run `spark reclaim` once, then resume sync — sync's metadata file is unaffected by reclaim (it just adopts the new index state).

**Rollback:** revert `spark-pipeline.sh` to call `reclaim`. The metadata file becomes vestigial (next `sync` run, if re-enabled, would still find it). No data loss.

## Open Questions

- Should `spark sync` skip the `synthesize` step or call it itself? Current `spark-pipeline.sh` runs both. **Resolution:** keep `pipeline.sh` running both (`sync` then `synthesize --days N`); `sync` does not implicitly call synthesize. They're orthogonal concerns and pipeline.sh remains the orchestrator.
- Should we expose a `--dry-run` flag that lists what would be re-indexed without doing the work? **Tentative:** yes, as a tasks-level "nice to have" — useful for verifying behavior on first deploy.
- Should the metadata file include `chunk_count` for each installation so sync can detect index drift (e.g., a partial run that wrote half the chunks)? **Tentative:** out of scope for v1. If needed, a future `spark verify` command can compare metadata against the actual LanceDB row counts.
