## ADDED Requirements

### Requirement: Discover installations from filesystem

Spark SHALL discover indexable installations by walking the configured `installations_path` (mounted at `/repos` in the container) for git working trees, using the existing `_discover_installations()` helper. Each installation is identified by its relative path from `installations_path`.

#### Scenario: Walk discovers every git tree under the mount

- **WHEN** `installations_path` contains directories `acme/svc-foo` and `acme/svc-bar`, each with a `.git` directory
- **THEN** discovery returns both as `(absolute_path, relative_path)` pairs, sorted by relative path

#### Scenario: Non-git directories are skipped

- **WHEN** `installations_path` contains `acme/notes` with no `.git` directory
- **THEN** discovery does not include `acme/notes` in the result

---

### Requirement: Persist per-installation indexing metadata

Spark SHALL persist per-installation indexing metadata at `<index_path>/installations.json`. The file maps each installation's relative path to a record containing:

- `indexed_at`: ISO 8601 UTC timestamp of the last successful index for this installation.
- `last_remote_ts`: Unix epoch seconds of the origin/HEAD commit observed at that index.
- `clone_url`: the installation's `origin` remote URL captured at index time.

Writes MUST be atomic (write-to-temp then rename) to avoid corruption from interrupted runs.

#### Scenario: Successful index writes metadata

- **WHEN** `spark sync` completes a successful re-index of installation `acme/svc-foo`
- **THEN** `installations.json` contains an entry for `acme/svc-foo` with `indexed_at` set to the run start time, `last_remote_ts` set to the observed origin commit time, and `clone_url` set to the captured remote URL

#### Scenario: Atomic write survives interruption

- **WHEN** `spark sync` is killed mid-write of `installations.json`
- **THEN** the existing `installations.json` on disk is either the prior version or the new version, never a partial/corrupted file

#### Scenario: Missing metadata file is treated as empty

- **WHEN** `installations.json` does not exist (first ever run, or deleted by operator)
- **THEN** Spark proceeds as if every discovered installation is new

#### Scenario: Corrupted metadata file is treated as empty with a warning

- **WHEN** `installations.json` exists but does not parse as valid JSON
- **THEN** Spark logs a warning and proceeds as if every discovered installation is new, recreating the file on the next successful write

---

### Requirement: Detect upstream changes via local HEAD timestamp

Spark SHALL determine whether an installation has been updated since its last index by running `git -C <repo_dir> log -1 --format=%ct HEAD` and comparing the resulting Unix timestamp to the stored `last_remote_ts`. Spark SHALL NOT attempt to fetch from origin — the spark container has `/repos` mounted read-only and no git credentials; keeping working trees current is the host's responsibility (typically via a separate nightly repo-sync job).

#### Scenario: HEAD has advanced since last index

- **WHEN** an installation's stored `last_remote_ts` is 1700000000 and the local `git log -1 --format=%ct HEAD` returns 1700001000
- **THEN** Spark marks this installation as `changed` and includes it in the re-index queue

#### Scenario: HEAD unchanged since last index

- **WHEN** an installation's stored `last_remote_ts` equals the local `git log -1 --format=%ct HEAD`
- **THEN** Spark marks this installation as `up-to-date` and does not re-index it

#### Scenario: Local HEAD cannot be read

- **WHEN** `git log -1 HEAD` fails (broken or detached HEAD, corrupted .git directory, missing repo, etc.)
- **THEN** Spark logs the failure for that installation, leaves its metadata and index rows untouched (classified as `fetch-failed` in the summary for backward-compatible counts), and continues with the next installation

---

### Requirement: Categorize installations on each sync run

Spark SHALL classify every installation each run into exactly one of: `up-to-date`, `changed`, `new`, `removed`, or `fetch-failed`. The classification SHALL be:

- `new`: present on disk, absent from `installations.json`
- `removed`: present in `installations.json`, absent from disk
- `up-to-date`: present in both, `git log` timestamp equals stored `last_remote_ts`, fetch succeeded
- `changed`: present in both, `git log` timestamp greater than stored `last_remote_ts`, fetch succeeded
- `fetch-failed`: present in both, fetch failed (both partial and plain variants)

