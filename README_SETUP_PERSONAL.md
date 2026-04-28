# Personal Setup

Local stack: agents-nexus + Langfuse + LiteLLM. LiteLLM upstreams directly to
Anthropic. Every Claude Code turn flows through LiteLLM and gets logged to
your self-hosted Langfuse with model, token, cache, and cost fields populated.

```
  claude code  ──►  litellm (:4000)  ──►  api.anthropic.com
                          │
                          └────────►  langfuse (:3000)
```

## Prerequisites

- Docker Desktop (or compatible)
- An Anthropic API key
- `tmux` if you use the agent launcher scripts (optional)
- `task` (Taskfile.dev) for the convenience targets (optional — raw
  `docker compose` works too)

## 1. Configure `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Required:

- `ANTHROPIC_API_KEY` — your Anthropic key (`sk-ant-...`)
- `HOST_TMUX_DIR` — usually `~/.tmux`
- `REPOS_PATH` — absolute path to a directory containing the repos you want
  spark to index
- `DATABASE_URL` — Postgres for memory storage. Either point at a cloud Postgres
  here, or use `docker-compose.work.yml` instead — that compose file bundles a
  local pgvector container so you don't need an external DB

Generate fresh secrets for the Langfuse stack (do not reuse defaults):

```bash
LANGFUSE_NEXTAUTH_SECRET=$(openssl rand -base64 32)
LANGFUSE_SALT=$(openssl rand -base64 32)
LANGFUSE_ENCRYPTION_KEY=$(openssl rand -hex 32)   # must be 64 hex chars
LANGFUSE_DB_PASSWORD=$(openssl rand -hex 16)
LANGFUSE_REDIS_AUTH=$(openssl rand -hex 16)
LANGFUSE_CLICKHOUSE_PASSWORD=$(openssl rand -hex 16)
```

Leave `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` blank for now — you'll fill
them in after creating a Langfuse project (step 3).

Leave `ANTHROPIC_API_BASE` commented out — it defaults to
`https://api.anthropic.com`.

## 2. Bring up the stack

```bash
docker compose up -d
docker compose --profile langfuse up -d
docker compose run --rm ollama-init       # one-time: pulls embedding model
```

Or via `task`: `task up && task langfuse:up && task ollama:init`.

Containers you should see:

- `nexus-ollama`, `nexus-spark`, `nexus-mnemon-mcp`, `nexus-mnemon-flush`,
  `nexus-dashboard`, `nexus-litellm`
- `langfuse-web`, `langfuse-worker`, `langfuse-postgres`, `langfuse-redis`,
  `langfuse-clickhouse`, `langfuse-minio`

## 3. Wire Langfuse keys into LiteLLM

1. Open `http://localhost:3000`. Create a user, then create a project.
2. Project Settings → **API Keys** → **Create new key**.
3. Copy the `pk-lf-...` value into `.env` as `LANGFUSE_PUBLIC_KEY`, and the
   `sk-lf-...` value as `LANGFUSE_SECRET_KEY`.
4. Recreate LiteLLM so it picks up the new env:

```bash
docker compose up -d --force-recreate litellm
```

In `docker logs nexus-litellm` you should see
`Initialized Success Callbacks - ['langfuse']`.

## 4. Verify end-to-end

```bash
curl -s http://localhost:4000/health/liveliness
# -> "I'm alive!"

curl -s http://localhost:4000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: dummy" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 15,
    "messages": [{"role":"user","content":"Say PONG."}]
  }'
```

Wait ~10 seconds, refresh Langfuse — a trace named `litellm-anthropic_messages`
should appear with model, token counts, and cost.

## 5. Route Claude Code through LiteLLM

Add this to your shell init for the launching shell — `~/.tmux/env.sh` if you
use the included tmux launchers, or `~/.zshrc` / `~/.bashrc` otherwise:

```bash
# Route Anthropic API through local LiteLLM when reachable. Falls through to
# direct Anthropic if the gateway is down so claude keeps working.
PERSONAL_LITELLM_URL="${PERSONAL_LITELLM_URL:-http://localhost:4000}"
if curl -sf -m 0.3 "$PERSONAL_LITELLM_URL/health/liveliness" >/dev/null 2>&1; then
  export ANTHROPIC_BASE_URL="$PERSONAL_LITELLM_URL"
else
  unset ANTHROPIC_BASE_URL
fi
```

Open a fresh terminal — `echo $ANTHROPIC_BASE_URL` should print
`http://localhost:4000`. Existing Claude Code sessions hold long-lived
connections; restart them to pick up the new gateway.

### Remote stack (laptop → another machine over LAN/tailscale)

When the agents-nexus stack runs on a different host than where Claude Code
launches, override the URL before sourcing the snippet above:

```bash
export PERSONAL_LITELLM_URL="http://<host-or-tailscale-ip>:4000"
```

The health check still gates the export, so claude falls back to direct
Anthropic if the remote host is unreachable.

## Troubleshooting

- **No trace appears in Langfuse**: Check `docker logs nexus-litellm` for
  `Initialized Success Callbacks - ['langfuse']`. If missing, the
  `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` env vars aren't set in the
  container — recreate it after editing `.env`.
- **Trace is named `undefined` in the UI**: applies only to mnemon MCP tool
  traces, not LiteLLM. The trace name is set via the `langfuse.trace.name`
  OTel attribute in the wrapper.
- **Port collision** (4000, 3000, 8330, etc.): adjust the matching `*_PORT`
  variable in `.env`.
- **Container can't reach `langfuse-web`**: services share an implicit
  default docker network — ensure all containers are in the same compose
  project (`docker compose ps` shows them under one project name).
