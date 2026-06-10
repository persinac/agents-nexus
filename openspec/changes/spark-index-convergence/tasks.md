## 1. P1 — Build the canonical bedrock index alongside the live one

- [x] 1.1 Choose + create the canonical index path — reusing `~/.spark-index/bedrock-full` for now (P3 may rename to a stable `~/.spark-index/the-index` + symlink)
- [x] 1.2 Run `spark reclaim` with bedrock — done this session: 438 installs, 40,338 chunks @ 1024d in ~22min, 0 zero-vectors
- [x] 1.3 Confirm the live 768d `guilty-spark` index is untouched and still serving during the build — confirmed (separate path)

## 2. P2 — Verify parity against the new index

- [x] 2.1 Verify row count (40,338) and vector dim = 1024 — confirmed
- [x] 2.2 Spot-check semantic `spark query` via bedrock — "claims adjudication state machine" → svc-claims-adjudication; 1024d match, no dim error
- [x] 2.3 Verify `query_registry`/`registry` filters — fastify→13, python+backend→197
- [ ] 2.4 Verify `installation_summary` + `list_installations` against the new index
- [x] 2.5 Confirm `installations.json` carries `detected` for all installs — 438/438

## 3. P3 — Atomic cutover of all serving surfaces

- [x] 3.1 Set canonical `agents-nexus/spark/.env`: `SPARK_EMBEDDER=bedrock`, `SPARK_INDEX_PATH=~/.spark-index/bedrock-full`, `AWS_PROFILE`/`AWS_REGION`; verified canonical spark resolves to bedrock with no env overrides
- [ ] 3.2 Back up `/usr/local/bin/spark` (done → /tmp/spark-wrapper.bak); repoint it (sudo, one line) to run canonical `agents-nexus/spark` ← **YOUR STEP**
- [x] 3.3 Container: rebuilt (canonical code + boto3), `SPARK_EMBEDDER=bedrock`, `AWS_PROFILE=dev-engineer` (pinned, not shell-derived), `~/.aws` SSO-cache mounted, host bedrock index mounted **READ-ONLY**. Verified: query (Titan→1024d), query_registry (13 fastify), SSE :8343 = 200, 40,338 chunks. NOTE: benign read-only-fs WARN from LanceDB (manifest-namespace opt skipped; reads fine).
- [ ] 3.4 Relaunch local MCP/sessions so the stdio MCP picks up canonical code + new index
- [ ] 3.5 Smoke-test each surface post-cutover: local MCP `spark`/`query_registry`, container SSE `:8343`, and a webhook/MR-review path

## 4. P4 — Retire legacy

- [ ] 4.1 Archive the `guilty-spark` checkout (keep on disk as fallback; do not delete)
- [ ] 4.2 Remove dead config layers (stale `.env`/`config.yaml` index/embedder pins)
- [ ] 4.3 After a soak period, drop the old 768d (`guilty-spark/data/the-index`) and partial 384d (`~/.spark-index/the-index`) indexes

## 5. P5 — Re-wire nightly + document

- [x] 5.1 Confirm the nightly `spark sync` resolves to the canonical bedrock index + embedder — verified via `sync --dry-run` (up-to-date=423 changed=4; resolves to bedrock-full @ 1024d). Caveat: cron embedding of changed repos needs non-SSO creds (6.1).
- [ ] 5.2 Watch one nightly run and confirm it completes + writes metadata — pending (gated on 6.1 creds for unattended embedding)
- [x] 5.3 Document the converged topology + config precedence + SSO→IAM follow-up — `docs/spark-convergence.md`

## 5b. Polish + config hygiene (done)

- [x] Align canonical `config.yaml` to the bedrock index/embedder so it can't contradict `.env` (D2)
- [x] `status` now prints the real `Embedder: bedrock (...1024d)` instead of a stale model echo (E1)
- [x] Stage `RUST_LOG=error` in compose to quiet the read-only-FS WARN (E2; applies next container recreate)

## 6. Follow-up (not blocking)

- [ ] 6.1 Provision a scoped `bedrock:InvokeModel` IAM key and switch serving creds off SSO (removes query-time rotation failures)
- [ ] 6.2 Relocate webhook incremental indexing to a host-side writer (or guard it). The container now mounts the index READ-ONLY (host owns writes), so GitLab MR-merge webhooks can no longer write chunks — merges are currently only picked up by the nightly host `spark sync`. Decide: move the webhook's embed+write to a host process, or guard the container webhook to skip indexing on a read-only mount, if instant MR indexing is wanted back.
