## Why

Spark has drifted into a tangle of **3 serving surfaces, 3 indexes, and 2 codebases**, the
product of a half-finished migration:

- The **local stdio MCP** (`/usr/local/bin/spark` → the legacy standalone `guilty-spark`
  checkout) serves an **old 768-dim nomic index** — this is what agents actually query.
- The **container** (`nexus-spark:8343`, SSE + GitLab webhooks) serves a **384-dim fastembed** index.
- The intended **host-native 384-dim** index (`~/.spark-index/the-index`, built by the nightly
  `spark sync` via the canonical `agents-nexus/spark` code) **stalled at 12k chunks** on an OOM
  that is now fixed (batched-write, `b750fb6`).
- A validated **Bedrock Titan v2 1024-dim** POC index (`~/.spark-index/bedrock-full`, 40,338
  chunks, built in ~22 min) exists but nothing serves it.

The result: the served code is a divergent checkout with its own uncommitted WIP, the canonical
code (with the embedder/registry/OOM work) isn't what's running, and three vector spaces coexist.
Converge now — on one codebase, one index, one embedder — to kill the drift and make every
serving surface consistent and maintainable.

## What Changes

- Standardize on **AWS Bedrock Titan Text Embeddings V2, 1024-dim** as the canonical embedder for
  both index-time and query-time. **BREAKING**: 1024d ≠ the live 768d/384d indexes, so a full
  rebuild is required and the old indexes are retired.
- Serve the **canonical `agents-nexus/spark` code** on every surface; **retire the standalone
  `guilty-spark` checkout** as the served code (archived on disk as a fallback, not deleted).
- Point all three serving surfaces — the local `/usr/local/bin/spark` wrapper, the
  `nexus-spark` container, and the nightly `spark sync` — at **one canonical index path** built
  with the bedrock embedder.
- Inject AWS credentials into the serving surfaces (container + MCP) for query-time embedding.
  **Near-term decision: use existing SSO creds and tolerate query-time failures on token rotation
  — do NOT gate the migration on a scoped IAM key.** The scoped `bedrock:InvokeModel` IAM key is a
  documented hardening follow-up, not a blocker.
- Collapse the config-resolution surprises (the `.env` vs `config.yaml` vs wrapper layering that
  silently redirected the index path this session).
- Execute as a **no-downtime cutover**: build the new index alongside the live one, verify parity,
  then atomically repoint serving and retire the old.

## Capabilities

### New Capabilities

- `spark-embedding`: The embedder contract — index-time and query-time MUST use the same
  embedder/dimensions; the canonical embedder and its credential/availability expectations; and
  the requirement that every serving surface resolves to one index built with that embedder.

### Modified Capabilities

<!-- None. spark-indexing's discovery/sync/prune/metadata requirements are unchanged by this
     migration; only the embedder and serving topology change, which the new spark-embedding
     capability covers. -->

## Impact

- **Code**: `agents-nexus/spark` becomes the single source of truth; `guilty-spark` retired as
  served code (its query_registry patch + WIP archived). `embedder.py` bedrock path becomes the
  default served embedder.
- **Serving**: `/usr/local/bin/spark` wrapper (root-owned; sudo one-liner), `docker-compose.work.yml`
  `nexus-spark` service (env + AWS creds), and the nightly `spark sync` cron all repoint to the
  canonical bedrock index.
- **Config**: `agents-nexus/spark/.env` (currently pins fastembed + `~/.spark-index/the-index`)
  and `config.yaml` reconciled to the bedrock index; dead layers removed.
- **Credentials**: query-time now depends on AWS Bedrock access (SSO near-term; IAM key follow-up).
- **Indexes**: new canonical bedrock 1024d index built; legacy 768d (guilty-spark) and partial
  384d (`~/.spark-index/the-index`) indexes retired after cutover.
- **Cost**: ~$0.22 per full Bedrock rebuild (~40k chunks); negligible query-time cost.
