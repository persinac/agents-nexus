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
  same-model transient retry only. One scoped exception: auto-mode classifier calls (§6).
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
ROUTE_CLASSIFIER=1                  # shield auto-mode classifier calls (§6; own switch)
ROUTE_CLASSIFIER_TIER=sonnet        # floor tier those calls may be served from
ROUTE_CLASSIFIER_WORK=1             # include work sessions (model only, never the upstream)
ROUTE_CLASSIFIER_MAX_RETRIES=4
ROUTE_CLASSIFIER_CHEAP_MAXTOK=150000
```
Pass through the `proxy` service in `docker-compose.yml` / `docker-compose.work.yml` with the
same `${VAR:-default}` style as `LANGFUSE_*`. No new service.

**Runtime toggle (no restart).** `ROUTE_ENABLED`/`ROUTE_DOWNGRADE_TIERS` are read live, so an
`/admin/route` endpoint flips them without recreating the container — which matters because the
proxy is the whole fleet's gateway and a rebuild blips every agent for the ~seconds it takes to
swap. Registered *before* the catch-all so it is served locally, not proxied upstream:
```
GET  /admin/route                     → {enabled, downgrade_tiers, max_retries, pool, cooldowns_active}
POST /admin/route  {"enabled": true}  → applies on the next request; ephemeral (reverts on restart)
```
The env value is the boot default; the runtime override lives in memory (single uvicorn worker →
one shared view). Set `ROUTE_ADMIN_TOKEN` to require an `x-route-admin-token` header on the POST.

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

### 6. Auto-mode classifier carve-out (added 2026-08-14)

**Problem.** Claude Code's auto permission mode adjudicates every mutating tool call with its own
`/v1/messages` call, and that call inherits the session model. With `model: opus[1m]` pinned in
`~/.claude/settings.json`, every permission decision depended on the scarcest capacity pool in the
account — and the client **fails closed**: on a classifier error it denies the tool call rather than
retrying or prompting (`Auto mode classifier unavailable, denying with retry guidance (fail
closed)`, verified in the 2.1.232 binary). One 529 therefore reads to the agent as "Bash is not
allowed" while read-only tools keep working, which is the symptom that motivated this change.

Not the context-exhaustion failure recorded in the memory stack: 2.1.232 separates the two — a
too-long classifier transcript *falls back to prompting*, an unavailable classifier *denies*.

**Detection.** `routing.is_classifier_request(body_bytes)` — byte-substring test for either
`=== ACTION BEING CLASSIFIED ===` or `<cc_automode_permissions>`, both stable literals in the
2.1.232 classifier prompt. Raw bytes, so no parse step and nothing that can raise on the hot path.

**Selection.** `routing.select_classifier_model()` pins the call to the cheapest Anthropic model at
or above `ROUTE_CLASSIFIER_TIER` (default `sonnet`, matching the client's own
`getClassifierSonnet5Default`) that costs no more than the requested model and is not in cooldown.
It deliberately does **not** consult `classify_difficulty`: a classifier transcript carries much of
the session, so difficulty always reads `hard` and the cost router would never fire on the one call
that most needs to leave the session's capacity pool.

**Not gated on `ROUTE_ENABLED`** — this is a resilience control, not a cost control. Its own kill
switch is `ROUTE_CLASSIFIER=0`, which restores byte-for-byte prior behaviour.

**Two failure modes it must not introduce:**
- *Transcript too large for the cheaper model.* Guarded twice: skip any candidate the estimated
  transcript would not fit (`TIER_CONTEXT` — haiku 200k, sonnet/opus 900k), and if a downgraded call
  still 400s, retry once on the requested model (`action: classifier-revert`) and stop shedding. A
  permanent 400 denies the tool call outright — strictly worse than the transient failure being
  avoided.

  **This started as one global 180k cap and that was wrong.** Live stage-2 calls on this fleet
  estimate ~183k tokens, so the cap pinned them straight back onto the unavailable session model —
  the long sessions needing the carve-out most were exactly the ones it excluded. Per-tier windows
  fix that, since the sonnet floor holds 183k with room to spare.
  `ROUTE_CLASSIFIER_CHEAP_MAXTOK` survives as an optional stricter global ceiling (0 = per-tier
  only). The `classifier call kept on …` log line is how you catch a recurrence.
- *429 becoming a denial.* For classifier calls the proxy owns 429 on the non-stream path too
  (`_is_retryable`). Everywhere else 429 is still surfaced verbatim so Claude Code's HTTP backoff
  runs; for a classifier call there is no backoff to trigger, only a denial.

**Work sessions are included** (`ROUTE_CLASSIFIER_WORK=1`) — the scoped exception to the
always-passthrough invariant. Only `body["model"]` changes, never the upstream, so corp gateway auth
and attribution are untouched, and a permission classification is not the main-loop turn whose
quality that invariant protects. Such a call may also shed down-ladder on a work session, for the
same reason. `ROUTE_CLASSIFIER_WORK=0` exempts work.

**Langfuse.** `action` is `classifier` when the model was repinned and `classifier-revert` after a
400 fallback; `difficulty` is `classifier`. `GET /admin/route` reports the whole block under
`classifier`.

### 7. Host-side backstop: `automode-watchdog` (added 2026-08-17)

This carve-out cuts the failure rate but doesn't eliminate it — nexus-proxy logs can show every
classifier call correctly served on sonnet-5 and the denial still happens (sonnet gets stressed
too, or the proxy's own retry/backoff pushes latency past whatever timeout Claude Code enforces
client-side). Real incident: `svc-chatbot` hit 13 fail-closed denials in 40 minutes on 2026-08-17
while the proxy was routing every classifier call correctly the whole time. Nothing alerted a
human or retried the call — no hook fires for this denial at all.

`tmux/mac/tmux-scripts/automode-watchdog.py` is the host-side backstop: it tails each live pane's
own transcript file for the `toolDenialKind` field Claude Code stamps on the denial, and on the
first one cycles that pane to Manual permission mode (not `bypassPermissions` — confirmed
unreachable from a live session) so it can keep working via the existing `notify-classify.py` ask
gate instead of the classifier, then reverts once idle. Full design rationale, the confirmed
`toolDenialKind` values, and the mode-cycling mechanics are documented in the script's own
docstring — not duplicated here. Launchd unit:
`launchd/com.agents-nexus.automode-watchdog.plist`.

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
