# Ideas & Roadmap

## High impact — low effort

1. ~~**Windows toast notifications**~~ — removed; red status bar indicator (item 3) is sufficient
2. **Batch approve/reject** — done: `qa` function (e.g., `qa 1` to approve all waiting agents)
3. **Wait duration in status bar** — done: three-state color system (green=working, grey=idle, red+timer=needs input) via `PreToolUse`, `Stop`, and `Notification` hooks
4. **CLAUDE.md files per repo** — done: `CLAUDE.md.template` + `claude-init` command to scaffold

## Medium effort — multiplier effects

| # | Priority | Idea | Status |
|---|----------|------|--------|
| 5 | 10 | **Session templates** — predefined layouts for common workflows (e.g., `work-stack frontend backend tests`). One command to set up a whole workstream | backlog |
| 6 | 1 | **Git worktree integration** — auto-create worktrees when spawning agents in the same repo, so two agents can work on different branches without conflicts | done |
| 7 | 5 | **Agent summary on peek** — enhance `v()` to parse the last Claude output and show a one-line status instead of raw terminal output | done |
| 8 | 5 | **Stuck agent detection** — if an agent's been "running" (green) for >10min with no tool use logged, flag it yellow | done |
| 9 | — | **Agent-to-agent messaging** — `/msg <slot> <message>` slash command + `agent-send.sh` script | done |

## Bigger bets

