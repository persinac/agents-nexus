## 1. Metadata module

- [x] 1.1 Create `spark/src/spark/indexer/metadata.py` with `InstallationMeta` dataclass (`indexed_at: str`, `last_remote_ts: int`, `clone_url: str`) and `load_metadata(path: Path) -> dict[str, InstallationMeta]` that returns `{}` when the file is missing, parses JSON, and logs a warning + returns `{}` on parse failure.
- [x] 1.2 Add `save_metadata(path: Path, meta: dict[str, InstallationMeta]) -> None` that writes to `<path>.tmp` then atomically renames to `<path>`.
- [x] 1.3 Expose `metadata_path` on `SparkConfig` (in `spark/src/spark/config.py`) derived as `<index_path>/installations.json`.

## 2. Git delta probe

- [x] 2.1 Add `_git_remote_ts(repo_dir: Path) -> int | None` helper to `spark/src/spark/indexer/builder.py` that runs `git -C <dir> fetch --filter=blob:none --quiet origin` then `git -C <dir> log -1 --format=%ct FETCH_HEAD`; returns the int timestamp or `None` on any failure.
- [x] 2.2 On filtered-fetch failure (non-zero exit AND stderr contains "filter" or "shallow"), retry once with plain `git fetch --quiet origin` before returning `None`.
- [x] 2.3 Add `_git_clone_url(repo_dir: Path) -> str | None` helper that runs `git -C <dir> remote get-url origin` and returns the URL or `None`.

## 3. Sync orchestrator

- [x] 3.1 Add `sync_installations(config: SparkConfig) -> dict[str, int]` to `spark/src/spark/indexer/builder.py` that loads metadata, walks `_discover_installations()`, and computes the four sets (`new`, `removed`, `up-to-date`, `changed`, `fetch-failed`) per the spec.
- [x] 3.2 For each `new` or `changed` installation, call existing `activate_installation()` inside a try/except. On success, update the in-memory metadata entry; on failure, log and preserve the prior entry.
- [x] 3.3 For each `removed` installation, call `table.delete(f"installation_path = '{rel_path}'")` (note: real column is `installation_path`, not `Path`) on the LanceDB table inside a try/except. On success, remove the metadata entry; on failure, log and keep the entry for retry next run.
- [x] 3.4 Save metadata once at the end via `save_metadata()`. Emit a single INFO summary line with classification counts, elapsed time, and total chunks written.
- [x] 3.5 Return a dict of `{classification: count}` so the CLI can produce a clean exit message.

## 4. CLI command

- [x] 4.1 Register `sync` as a new click command in `spark/src/spark/cli.py`, mirroring the `activate` pattern around line 61.
- [x] 4.2 Add a `--dry-run` flag that runs through classification but skips activation, prune, and metadata write — log what would happen and exit 0.
- [x] 4.3 Print the summary dict to stdout when sync completes successfully; exit non-zero only if the orchestrator itself raises (per-installation failures are soft).

## 5. Pipeline script

- [x] 5.1 Update `spark/scripts/spark-pipeline.sh` to call `"$SPARK_BIN" sync` instead of `"$SPARK_BIN" reclaim`.
- [x] 5.2 Update the script's header comment to reflect the new step ordering and note that `reclaim` is now reserved for schema migrations.

## 6. Verification

- [x] 6.1 Run `spark sync` against the existing index manually inside the `nexus-spark` container. Scoped to `-p agents-nexus` (a 437-repo full first-sync would take 4h, deferred to nightly cron). Result: `new=1`, 409 chunks indexed in 534s, `installations.json` written.
- [x] 6.2 Run `spark sync` a second time immediately after the first. Result: `changed=1` (because first run stored `last_remote_ts=0` due to the original `git fetch`-from-container bug — see decision 1 in design.md). Metadata correctly updated to real HEAD ts `1779414048`. **Apply revealed a real bug:** the container has no git credentials, so the spec was revised to use local HEAD instead of `git fetch FETCH_HEAD`. Spec + design updated to match.
- [x] 6.3 Verified via cheaper path: rolled metadata's `last_remote_ts` back to `1`, ran `spark sync -p agents-nexus --dry-run`. Classification: `changed=1` (HEAD ts 1 → 1779414048). Then restored real timestamp. Skipped the real empty-commit test to avoid polluting git history — same code path as 6.2 anyway.
- [x] 6.4 Verified via cheaper path: injected phantom `phantom-test` entry into `installations.json`, ran `spark sync -p phantom-test`. Classification: `removed=1`, phantom entry pruned from metadata. (Did not `rm -rf` a real installation since `/repos` is mounted read-only in the container.)
- [x] 6.5 `spark status` shows index intact (437 installations, 39,612 chunks — up from 39,604 because the second agents-nexus index wrote 409 vs 401 chunks). `spark reclaim --help` and `-p` flag still parse correctly. Reclaim's code path was not touched; full re-reclaim not re-run to avoid burning another 4h.
- [x] 6.6 Confirmed Command Center `/api/system/timers` includes `com.agents-nexus.guilty-spark.nightly` with the sidecar description, sensible `nextRun`/`leftUntil`, and `result=success`. Description in `launchd/descriptions.json` updated from "full Spark index rebuild" to "incremental Spark re-index (sync)" to match new pipeline behavior.

## 7. Docs

- [x] 7.1 Add a short section to `spark/README.md` (or create one if missing) documenting `spark sync`, the classification model, and when to fall back to `spark reclaim`.
- [x] 7.2 Add a one-line note to `docs/linux-mac-parity.md` about `spark sync` being the new nightly entrypoint, so the eventual Linux systemd port targets the right command.
