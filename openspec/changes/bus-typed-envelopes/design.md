## Context

The A2A bus now has a pluggable transport (`nats-a2a-bus-transport`, live on `F4HFKXH56W`). The wire carried is still flat: `orchestrator.js` builds/parses `parseAddressedLine` (`token: body`) for Slack and the NATS transport publishes `{to, from, msg, ts}`. `handleBusMessage` resolves the target and delivers `body` verbatim (prefixed `↩ from <sender>:` at post time). There is no kind, id, or correlation — so a reply is just another nudge no one can match to a question.

The codebase already has the exact pattern this change needs: **version-tolerant wire parsing**. `parsePresence` accepts v1 (bare names) and v2 (`instances`) and normalizes both; `formatPresence` emits a back-compat mirror. The typed envelope follows the same discipline — a new `v`/`kind` layer that degrades cleanly among old peers.

Agents are **not RPC servers**: they act on their turn and go idle. So "request/reply" here is **async correlation with a deadline**, not a blocking call. The delivery path is already idle-gated (`@waiting`), which fits — a `request` is delivered when the recipient is idle, it answers on its next turn with a `reply`, and the bridge routes that back to the requester.

## Goals / Non-Goals

**Goals:**
- A versioned, typed envelope (`msg`/`request`/`reply`/`event`) with an id and correlation id, carried identically across Slack and NATS.
- Backward compatible: a `kind`-less message is `msg`; existing `agent-send.sh` calls and delivered text for `msg` are unchanged.
- Async request→reply correlation with a deadline, routed by the bridge.
- An optional `POST /request` so a skill/loop/Conductor node can await a structured answer from an agent.
- Keep it behind the transport seam — one envelope, both transports.

**Non-Goals:**
- Synchronous / blocking RPC (agents reply on their turn, not inline).
- The durable outbox + hard delivery receipts (Phase A) — best-effort + JetStream durability for now; an `ack`/`receipt` kind is a follow-on.
- Capability routing (Phase C) — the `to` is still an address, not a capability.
- A schema'd/validated `body` — opaque text + an optional `meta.content_type`.

## Decisions

### The envelope shape
`{ v:1, id, ts, from, to, kind, corr?, reply_to?, body, meta? }`. `id` = a unique id (uuid-ish; generated where the send originates). `kind` ∈ `msg|request|reply|event`. `corr` is set on a `reply` = the request's `id`. `reply_to` is an address the reply should target (defaults to `from`). `body` is opaque text; `meta` is a small open map (deadline, content_type, …). **Alternative considered:** put kind/corr in `meta` and keep the top level flat — rejected; kind + corr are first-class routing fields, not metadata, and hiding them complicates the parser.

### Backward compatibility = version tolerance (the presence pattern)
Parsing normalizes three inputs to one envelope: (a) a Phase-B envelope (`v:1`); (b) the current NATS record `{to,from,msg,ts}` → `{kind:'msg', body:msg}`; (c) a bare Slack line `token: ↩ from x: y` → `{kind:'msg', body:'…'}`. So a mixed fleet (one bridge upgraded, one not) interoperates: an old bridge sees a `msg` (it ignores unknown fields), a new bridge treats an old message as `msg`. **Alternative considered:** a hard cutover to the new wire — rejected; it breaks any un-upgraded sender and contradicts the roadmap's "settle the shape without a rewrite."

