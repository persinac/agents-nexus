# Spark — converged topology

Status of the `spark-index-convergence` change. Goal: one codebase, one index, one
embedder, every serving surface aligned.

## Canonical setup

- **Code:** `agents-nexus/spark` (the only source of truth). The legacy standalone
  `guilty-spark` checkout is being retired as served code.
- **Embedder:** AWS Bedrock Titan v2 (`amazon.titan-embed-text-v2:0`), **1024-dim**, index- and
  query-time. Selectable via `SPARK_EMBEDDER` (`bedrock` | `fastembed` | `litellm`).
- **Index:** `~/.spark-index/bedrock-full` (40,338 chunks @ 1024d; `installations.json` carries
  per-repo `detected` profiles for `query_registry`).
- **Installations:** `~/repos` (438 git trees).

## Serving surfaces

| Surface | How it runs | Index | State |
|---|---|---|---|
| Local stdio MCP / CLI | `/usr/local/bin/spark` → `agents-nexus/spark/.venv/bin/spark` | bedrock 1024d | ✅ repointed (relaunch sessions to pick up) |
| Container SSE + webhooks | `nexus-spark:8343`, built from `./spark` | bedrock 1024d (host index mounted **read-only**) | ✅ live |
| Nightly `spark sync` | `spark/scripts/spark-pipeline.sh` → canonical `.venv` | bedrock 1024d | ✅ wired (needs non-SSO creds at cron — see below) |

## Config precedence (single source of truth)

Effective config resolves as: **process env > `.env` (loaded by `config.load()`) > `config.yaml`**.
To avoid the layering confusion that bit us, the canonical `agents-nexus/spark/.env` is the one
place that sets `SPARK_EMBEDDER`, `SPARK_INDEX_PATH`, `SPARK_INSTALLATIONS_PATH`, and AWS creds;
`config.yaml` defaults are aligned to match. The container overrides via compose `environment:`.

## Write ownership

**Host owns writes** (nightly `sync` / `reclaim`); the container serves the index **read-only**.
Consequences:
- The container emits a benign LanceDB `ReadOnlyFilesystem` WARN (quieted via `RUST_LOG=error`);
  reads are unaffected.
- **Webhook incremental indexing on the container can no longer write** — MR merges are picked up
  by the nightly host sync, not instantly. To restore instant indexing, relocate the webhook
  embed+write to a host process (follow-up 6.2).
- If a full host `reclaim` ever overwrites the table, **bounce the container** so its read handle
  picks up the new version (prefer incremental `sync` to avoid this).

## Credentials

Query-time + nightly embedding require AWS Bedrock access. **Near-term: SSO creds** (mounted
`~/.aws` cache for the container; `AWS_PROFILE=dev-engineer`). SSO tokens rotate (~8–12h); when
they expire, query-time embedding fails **loudly** (`BedrockAuthError`) and the unattended nightly
cannot embed at all. **Hardening (follow-up 6.1): provision a scoped `bedrock:InvokeModel` IAM key**
and switch the container env + host `.env` + nightly to it — this is what makes the whole thing
truly seamless.

## Remaining

- **6.1** Scoped IAM key → replace SSO (the seamless linchpin).
- **6.2** Webhook incremental indexing → host-side writer (or guard) for instant freshness.
- **P4** After a soak on the local cutover, archive the `guilty-spark` checkout and drop the legacy
  768d (`guilty-spark/data/the-index`) and partial 384d (`~/.spark-index/the-index`) indexes.
