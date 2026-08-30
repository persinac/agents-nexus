# DeepSeek cost leg (session-class routed)

Spreads inference cost onto DeepSeek for sessions you choose, without touching the interactive
Claude Code path or `routing.py`'s Anthropic-only pool. See `IDEAS.md` #31 for the sibling design
this reuses the topology from (Gemini **outage fallback** — reactive, triggered by both Anthropic
tiers failing). This is **cost-spread** — proactive, triggered by session identity, not by failure.

## Chain

```
conductor.py mission (name="ds-<mission-id>")
    │  ANTHROPIC_BASE_URL = {PROXY}/sess/ds-<mission-id>
    ▼
nexus-proxy:4000                                    (unchanged for every other session)
    │  _is_deepseek(session_id) → true (ds- prefix)
    │  strip Authorization/x-api-key (litellm holds the DeepSeek key, not a forwarded sk-ant-…)
    │  force served_model = DEEPSEEK_MODEL  (routing.select_model / the Anthropic pool never runs)
    ▼
litellm:4000  (sidecar, internal-only, same docker network)
    │  Anthropic /v1/messages format ⇄ DeepSeek API translation
    ▼
api.deepseek.com
```

## The `ds-` convention

Same precedent as `bg-` sessions opting into `BG_CEILING_ENABLED` (`docs/model-routing.md`) — a
session opts in by prefixing its own name. `conductor.py`'s `_set_sess(name)` already tags every
mission's session (`ANTHROPIC_BASE_URL={PROXY}/sess/{name}`); set `CONDUCTOR_DEEPSEEK=1` to prefix
every mission with `DEEPSEEK_SESSION_PREFIX` (default `ds-`). This is deliberately blunt for a first
cut — all conductor missions, not per-role — same "start blunt, tune from real data" pattern already
used for `ROUTE_DOWNGRADE_TIERS` and `BG_CEILING_MODEL`.

A `ds-`-prefixed session with `DEEPSEEK_ENABLED=0` (the default) behaves exactly as before — the
prefix alone does nothing until the kill switch is flipped.

## Why session-class, not per-turn difficulty

The obvious trigger — extend `classify_difficulty`/`ROUTE_POOL` with a DeepSeek entry — was rejected.
`docs/model-routing.md`'s bg-ceiling section measured that **99.3% of interactive turns classify
"hard"** (Claude Code resends the full transcript every turn), so a difficulty-gated DeepSeek tier
would almost never fire. It would also require breaking `routing.py`'s documented, tested invariant
that the pool is Anthropic-only. Session-class gating avoids both problems: `_decide_served` checks
`_is_deepseek(session_id)` **first**, unconditionally, before the classifier carve-out or the bg-
ceiling — a `ds-` session never reaches `routing.select_model`, `select_classifier_model`, or
`select_bg_ceiling` at all.

## Explicit non-goal: no silent fallback to Anthropic

If the litellm sidecar or DeepSeek itself fails, the request does **not** retry against
`PERSONAL_UPSTREAM`/`WORK_UPSTREAM`. That would spend real Anthropic dollars on a session explicitly
routed away from Anthropic for cost reasons — invisibly. Both `_nonstream_response` and
`_stream_response` treat a deepseek session like a work session for shedding purposes (`if (is_work
or is_deepseek) and not is_classifier: break`): retries are bounded and happen only against
`LITELLM_UPSTREAM`, then the failure surfaces to the caller. `conductor.py`'s own mission-level retry
logic decides what to do next — including, if you want it, retrying the same mission without the
`ds-` prefix.

This also means a `ds-` session is never treated as an auto-mode classifier call
(`_is_classifier_call` returns `False` for it outright) — the classifier carve-out exists to pin
calls to a tier in the Anthropic pool, which a deepseek session must never touch.

## Cost accounting gap (fast-follow, not blocking)

`NEXUS_PRICES_PATH`'s price table has no DeepSeek entry yet. Until one is added, `_turn_usd`'s
"unknown model reads as 0" fallback means DeepSeek spend shows as **$0** in nexus's own per-session
USD ceiling accounting (`GET /admin/ceilings`) — token counts and Langfuse's own cost view are
unaffected. Langfuse tags the served model as `DEEPSEEK_MODEL` (default `deepseek-cheap`) with
`action: vendor-route`, distinct from `downgrade`/`classifier`/`passthrough`, so DeepSeek spend is
still separable from Anthropic routing in the trace data even before the price entry exists.

## Bringing it up

Requires the `proxy` compose profile (brings up the `litellm` sidecar alongside `nexus-proxy`):

```
DEEPSEEK_API_KEY=...        # in .env — consumed by litellm, never nexus-proxy itself
DEEPSEEK_ENABLED=1
docker compose --profile proxy up -d litellm proxy
```

Smoke test: hit `nexus-proxy:4000/sess/ds-smoketest/v1/messages` directly, or run a mission with
`CONDUCTOR_DEEPSEEK=1`, and confirm the response comes back in Anthropic Messages-API shape with the
Langfuse trace tagged `deepseek-cheap` / `vendor-route`. Hit the same proxy without the `ds-` prefix
and confirm zero behavior change — the regression check on the primary invariant.

## Out of scope here

`minions`' external-worker path (`claim_engineer_work`/`report_pr`) doesn't go through
`claude_agent_sdk` (Anthropic wire format) at all, so it doesn't need this sidecar or its
translation — a DeepSeek-based herder worker there would call DeepSeek's API directly. Worth doing,
but a separate, smaller, independent piece of work.