### `agent-send.sh` verbs, default unchanged
`agent-send.sh <to> <msg>` stays `kind:msg`, byte-for-byte. New flags set the kind: `--request` (mints an `id`, sets `reply_to` to the sender's FQDN), `--reply <corr-id>` (sets `kind:reply`, `corr`), `--event` (`kind:event`). The flags only add fields to the `/send` JSON. **Alternative considered:** a single `--kind X` flag — kept as an internal form, but the named verbs read better at the call site and let `--reply` take the corr-id positionally.

### Bridge-side correlation with a deadline
A `request` is recorded in a correlation map `id → { requester_addr (reply_to), from, at, deadline }`. When a `reply` with `corr=id` arrives, the bridge routes its body to `requester_addr` and clears the entry. A sweep expires entries past `deadline`, emitting a synthetic `reply` of `meta.status:'timeout'` to the requester (so a waiter never hangs). **Alternative considered:** stateless correlation (encode the requester in `reply_to` and have the replier address it directly) — simpler and it IS the fallback for a plain `--reply`, but the bridge map is what enables `POST /request` to await, the timeout, and observability of in-flight requests. Do both: `reply_to` carries the address (stateless base) and the map adds await+timeout on top.

### Typed delivery rendering
Delivery renders by kind: `msg` → today's `↩ from <sender>: <body>` (unchanged); `request` → `↩ request from <sender> [id abc123]: <body>` plus a one-line hint on how to reply (`reply: agent-send.sh --reply abc123 <sender> "<answer>"`); `reply` → `↩ reply from <sender> [re abc123]: <body>`; `event` → `↩ event from <sender>: <body>`. The recipient is a Claude agent reading text, so the hint is how it learns the reply verb without new machinery. **Alternative considered:** a structured side-channel for the agent to reply — rejected for v1; text + the hint reuses the existing send path and needs no runner change.

### `POST /request` awaits the reply
`POST /request { to, body, deadline_ms }` publishes a `request` and holds the HTTP response open until the matching `reply` arrives (resolve) or `deadline_ms` elapses (resolve with `{status:'timeout'}`). This gives skills/loops/Conductor a real ask-an-agent primitive. **Alternative considered:** only fire-and-forget `request` + a separate poll endpoint — rejected; awaiting is the ergonomic win, and the deadline bounds the held connection.

### One envelope, both transports
The envelope is JSON. On NATS it's the published payload (already JSON — just more fields) and `reply_to` may be a NATS inbox subject for a fast reply path. On Slack it's serialized into the channel line (a compact `::env:: {json}` sentinel form, or the existing addressed line for `msg` to stay human-readable) and `reply_to` is an address. `handleBusMessage` + the NATS `onMessage` both parse to the same envelope and share the render+deliver+correlate code. **Alternative considered:** a NATS-only typed envelope (leave Slack flat) — rejected; the seam's whole point is parity, and Slack is still the human-readable mirror.

## Risks / Trade-offs

- **Wire compatibility during rollout.** A new-envelope message reaching an old bridge must degrade to `msg`. Mitigation: keep the addressed-line form for `msg` (old bridges parse it exactly as today); only `request`/`reply`/`event` use the new serialization, and those are new behavior anyway. Version-tolerant parse on both sides; unit tests for every legacy→envelope case.
- **Correlation map growth / leaks.** Unbounded in-flight requests. Mitigation: a cap + a deadline sweep that always resolves (timeout reply), mirroring the existing `busQueue`/`messagedPanes` sweeps.
- **Reply never comes / agent ignores the hint.** An agent may not reply. Mitigation: the deadline → timeout reply; `request` is best-effort by design; document that it's async, not guaranteed.
- **Delivered-text hint injects a command suggestion.** The reply hint contains an `agent-send.sh --reply …` line; the recipient agent might run it verbatim with a wrong answer. Mitigation: the hint is instructional text, not auto-run; phrasing makes clear it's how to reply, not what to reply.
- **Slack serialization of `request`/`reply` adds a sentinel.** Like `::nexus-presence::`/`::nexus-relay::`, an `::env::` line must never parse as an addressed delivery. Mitigation: reuse the leading-`:` sentinel guard already proven for presence/relay; unit-test the never-parse-as-delivery invariant.

## Migration Plan

1. Add the envelope build/parse + version tolerance + typed render to `orchestrator.js` (pure, unit-tested). No behavior change yet.
2. `/send` accepts optional `kind`/`corr`/`reply_to`; when absent → `msg` (unchanged). Delivery renders by kind. Both transports carry the fields.
3. `agent-send.sh` gains `--request/--reply/--event/--reply-to`; bare call unchanged. Verify a `msg` is byte-for-byte identical on the wire and in delivery.
4. Add the correlation map + deadline sweep; route a `reply` to its request's `reply_to`.
5. Add `POST /request` (await reply / timeout). Exercise agent→agent request→reply live on the NATS bridge.
6. Slack-transport parity: serialize `request`/`reply`/`event` behind an `::env::` sentinel; keep `msg` as the human-readable addressed line.
7. Rollback: the change is additive + version-tolerant; disabling is "don't use the new flags" (and a flag can gate `POST /request` + typed rendering if needed).

## Open Questions

- **`id` generation site:** `agent-send.sh` (shell — needs a uuid source) vs the bridge stamping `id` on `/send`. Leaning: the bridge stamps `id` (one place, uuid in Node), returns it to the caller so a `--request` caller learns its id.
- **Slack `msg` serialization:** keep the bare addressed line for `msg` (human-readable, old-bridge-compatible) and use `::env::` only for `request`/`reply`/`event`? Leaning yes.
- **`reply_to` on NATS:** a JetStream/durable subject (survives requester restart) vs a core-NATS ephemeral inbox (fast, lost on restart)? Leaning durable subject for parity with the bus's durability story.
- **Timeout semantics:** synthetic `reply{status:timeout}` vs a silent drop + a log? Leaning synthetic reply so `POST /request` and any awaiter always resolve.
- **Content type in `meta`:** do we standardize `meta.content_type` (text/markdown/json) now, or defer until a structured `body` is needed? Leaning: reserve the field, default `text`.
- **Relation to Phase A receipts:** is an `ack`/`receipt` just another `kind` on this envelope (so A rides B), or a separate lane? Leaning: another kind — the envelope is the substrate for receipts too.