Each per-installation outcome SHALL be logged at INFO level in a single line containing the relative path and the classification, suitable for tailing the nightly log without `-v`.

#### Scenario: A new repo appears under the mount

- **WHEN** a directory `acme/svc-new` with a `.git` exists under `installations_path` and has no entry in `installations.json`
- **THEN** the installation is classified `new` and added to the re-index queue with `indexed_at` treated as the epoch

#### Scenario: A previously indexed repo is removed from the mount

- **WHEN** `installations.json` contains `acme/svc-old` but the directory no longer exists under `installations_path`
- **THEN** the installation is classified `removed` and queued for pruning from LanceDB and from `installations.json`

---

### Requirement: Re-index changed and new installations

For each installation classified `changed` or `new`, Spark SHALL invoke the existing `activate_installation()` helper to chunk, embed, and write the installation's chunks to LanceDB, then update the metadata entry. Activation failures SHALL be caught and logged per-installation; one failure SHALL NOT abort the rest of the sync run.

#### Scenario: Successful activation updates metadata

- **WHEN** `activate_installation('acme/svc-foo')` returns success
- **THEN** `installations.json` is updated with the new `indexed_at` and `last_remote_ts` for `acme/svc-foo` before the next installation is processed

#### Scenario: Activation failure preserves prior metadata

- **WHEN** `activate_installation('acme/svc-foo')` raises an exception
- **THEN** Spark logs the failure, does NOT update `acme/svc-foo`'s metadata entry, and continues with the next installation

---

### Requirement: Prune removed installations

For each installation classified `removed`, Spark SHALL delete its rows from the LanceDB index by filtering on the existing `Path` column (`table.delete("Path = '<rel_path>'")`) and then remove its entry from `installations.json`. Prune failures SHALL be caught and logged per-installation, never aborting the run.

#### Scenario: Successful prune removes rows and metadata entry

- **WHEN** installation `acme/svc-old` is classified `removed` and the LanceDB delete succeeds
- **THEN** every row in the index with `Path = 'acme/svc-old'` is gone, and `installations.json` no longer contains an entry for `acme/svc-old`

#### Scenario: Prune failure preserves metadata entry

- **WHEN** the LanceDB delete for `acme/svc-old` raises an exception
- **THEN** Spark logs the failure, leaves the metadata entry in place, and re-attempts the prune on the next sync run

---

### Requirement: Summarize each sync run

Spark SHALL emit a final summary line at INFO level after every sync run with counts for each classification (`up-to-date`, `changed`, `new`, `removed`, `fetch-failed`), elapsed wall time, and total chunks (re)written.

#### Scenario: Typical incremental run

- **WHEN** a sync run processes 437 installations, 3 of which are `changed` and the rest `up-to-date`
- **THEN** the summary line includes `up-to-date=434 changed=3 new=0 removed=0 fetch-failed=0` along with the elapsed time and chunk count

---

### Requirement: Preserve existing reclaim and activate commands

`spark reclaim` and `spark activate <rel_path>` SHALL continue to function as before. `reclaim` remains the escape hatch for schema migrations and disaster recovery; `activate` remains the way to force-index a single installation outside the sync cadence.

#### Scenario: Reclaim still rebuilds the full index

- **WHEN** an operator runs `spark reclaim` against an existing index
- **THEN** every installation discovered under `installations_path` is re-chunked, re-embedded, and written, producing the same index shape as before this change

#### Scenario: Activate still re-indexes a single installation

- **WHEN** an operator runs `spark activate acme/svc-foo`
- **THEN** only `acme/svc-foo`'s rows are rewritten in LanceDB; other installations' rows are not touched

---

### Requirement: Pipeline script uses sync as the nightly cadence

`spark/scripts/spark-pipeline.sh` SHALL invoke `spark sync` instead of `spark reclaim` as its first step. The synthesize step (`spark synthesize --all --days N`) SHALL continue to run as before, unmodified.

#### Scenario: Nightly cron runs sync then synthesize

- **WHEN** the launchd job `com.agents-nexus.guilty-spark.nightly` fires `spark-pipeline.sh`
- **THEN** the script first runs `spark sync` and only proceeds to `spark synthesize --all --days N` if sync exits 0
