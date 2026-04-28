# Work Setup

Same stack as the personal setup, but LiteLLM upstreams to a corporate LLM
gateway (Bifrost-style proxy with SSO auth) instead of `api.anthropic.com`
directly. Your local Langfuse still receives every trace, so you keep personal
observability on top of whatever the corporate gateway provides.

```
  claude code  ──►  litellm (:4000)  ──►  corporate gateway (host)  ──►  upstream
                          │
                          └────────►  langfuse (:3000)
```

## Prerequisites

- Everything from [README_SETUP_PERSONAL.md](README_SETUP_PERSONAL.md) — finish
  steps 1–4 there first to confirm the stack works against direct Anthropic
  before adding the gateway hop.
- A locally-running tunnel/daemon that exposes the corporate gateway on a
  loopback port. Typically this is a vendor-supplied CLI that runs in the
  background and binds `127.0.0.1:<port>`. Confirm it's up:
  ```bash
  lsof -nP -iTCP -sTCP:LISTEN | grep -i <daemon-name>
  # -> 127.0.0.1:<port> (LISTEN)
  ```
- The gateway's path prefix (commonly `/anthropic` for Bifrost-shaped proxies).

## 1. Configure LiteLLM to upstream to the gateway

Edit `.env` and uncomment / set `ANTHROPIC_API_BASE`:

```bash
ANTHROPIC_API_BASE=http://host.docker.internal:<port>/anthropic
```

Two things to get right:

- **`host.docker.internal`** (not `localhost`). The LiteLLM container needs to
  reach the daemon on the host. On Docker Desktop this resolves to the host's
  loopback interface even when the daemon is bound to `127.0.0.1`.
- **The path suffix** matches the gateway's expected route. Bifrost-style
  proxies expose `/anthropic/v1/messages`, so set `ANTHROPIC_API_BASE` to the
  prefix without `/v1/messages` — LiteLLM will append it.

Recreate the container so it picks up the new env:

```bash
docker compose up -d --force-recreate litellm
```

Verify the env reached the container:

```bash
docker exec nexus-litellm env | grep ANTHROPIC_API_BASE
```

## 2. Verify the chain

A bare request without tools may fail with a validation error such as
`At least one tool must have defer_loading=false` — many corporate gateways
inject server-side tool definitions and require at least one non-deferred
tool. Use a request shaped like a real Claude Code turn:

```bash
curl -s http://localhost:4000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: dummy" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 30,
    "system": "Reply with one word: PONG.",
    "tools": [{
      "name": "noop",
      "description": "does nothing",
      "input_schema": {"type": "object", "properties": {}}
    }],
    "messages": [{"role": "user", "content": "go"}]
  }'
```

Expect HTTP 200 with `"text": "PONG"` (or `"PONG."`). Wait ~10 seconds and
confirm Langfuse picked up the trace:

```bash
docker exec langfuse-clickhouse clickhouse-client --query \
  "SELECT id, name, timestamp FROM traces \
   WHERE timestamp > now() - INTERVAL 1 MINUTE \
   ORDER BY timestamp DESC LIMIT 1 FORMAT Vertical"
```

## 3. Auth

Authentication for the upstream call is handled by the tunnel daemon (typically
SSO via Cloudflare Access or similar). The `x-api-key` LiteLLM forwards is
effectively unused — the gateway substitutes its own credentials before
reaching the model provider. `ANTHROPIC_API_KEY` in `.env` still has to be
*set* (LiteLLM requires the env var to be present) but its value doesn't
matter for the upstream call. Use a placeholder if you don't have a real key.

## 4. Token overhead

Corporate gateways often inject system context (compliance prompts, internal
tool definitions, etc.) into every request. Expect input token counts to jump
by several hundred tokens per turn versus direct Anthropic. On Haiku and
Sonnet this is negligible cost; budget accordingly for Opus-heavy workflows.

The Langfuse trace records the *full* token count as billed by the upstream,
so you can audit the overhead per call from the UI.

## 5. Route Claude Code through LiteLLM

The same step from the personal setup applies — add the snippet from
[README_SETUP_PERSONAL.md § 5](README_SETUP_PERSONAL.md#5-route-claude-code-through-litellm)
to your shell init. No work-specific change is needed there: Claude Code still
points at LiteLLM (`http://localhost:4000`), and LiteLLM is the one that fans
out to the corporate gateway based on `ANTHROPIC_API_BASE`.

## Switching back to direct Anthropic

Comment out (or remove) `ANTHROPIC_API_BASE` in `.env`, then
`docker compose up -d --force-recreate litellm`. The default falls back to
`https://api.anthropic.com`.

## Troubleshooting

- **`error code: 1010` from the daemon**: Cloudflare Access blocked the
  request — usually means the SSO session expired. Re-authenticate via the
  vendor CLI.
- **Connection refused from container**: confirm the daemon is bound and
  reachable. From inside the container:
  ```bash
  docker exec nexus-litellm python3 -c \
    "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:<port>/anthropic', timeout=2).status)"
  ```
- **400 with `defer_loading` in the error message**: see § 2 — your test
  request needs at least one tool with `defer_loading: false` (or simply
  any tool, if the gateway defaults `defer_loading` to false).
- **No trace in Langfuse despite a successful call**: same as personal setup
  — verify Langfuse callbacks initialized in `docker logs nexus-litellm`.
