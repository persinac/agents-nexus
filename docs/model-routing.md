# nexus-proxy: resilience + Anthropic-only cost routing

## Context

`proxy/main.py` (`nexus-proxy`, port 4000) is a hand-rolled FastAPI+httpx transparent
pass-through: it forwards Claude Code's Anthropic Messages traffic verbatim and logs each
`/v1/messages` call to Langfuse. Today it has **no retry and no routing** — a `429/5xx/529`
*response* from Anthropic is returned to the client verbatim (only transport `httpx.HTTPError`
is caught → 502).

Goal: stop provider overload/rate-limits from blocking work, and shed cheap turns to cheaper
Anthropic models — **without ever degrading or breaking Claude Code**. Strictly additive,
**fail-open**: any uncertainty → forward original bytes to the requested model.

> This supersedes an earlier scratch plan that an adversarial pre-build review correctly
> rejected for three concrete defects, all fixed below:
> 1. **429 "let Claude back off" is impossible for streaming.** `proxy()` returns a
>    `StreamingResponse` whose HTTP `200` + headers are flushed *before* `generate()` ever
>    contacts upstream, so an upstream `429` can only surface as an in-band SSE `event: error`
>    — which does **not** trigger Claude Code's HTTP-429 client backoff. Streaming is the
>    dominant `/v1/messages` traffic, so the proxy must own 429 handling for streams.
> 2. **Tool-presence cannot gate routing.** Claude Code resends its full tool array on
>    essentially every turn (stateless), so "passthrough when tools present" ⇒ the router
>    never fires. Difficulty must key on context size, not tools.
> 3. **"in-family" was ambiguous.** Use "never cross **vendor** (Anthropic), with a
>    haiku<sonnet<opus size ladder."

## Invariants (must hold)

- **Fail-open.** Any exception in classify/select/serialize → forward original `body_bytes`,
  requested model. With `ROUTE_ENABLED=0` the path is byte-for-byte today's.
- **Never cross vendor.** Candidates are Anthropic only. Within Anthropic, downgrades move
  *down* the size ladder `haiku < sonnet < opus`; never up, never to another vendor.
- **Work sessions always passthrough** (`_is_work()` short-circuits) — no routing/downgrade,
  same-model transient retry only.
- **Streaming: commit the outcome before the first byte.** Once a chunk is yielded the model
  is committed; no retry after that.
- **Preserve prompt caching** — only `body["model"]` is rewritten; `cache_control`, `system`,
  `tools`, `messages` untouched and re-serialized unchanged.
- **Bounded latency & memory**, no new deps, stays under the 512m/swap-off container cap.
- **Do NOT rebuild or restart the live `nexus-proxy` container as part of this work** — it is
  the live gateway for the whole agent fleet. Verification is unit + a local mock upstream
  only; deployment is a human step (see Verification).

## Design

### 1. `proxy/routing.py` (new, pure, unit-tested)
- `RETRYABLE = {500, 502, 503, 529}` (429 handled separately, below).
- `backoff_delays(attempt, retry_after)` — honor upstream `Retry-After`; else
  `min(0.5 * 2**attempt + jitter, 8s)`.
- `classify_difficulty(body) -> "trivial"|"normal"|"hard"` — signals: estimated input tokens
  `(len(json(messages)) + len(system_text)) // 4`, message count (turn depth),
  `bool(body.get("thinking"))`, `max_tokens`. **Ignore `tools`/`tool_result` presence entirely**
  (always present in Claude Code traffic — not a discriminator). `trivial` = small context +
  shallow depth + no thinking + modest `max_tokens`, *regardless of tools*.
- `load_pool()` — Anthropic size ladder `[{model, tier, cost}]` (haiku/sonnet/opus) with static
  prices; overridable via `ROUTE_POOL_FILE`/`ROUTE_POOL`. (A future cross-vendor entry carries a
  `translate` flag and is inert until Phase 3 — extensibility seam only.)
- `select_model(requested, difficulty, pool, cooldowns)` — downgrade **only** when `difficulty`
  ∈ `ROUTE_DOWNGRADE_TIERS` (default `trivial`): pick the cheapest Anthropic model whose tier
  covers `difficulty`, `cost ≤ requested.cost`, not in cooldown. `normal`/`hard` keep requested.
  Unknown requested model → passthrough. **No tool-based gate anywhere.**
- `Cooldowns` — `{model: until_ts}` + per-model sliding 429/5xx window; shared by resilience and
  selection so a throttled model is skipped.

### 2. `proxy/main.py` wiring
Capture `requested_model = body.get("model")` **before** any rewrite. If `ROUTE_ENABLED` and
personal and `is_messages`, `served = select_model(...)`; if changed, set `body["model"]=served`
and forward `content=json.dumps(body)` (else forward original `body_bytes`).

