## Why

A full `spark reclaim` across the 437 indexed installations now takes ~4 hours (proven empirically tonight: 39,604 chunks × ~2.8 chunks/sec embedding throughput against Ollama `nomic-embed-text`). The nightly cron at 02:00 currently runs `reclaim` unconditionally — that's a 4 h burn every night even though typically fewer than a dozen repos have meaningful changes in any 24 h window. The result is a lot of redundant embedding work, a long lockout where the index is being rebuilt, and a real disincentive against ever running the pipeline outside the nightly slot.

## What Changes

- Add a new `spark sync` CLI command that re-indexes only the installations whose origin/HEAD has advanced since their last index.
- Persist per-installation indexing metadata (`indexed_at`, `last_remote_ts`, `clone_url`) in a small JSON file at `<index_path>/installations.json`, written atomically.
- Detect new installations (present in `/repos`, absent from metadata) and full-index them on first sight.
- Detect removed installations (in metadata, absent from `/repos`) and prune their rows from LanceDB by `Path` filter.
- Update `spark/scripts/spark-pipeline.sh` to call `spark sync` instead of `spark reclaim`. **BREAKING** for the nightly cron's observable behavior — `reclaim` stays as an escape hatch for schema migrations, but is no longer the default cadence.
- No new dependencies; delta detection runs via `git fetch` + `git log` subprocess calls inside the existing `nexus-spark` container.

## Capabilities

### New Capabilities
- `spark-indexing`: covers how Spark discovers, indexes, and incrementally re-indexes installations — including the new `sync` command, the per-installation metadata store, and the contract for detecting deltas, new repos, and removed repos. The existing `reclaim` and `activate` commands are also documented here since they belong to the same capability surface.

### Modified Capabilities
<!-- No prior specs exist (openspec/specs/ is empty). All requirements land under the new capability above. -->

## Impact

- **Code**: `spark/src/spark/cli.py` (new `sync` command), `spark/src/spark/indexer/builder.py` (new `sync_installations` orchestrator + helpers for git probing and pruning), new `spark/src/spark/indexer/metadata.py` (load/save/atomic-write the installations.json), `spark/src/spark/config.py` (expose `metadata_path` derived from `index_path`).
- **Scripts**: `spark/scripts/spark-pipeline.sh` switches `reclaim` → `sync`. The launchd plist for `com.agents-nexus.guilty-spark.nightly` is unaffected (it just invokes the script).
- **Runtime**: nightly cron drops from ~4 h to seconds-to-minutes for typical days. Schema migrations still require a one-time `spark reclaim`.
- **Data**: a new file at `<index_path>/installations.json` (gitignored — lives inside the Docker volume). First post-deploy run will treat every installation as new and effectively perform a full reclaim, populating the metadata.
- **Dependencies**: none added. Relies on `git` already being available in the container (it is — Spark already uses it for the VCS-enriched chunks).
- **Backward-compat**: `spark reclaim` and `spark activate` continue to work exactly as before. `sync` is purely additive at the CLI surface.
