## Status

Proposed (design complete). Built on the shipped transport seam (`nats-a2a-bus-transport`,
live on F4HFKXH56W). Additive + version-tolerant — inert for callers that don't use the new
flags. Not yet implemented.

## 1. Envelope (pure, in orchestrator.js)

- [ ] 1.1 `buildEnvelope({from,to,kind,body,corr,reply_to,meta})` → `{v:1,id,ts,...}` (id/ts stamped where noted in §2.1)
- [ ] 1.2 `parseEnvelope(input)` — version-tolerant: a `v:1` envelope; the legacy `{to,from,msg,ts}` record → `{kind:'msg',body:msg}`; a bare addressed line `token: ↩ from x: y` → `{kind:'msg',body}`. Unknown fields ignored
- [ ] 1.3 `renderDelivery(envelope)` → the agent-visible text per kind (`msg` = today's `↩ from <sender>: <body>`; `request` adds id + reply hint; `reply`/`event` labelled)
- [ ] 1.4 Slack serialization: `msg` stays the bare addressed line; `request`/`reply`/`event` use an `::env::` sentinel that never parses as an addressed delivery (reuse the presence/relay sentinel guard)
- [ ] 1.5 Unit tests in `orchestrator.test.js`: every legacy→envelope case; round-trip; kind rendering; the `::env::` never-parses-as-delivery invariant

## 2. Bridge (/send + delivery + correlation)

- [ ] 2.1 `/send` accepts optional `kind`/`corr`/`reply_to`; bridge stamps `id` + `ts` and returns `id` to the caller; absent kind → `msg` (unchanged)
- [ ] 2.2 Publish the envelope via the active transport; `handleBusMessage` + NATS `onMessage` both `parseEnvelope` → `renderDelivery` → existing deliver path
- [ ] 2.3 Correlation map `id → {reply_to, from, at, deadline}` for outstanding requests; a `reply` with matching `corr` routes its body to `reply_to`, then clears
- [ ] 2.4 Deadline sweep (mirrors the busQueue/messagedPanes sweeps): expire past-deadline requests with a synthetic `reply{meta.status:'timeout'}` to the requester; map cap
- [ ] 2.5 `POST /request { to, body, deadline_ms }` — publish a `request`, hold the response until the matching `reply` or the deadline, resolve with the body or `{status:'timeout'}`

## 3. Transports

- [ ] 3.1 NATS: publish/consume the full envelope (already JSON — add the typed fields); optional durable `reply_to` inbox subject
- [ ] 3.2 Slack: serialize/deserialize the `::env::` sentinel form for non-`msg` kinds; keep `msg` as the human-readable line

## 4. agent-send.sh verbs

- [ ] 4.1 `--request` (mint via bridge id; `reply_to` = sender FQDN), `--reply <corr-id>`, `--event`, `--reply-to <addr>` → add fields to the `/send` JSON
- [ ] 4.2 Bare `agent-send.sh <to> <msg>` unchanged — verify byte-for-byte identical wire + delivery for `msg`

## 5. Config & docs

- [ ] 5.1 Env: `SLACK_BUS_REQUEST_TTL_MS` (default deadline), correlation-map cap
- [ ] 5.2 Document the envelope + verbs + `/request` in `docs/slack-bridge.md`; update `docs/agent-bus-roadmap.md` Phase B status

## 6. Verify

- [ ] 6.1 Unit: legacy compatibility + rendering + sentinel invariants (§1.5) green in `npm test`
- [ ] 6.2 Integration (live broker): a `request` → `reply` round-trips and correlates; a `timeout` resolves; extend `nats-transport.itest.mjs` (isolated prefix)
- [ ] 6.3 Live on the NATS bridge: agent A `--request` agent B → B replies → A receives the correlated reply; `POST /request` awaits + times out
- [ ] 6.4 Back-compat: a bare `msg` send is unchanged end-to-end (delivered text identical)
