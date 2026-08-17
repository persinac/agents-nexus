## Status

**Proposed. Not implemented.** Carries forward the genuinely-unshipped items from
`nats-a2a-bus-transport` (5.1, 5.2, 5.4, 4.5), which was archived 2026-08-17 as complete — the
*cutover* is complete; ack-on-idle was always a post-cutover hardening item and archiving it
would have taken the only tracked record of this gap out of `openspec validate`'s view.

Today's behavior is **ack-on-receive**, and it works: the fleet is live on NATS and messages are
delivered. What is missing is durability of a *hold* — see `proposal.md` for the exact line of
code and what it costs.

## 1. Settle-point refactor (the core)

- [ ] 1.1 Remove the unconditional `msg.ack()` from the NATS subscribe callback in `index.js` (~2610); the callback's job ends at `routeEnvelope`
- [ ] 1.2 Thread the `JsMsg` handle through `routeEnvelope` → `deliverIdleGated` so every terminal path can settle it
- [ ] 1.3 `deliverIdleGated`: `ack` after a successful `deliverToPane`; on `enqueueBus`, settle nothing (the message is now held)
- [ ] 1.4 `term()` when the target resolves to another host (never ours — redelivery cannot help) and keep the existing `term()` for an undecodable envelope
- [ ] 1.5 Settle-exactly-once audit: every `return` in the delivery path either settles or documents why the message stays held

## 2. Held-message lifecycle

- [ ] 2.1 `busQueue` entries carry `{ target, body, at, msg }`; `msg` may be absent (Slack path, `--local`) and every settle call site must tolerate that
- [ ] 2.2 `flushBusQueue`: `ack` on successful inject; leave held (unsettled) on a failed inject, as today
- [ ] 2.3 Extension sweep beside `flushBusQueue`: `working()` on each held message every ~`NATS_A2A_ACK_WAIT_MS / 3`
- [ ] 2.4 Extension ceiling: after N extensions (default ≈1 hour of holding), `nak` and drop from `busQueue` — the stream re-schedules it
- [ ] 2.5 Queue-cap eviction `nak`s instead of `shift()`-and-discard; update the `(still in channel)` log line, which is a false Slack-era claim under NATS
- [ ] 2.6 Dead-pane cleanup `nak`s the held messages instead of deleting the queue

## 3. Bare-name single owner (was 4.5)

- [ ] 3.1 Resolve a bare target to an FQDN via the presence KV at publish time (`/agents` already exposes a paste-ready `fqdn` per entry)
- [ ] 3.2 Shared queue group on the consumer as the resolve-race safety net
- [ ] 3.3 Test: two hosts claiming one bare name deliver exactly once, with a hold in play on both

## 4. Tests

- [ ] 4.1 Unit: the settle matrix from `design.md` — injected/busy/evicted/dead-pane/foreign-host/undecodable each settle exactly once, with the right verb
- [ ] 4.2 Unit: an extension sweep calls `working()` on held messages and stops at the ceiling with a `nak`
- [ ] 4.3 Unit: eviction past `BUS_QUEUE_MAX` `nak`s rather than drops
- [ ] 4.4 Integration (was 5.4): enqueue for a busy agent, restart the bridge, assert the message is redelivered and still delivered on idle — **the test this whole change exists for**
- [ ] 4.5 Integration: a message held across a `working()` cycle is not prematurely redelivered

## 5. Docs

- [ ] 5.1 `docs/slack-bridge.md` — the ack lifecycle and the settle matrix
- [ ] 5.2 `docs/slack-to-nats-cutover.md` — drop the "ack-on-receive" caveat once this lands
- [ ] 5.3 Remove the follow-up note in the `index.js` subscribe comment (~2606-2609) that points here