| # | Priority | Idea | Status |
|---|----------|------|--------|
| 10 | 3 | **Agent handoff / chaining** — when agent 2 finishes, auto-send its summary to agent 3. Pipeline: write → review → test | |
| 11 | 2 | **Searchable history & agent memory** — central knowledge store across all devices. Design: [`searchable-history-design.md`](docs/searchable-history-design.md) | done |
| 11a | 2 | ↳ Central Postgres schema (`memory_events` + `memory_nodes` + `memory_links` + `memory_entities`) + MCP server with basic CRUD tools | done |
| 11b | 2 | ↳ Local wrapper + CF tunnel + tmux hook integration (auto-capture events from `Stop`/`PreToolUse`/`Notification` hooks) | done |
| 11c | 3 | ↳ Entity extraction + backlinks ("everything about auth-module") + tag taxonomy | done |
| 11d | 4 | ↳ pgvector embeddings on notes + semantic search (MAGMA-style anchor identification) | done |
| 11e | 5 | ↳ Temporal edges | done |
| 11g | 6 | ↳ Auto-inject knowledge into agent prompts (`/recall` + MAGMA retrieval + token-budgeted context) | done |
| 12 | 4 | **Cost/token dashboard** — track token usage per agent per session alongside APM | |
| 13 | 7 | **Auto-routing** — when an agent goes red, automatically `v` it in a small persistent pane at the bottom | |
| 14 | 2 | **Pixel agents dashboard** — fork [pixel-agents](https://github.com/pablodelucca/pixel-agents) React webview into a standalone Electron/web app. Replace VS Code postMessage with WebSocket fed by tmux hook data. Animated pixel art characters show agent state (typing/reading/waiting/idle) in a fun office visualization. Source: `webview-ui/` in the pixel-agents repo. | done |
| 15 | 2 | **Agent awareness** — agents should know about other running agents (window slot, repo, branch, status) and be able to use `/msg` to request info from them. Could be a CLAUDE.md snippet or a hook that injects context on session start. | done |
| 16 | 6 | **Causal inference** — slow-path LLM batch over note pairs to infer why changes happened. Anthropic Message Batches API. Run after session end. Defer until ~50+ notes in corpus. | |
| 17 | 6 | **Knowledge graph web UI** — interactive visualization of memory nodes + links. Orthogonal views (temporal/causal/semantic/entity). Extend pixel-dashboard. Build after causal inference produces links worth visualizing. | |
| 18 | 5 | **Versioned agentic scheduled jobs** — add a `jobs/` directory pattern: each job is a folder with a plist + script (e.g. `obs-digest`, `obs-tidy`). Installer links scripts to `~/.local/bin` and plists to `~/Library/LaunchAgents`. Makes it easy to add new Claude-powered cron-style jobs. | |
| 19 | 4 | **Multi-org GitHub token rotation for Spark** — support multiple GitHub PATs in config (keyed by org), so Spark enrichment (descriptions, PRs) works across `persinac`, `lockfale`, `flippin-balls` without needing a single god-token. Fine-grained PATs are scoped per-org. | |
| 20 | 1 | **Spark discovery enrichment** — improve Stage 1 broad search recall by enriching monitor-log summaries with structured tech/service metadata extracted from actual code. See details below. | |

## Spark Discovery Enrichment (idea 20)

### Problem

Spark's two-stage search funnel (Stage 1: summary discovery → Stage 2: code search) fails when broad queries use terms like "oauth", "cognito", or "google login" — even though multiple repos (management-dashboard, store-front, storefront-api) heavily use these services. The root cause: Stage 1 relies on monitor-log summaries capped at 950 chars (`SUMMARY_MAX_CHARS`), built from `_SUMMARY_FILES` (CLAUDE.md, README, package.json, etc.). If those files don't mention the right keywords in their first ~950 chars, Stage 2 code search never fires because the repo isn't discovered.

The existing `detector.py` only detects web frameworks (NextJS, FastAPI, etc.), CI type, and deploy target. It completely ignores cloud services, auth patterns, and infrastructure dependencies that are the most common broad-search targets.

### Current state of relevant code

- **`detector.py`** — `detect_framework()` scans `package.json` deps and `pyproject.toml` but only for web framework names (next, react, fastapi, flask, etc.). No detection of cloud services (Cognito, S3, DynamoDB), auth patterns (OAuth, SSO, JWT, OIDC), or infrastructure services (Redis, Elasticsearch, Kafka).
- **`chunker.py:build_summary_chunk()`** — Assembles the monitor-log from a structured header (team, path, groups, description, topics, languages, framework, CI, deploy) plus file excerpts from `_SUMMARY_FILES`. The `_SUMMARY_FILES` list is root-only — no crawl of `docs/` or `notes/` subdirectories.
- **`chunker.py:Chunk` dataclass** — Has fields for `framework`, `ci_type`, `deploy_target`, `test_command`, `lint_command` but no field for cloud services, auth mechanisms, or dependency-derived tags.
- **`mcp_server.py:spark()`** — Stage 1 filters to `chunk_type = "summary"` and runs hybrid search (vector + BM25 via RRF if enabled). BM25 can only match keywords that appear in the summary text — if "cognito" isn't in the 950-char summary, it's invisible to both vector and keyword search.
- **`mcp_server.py:spark_deep()`** — Stage 1 auto-discovery returns top 5 repos, which is a tight candidate set.

### Proposed changes

#### 1. Add `detect_services()` to `detector.py`

Scan dependency files for known cloud services, auth patterns, and infrastructure:

**package.json dependencies:**
- AWS: `@aws-sdk/client-cognito-identity-provider` → "cognito", `@aws-sdk/client-s3` → "s3", `@aws-sdk/client-dynamodb` → "dynamodb", `aws-amplify` → "amplify"
- Auth: `next-auth` → "oauth, nextauth", `passport` → "oauth, passport", `jsonwebtoken` → "jwt", `@auth0/nextjs-auth0` → "auth0, oauth", `firebase-admin` → "firebase-auth"
- Infra: `redis`/`ioredis` → "redis", `@elastic/elasticsearch` → "elasticsearch", `kafkajs` → "kafka", `pg`/`knex`/`prisma` → "postgres", `mongoose`/`mongodb` → "mongodb"
- Google: `googleapis` → "google-api", `@google-cloud/*` → "gcp"

**pyproject.toml / requirements.txt:**
- `boto3`/`botocore` → "aws", `django-allauth` → "oauth", `python-jose` → "jwt", `authlib` → "oauth", `celery` → "celery", `sqlalchemy` → "postgres/sql", `redis` → "redis"

**Terraform resources:**
- Parse `*.tf` files for `resource "aws_cognito_*"` → "cognito", `resource "aws_s3_*"` → "s3", `resource "aws_dynamodb_*"` → "dynamodb", `resource "aws_rds_*"` → "rds", etc.

**Docker-compose services:**
- Parse `docker-compose.yml` for `image:` entries: `redis:*` → "redis", `postgres:*` → "postgres", `elasticsearch:*` → "elasticsearch", `localstack` → "aws-local"

**Env var names (.env.example, next.config.js, etc.):**
- `COGNITO_*` or `NEXT_PUBLIC_COGNITO_*` → "cognito"
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` → "google-oauth"
- `AUTH0_*` → "auth0"
- `REDIS_*` → "redis"
- `DATABASE_URL` with `postgres` → "postgres"

Return a deduplicated `list[str]` of service/technology tags.

#### 2. Add `services` field to `Chunk` and `DetectedProject`

Add a `services: str = ""` field (comma-separated tags) to the `Chunk` dataclass and `DetectedProject`. During `build_summary_chunk()`, append a `Services: cognito, oauth, amplify, s3` line to the summary parts. This makes the tags visible to both the embedding model (semantic) and BM25 (keyword) search.

#### 3. Expand `_SUMMARY_FILES` to include docs/notes

Add a secondary scan after the primary `_SUMMARY_FILES` loop: if budget remains, crawl `docs/`, `notes/`, and `doc/` for `*.md` files and append the first N chars from each. These directories often contain rich context about what a repo actually does (e.g., management-dashboard had detailed OAuth flow docs in `notes/`).

Could either:
- Add glob patterns like `docs/README.md`, `docs/*.md`, `notes/*.md` to `_SUMMARY_FILES`
- Or add a post-loop that walks those dirs and appends content up to the remaining budget

The second approach is more flexible and doesn't require knowing filenames in advance.

#### 4. Widen Stage 1 candidate set in `spark_deep`

Change the hardcoded top-5 Stage 1 discovery in `spark_deep()` to top-8 or top-10. The cost of searching a few extra repos at the file/symbol level is low (a few hundred ms) compared to missing relevant results entirely. Could also make this configurable via `config.yaml`.

#### 5. Extract Terraform resource types as service signals

During indexing, scan `*.tf` files for `resource "TYPE" "NAME"` patterns and extract the service component (e.g., `aws_cognito_user_pool` → "cognito", `aws_lambda_function` → "lambda"). This is a high-signal, low-noise extraction — Terraform resource types are a direct declaration of what cloud services a repo provisions.

#### 6. Periodic deep re-indexing

The existing `reclaim()` function rebuilds the full index but uses the same shallow analysis. Add a `--deep` flag (or schedule separately) that runs the enriched service detection. Summaries generated when repos were younger may not reflect current service usage. A nightly or weekly re-index with the deeper pass keeps summaries current. Could piggyback on the existing `nightly-pipeline` systemd timer.

### Implementation priority

1. **`detect_services()` + `services` field on Chunk** — closes the core gap; highest ROI
2. **Expand `_SUMMARY_FILES` with docs/notes crawl** — cheap to implement, captures existing rich context
3. **Widen Stage 1 top-k** — one-line change, immediate recall improvement
4. **Terraform resource extraction** — subset of #1, straightforward regex
5. **Env var signal extraction** — moderate effort, good for catching config-level service hints
6. **Deep re-indexing schedule** — ops concern, do after the detection code lands
