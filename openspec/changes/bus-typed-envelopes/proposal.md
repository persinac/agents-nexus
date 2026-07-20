## Why

The A2A wire is flat and untyped: on Slack a delivered line is `to: ↩ from <sender>: <text>`; on NATS the envelope is `{to, from, msg, ts}`. Every message is a fire-and-forget nudge — there is no message **kind**, no message **id**, and no way to **correlate a reply to a request**. So an agent cannot ask another agent a question and programmatically match the answer, the runtime cannot tell an event from a directive from a reply, and there is no basis for delivery receipts or RPC.

This is roadmap **Phase B** (typed envelopes + request/reply). The transport seam already landed early (Phase G, `nats-a2a-bus-transport`, now live on `F4HFKXH56W`), so settling the message *shape* is the natural next step — and, as the roadmap notes, doing B behind the seam is a wire change, not a rewrite. A typed envelope is also the prerequisite for Phase A receipts and Phase C capability routing.

## What Changes

- **Versioned typed envelope.** A2A messages carry `{ v, id, ts, from, to, kind, corr, reply_to, body, meta }`. `kind` ∈ `msg` (today's fire-and-forget), `request` (expects a reply), `reply` (answers a request; `corr` = the request's `id`), `event` (notification, no reply). `id` is a unique message id; `reply_to` is where a reply should be addressed.
- **Backward compatible, default `msg`.** A legacy line/envelope with no `v`/`kind` parses as `kind: msg` — the same version-tolerant pattern presence already uses (v1/v2). Existing `agent-send.sh` calls are unchanged and keep sending `msg`.
- **`agent-send.sh` verbs.** New optional flags: `--request` / `--reply <corr-id>` / `--event` (and `--reply-to <addr>`), which set the envelope `kind`/`corr`/`reply_to`. Bare `agent-send.sh <to> <msg>` stays `msg`, byte-for-byte.
- **Typed delivery rendering.** The bridge renders the envelope into the agent-visible text with a kind marker and, for a `request`, the correlation hint the recipient needs to reply (e.g. `[request abc123 · reply: agent-send.sh --reply abc123 <from> …]`). The `↩ from <sender>:` prefix is preserved for `msg`.
- **Async request/reply correlation.** The bridge tracks outstanding requests (`id → { requester reply_to, deadline }`) so a `reply` routes back to the requester, with a timeout that emits a `reply` of kind `timeout` (or drops with a log) when no answer arrives. This is **async** correlation (agents reply on their next turn), not synchronous RPC.
- **Optional programmatic RPC surface.** A localhost `POST /request { to, body, deadline_ms }` that publishes a `request` and resolves when the matching `reply` arrives (or the deadline elapses) — so a skill/loop/Conductor node can call one agent and await a structured answer.
- **Transport-agnostic.** The envelope rides the existing transport seam: on NATS, `reply_to` is a subject/inbox and correlation can use JetStream; on Slack, `reply_to` is the from-addr and correlation is the in-memory map. Both transports carry the same envelope.
- **Out of scope:** synchronous/blocking RPC (agents are not servers), the durable outbox + hard receipts (Phase A), capability-based routing (Phase C), and any structured/schema'd `body` beyond opaque text + a `content-type` hint in `meta`.

## Capabilities

### New Capabilities
- `agent-bus-typed-envelope`: A versioned A2A envelope (`v, id, ts, from, to, kind, corr, reply_to, body, meta`) with kinds `msg`/`request`/`reply`/`event`, backward-compatible parsing (a `kind`-less message is `msg`), carried unchanged across the Slack and NATS transports.
- `agent-bus-request-reply`: Async request/reply on top of the envelope — `agent-send.sh --request/--reply/--event`, bridge-side correlation of a `reply` to its `request` with a deadline/timeout, typed delivery rendering that gives the recipient the correlation hint, and an optional `POST /request` that awaits the reply.

### Modified Capabilities
- `agent-bus-transport`: `/send` and the transport `publish`/inbound now carry the typed envelope instead of a flat `{to,from,msg}` line/record; delivery renders by `kind`. The default (`msg`) delivered text is unchanged, so existing behavior is preserved.

## Impact

- **Code:** `orchestrator.js` — envelope build/parse + version tolerance + typed delivery-line rendering (pure, unit-tested beside `parseAddressedLine`/presence). `slack-bridge/index.js` — `/send` accepts `kind`/`corr`/`reply_to`; a correlation map + timeout sweep; typed rendering in the delivery path; optional `POST /request`. `slack-bridge/transports/nats-transport.js` — publish/consume the envelope (already JSON on NATS; add the typed fields) + optional reply-to inbox. `tmux/mac/tmux-scripts/agent-send.sh` — the `--request/--reply/--event/--reply-to` flags (bare call unchanged).
- **Config:** request deadline default (e.g. `SLACK_BUS_REQUEST_TTL_MS`), correlation-map cap.
- **Compatibility:** version-tolerant on the wire (mixed old/new senders interoperate; old = `msg`). Inert for callers that don't use the new flags.
- **Depends on / relates to:** the transport seam (`nats-a2a-bus-transport`, shipped). Unlocks Phase A receipts (an `ack`/`receipt` kind) and Phase C routing (a `capability` target) as follow-ons.