**Non-stream path:** retry loop around `client.request` — on `RETRYABLE`/`httpx.HTTPError`,
`backoff_delays` then retry same model up to `ROUTE_MAX_RETRIES` (default 2); then one in-family
(down-ladder) shed. On `429`: may return verbatim (Claude Code's HTTP backoff works here) unless
persistent → cooldown + shed. (Non-stream is rare.)

**Stream path (the corrected core):** the proxy must decide the outcome **before** returning a
`StreamingResponse`, because FastAPI flushes `200`+headers the instant the body starts. So:
- Open `client.stream(...)`, inspect `r.status_code` **before yielding anything**.
- `RETRYABLE` (5xx/529) and no bytes → close, backoff, retry same model (bounded), then shed
  down-ladder — all before any client byte.
- `429` while streaming → the proxy **owns it** (an HTTP 429 can no longer reach the client, and
  an SSE error won't trigger Claude's backoff): bounded retry-with-backoff, then shed to the next
  cheaper Anthropic model; cooldown the throttled model. **Do not rely on Claude's client
  backoff for streams.**
- Only once a `200` upstream stream is in hand do we return the `StreamingResponse` and yield.
  After the first byte, commit (surface later errors as today).
- Implementation: hoist the upstream open + status-peek + retry/select into the async handler so
  a plain error `Response` (real status) can be returned on give-up, and the `StreamingResponse`
  is constructed only on a committed `200`.

**Langfuse:** pass a `routing` block into `_emit_trace` metadata via `_log_generation`/
`_log_stream`: `{requested_model, served_model, difficulty, action:
passthrough|downgrade|shed|retry, retries, cooldown_skips}`. Keep `model=served` for cost; the
requested model lives in metadata. Reuse existing helpers unchanged (`_upstream_for`, `_is_work`,
`_forward_headers`, `_response_headers`, `_summarize_blocks`, `_usage_details`, `_merge_usage`,
`_emit_trace`).

### 3. Config (`.env.example`, aligned with the existing `SLACK_ROUTE_*` convention)
```
ROUTE_ENABLED=1                 # master switch for proactive routing (resilience always on)
ROUTE_DOWNGRADE_TIERS=trivial   # tiers eligible to downgrade
ROUTE_CHEAP_MODEL=claude-haiku-4-5
ROUTE_MAX_RETRIES=2
ROUTE_429_SHED_THRESHOLD=2
ROUTE_429_WINDOW_SECS=20
# ROUTE_POOL_FILE=/app/pool.json
```
Pass through the `proxy` service in `docker-compose.yml` / `docker-compose.work.yml` with the
same `${VAR:-default}` style as `LANGFUSE_*`. No new service.

### 4. Net-savings gate (the measurable activation criterion)
Langfuse routing metadata makes the gate concrete: compare served-model vs requested-model cost
on downgraded turns, and watch for quality regressions. Keep routing ON only if net savings
materialize. (Reviewers preferred defaulting `ROUTE_ENABLED=0` until this is demonstrated;
operator's call — default ON is acceptable *because* every downgrade is tagged and reversible via
the kill switch.)

### 5. Extensibility / Phase 3 (NOT now)
Add a cheaper model = append a pool entry. A sub-Haiku **cross-vendor** entry needs the Anthropic
⇄ provider translation leg — deferred to a LiteLLM `/v1/messages` sidecar per IDEAS #31, off by
default.

## Files
| File | Change |
|---|---|
| `proxy/routing.py` | **new** — RETRYABLE, backoff, classify_difficulty (no tool signal), load_pool, select_model, Cooldowns |
| `proxy/main.py` | wire retry + streaming status-peek restructure + model rewrite + routing metadata; read `ROUTE_*` |
| `proxy/tests/test_routing.py` | **new** — see Verification (the tools-present-still-downgrades + streaming-429 tests are mandatory) |
| `.env.example` | add `ROUTE_*` block |
| `docker-compose.yml`, `docker-compose.work.yml` | pass `ROUTE_*` to the `proxy` service |

No change to `requirements.txt` (stdlib only).

## Verification (no live-proxy restart)
1. **Unit** (`proxy/tests/`): **(a)** a trivial turn *carrying a full tools array* STILL downgrades
   (guards against defect #2); **(b)** never crosses vendor; hard/normal keep requested; work
   never routed; **(c)** streaming `429`-before-bytes is handled proxy-side (retry/shed), never a
   torn stream (guards defect #1); backoff honors `Retry-After`.
2. **Local mock upstream** (tiny stub, no real Anthropic): stream `429,then-200` → clean model
   cutover before first byte; stream `200` then mid-stream drop → error surfaced, no retry;
   non-stream `529,529,200` → 2 retries then success.
3. **Kill-switch:** `ROUTE_ENABLED=0` → outbound body byte-identical to input (diff a passthrough).
4. **Deploy is a human step.** Do **not** `docker compose build/up` the live `nexus-proxy` in this
   work — it would recreate the container all 14 agents route through. Land the code + tests +
   green unit/mock runs; a human rebuilds the proxy out-of-band.

## Risks
- Silent downgrade on a turn the user wanted premium → `trivial`-only default + every downgrade
  Langfuse-tagged; tune from real traces.
- Streaming-429 correctness → handled proxy-side (defect #1 fixed); no reliance on client backoff.
- Router never firing → difficulty ignores tool-presence (defect #2 fixed); the mandatory unit
  test encodes it.
