# Personal Setup

Local stack: agents-nexus + Langfuse + a transparent Anthropic proxy. The proxy
upstreams directly to Anthropic. Every Claude Code turn flows through it and
gets logged to your self-hosted Langfuse with model, token, cache, and cost
fields populated.

```
  claude code  ──►  proxy (:4000)  ──►  api.anthropic.com
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
./install.sh --profile personal
```

Pick the **Langfuse** peripheral when prompted so the six stack secrets get generated for you. `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` come later in § 3.

Full installer reference — every flag, every prompt, the manual fallback — lives in **[INSTALL.md](INSTALL.md)**.

## 2. Bring up the stack

```bash
docker compose up -d
docker compose --profile langfuse up -d
docker compose run --rm ollama-init       # one-time: pulls embedding model
```

Or via `task`: `task up && task langfuse:up && task ollama:init`.

Containers you should see:

- `nexus-ollama`, `nexus-spark`, `nexus-mnemon-mcp`, `nexus-mnemon-flush`,
  `nexus-dashboard`, `nexus-proxy`
- `langfuse-web`, `langfuse-worker`, `langfuse-postgres`, `langfuse-redis`,
  `langfuse-clickhouse`, `langfuse-minio`

## 3. Wire Langfuse keys into the proxy

1. Open `http://localhost:3000`. Create a user, then create a project.
2. Project Settings → **API Keys** → **Create new key**.
3. `./install.sh --finish-langfuse` and paste the `pk-lf-...` / `sk-lf-...` values when prompted.

See [INSTALL.md § Two-phase Langfuse setup](INSTALL.md#two-phase-langfuse-setup) for the manual equivalent.

`docker logs nexus-proxy` should show the FastAPI startup line and no errors
on subsequent /v1/messages traffic.

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

Wait ~10 seconds, refresh Langfuse — a trace named `claude-code` should
appear with model, token counts, and cost.

## 5. Route Claude Code through the proxy

Add this to your shell init for the launching shell — `~/.tmux/env.sh` if you
use the included tmux launchers, or `~/.zshrc` / `~/.bashrc` otherwise:

```bash
# Route Anthropic API through the local proxy when reachable. Falls through to
# direct Anthropic if the gateway is down so claude keeps working.
PERSONAL_PROXY_URL="${PERSONAL_PROXY_URL:-http://localhost:4000}"
if curl -sf -m 0.3 "$PERSONAL_PROXY_URL/health/liveliness" >/dev/null 2>&1; then
  export ANTHROPIC_BASE_URL="$PERSONAL_PROXY_URL"
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
export PERSONAL_PROXY_URL="http://<host-or-tailscale-ip>:4000"
```

The health check still gates the export, so claude falls back to direct
Anthropic if the remote host is unreachable.

## Troubleshooting

- **No trace appears in Langfuse**: Check `docker logs nexus-proxy` for
  langfuse-related warnings. If `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
  aren't set in the container, recreate it after editing `.env`.
- **Trace is named `undefined` in the UI**: applies only to mnemon MCP tool
  traces. The proxy sets the trace name via `langfuse.trace.name`.
- **Port collision** (4000, 3000, 8330, etc.): adjust the matching `*_PORT`
  variable in `.env`.
- **Container can't reach `langfuse-web`**: services share an implicit
  default docker network — ensure all containers are in the same compose
  project (`docker compose ps` shows them under one project name).
