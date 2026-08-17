## Why

The `nats-jetstream-a2a-bus` capability already specifies **ack-based idle-gated delivery**: a
message is not acknowledged until it has been injected at an idle prompt, is held un-acked while
the recipient is busy, and survives a bridge restart mid-hold. That requirement shipped as spec
and **was never implemented.** The live code acks on receive:

```js
// slack-bridge/index.js — the NATS subscribe callback
finally { try { msg.ack(); } catch { /* connection draining */ } }
```

The message is acknowledged the instant it is parsed and routed, before `deliverIdleGated` has
decided anything. If the recipient is busy, the body is copied into the **in-memory** `busQueue`
and the JetStream message is already gone. So today:

- **A hold does not survive a restart.** `busQueue` is a `Map` in process memory. Restart the
  bridge while an agent is mid-task and every message queued for it is lost — silently, because
  the stream considers them delivered.
- **The queue cap drops messages outright.** `enqueueBus` shifts the oldest off the front past
  `BUS_QUEUE_MAX`, and `flushBusQueue` deletes the whole queue when a pane dies. Both log
  "still in channel" — a Slack-era consolation that is no longer true: under NATS there is no
  channel to go read it back out of.
- **The spec is lying.** Anyone reading `nats-jetstream-a2a-bus` believes A2A is restart-durable
  end to end. It is durable up to the bridge's front door and best-effort after it.

This was tracked as tasks 5.1/5.2/5.4 (plus 4.5) inside `nats-a2a-bus-transport`, which has now
been **archived as complete** — correctly, since the cutover itself is done. Archiving it moved
the only record of this gap out of `openspec validate`'s view. This change gives that genuinely
unshipped work its own home.

It also **sharpens the requirement**, which is under-specified in precisely the ways that made it
easy to defer: it says the lease is "extended" without saying by what or on what cadence, and it
says a held message is never lost without reconciling that against a bounded queue and a
recipient pane that can die while holding.

## What Changes

- **Ack moves to the delivery point.** The subscribe callback stops acking. `ack` / `nak` / `term`
  become the responsibility of whatever finally decides the message's fate: injected → `ack`,
  undeliverable-but-retryable → `nak`, permanently undeliverable → `term`.
- **The hold carries its JetStream message.** `busQueue` entries gain the `JsMsg` handle so a
  queued item can be acked when it is eventually flushed. The queue becomes a *view* of
  outstanding un-acked work rather than a private copy of the text.
- **Lease extension while held.** A periodic `working()` on every held message, on a cadence
  derived from `ackWaitMs` (default 5 min), so a legitimately long hold is not redelivered
  underneath us. A message whose recipient stays busy past a bounded number of extensions is
  `nak`ed back to the stream rather than held forever in one process.
- **Queue-cap eviction becomes `nak`, not drop.** Past `BUS_QUEUE_MAX`, evict by `nak`ing —
  the message returns to the stream and is redelivered later instead of vanishing. Same for a
  dead pane: `nak` the held messages so another delivery attempt (or another host, post-4.5) can
  take them.
- **Restart is a no-op for correctness.** Nothing acked means nothing lost; on reconnect the
  consumer redelivers the held set and the bridge re-holds it.
- **Bare-name single-owner resolution (task 4.5).** An empty host currently defaults to
  `selfHost`. Resolve a bare name to an FQDN via presence, with a queue group as the race safety
  net, so a bare target cannot double-deliver once holds are long-lived.
- **Out of scope:** the Slack transport's idle gate (Slack A2A is deprecated and its path stays
  ack-less), typed-envelope changes (`bus-typed-envelopes` owns the wire), and any change to the
  `@waiting=2` / human-typing gate semantics themselves — this changes *when we ack*, not *when
  we inject*.

## Capabilities

### Modified Capabilities

- `nats-jetstream-a2a-bus`: the **Ack-based idle-gated delivery** requirement gains the lease,
  eviction, and pane-death behavior it was missing, and is restated so it can be verified rather
  than assumed. The **Single-owner delivery for duplicate names** requirement gains the presence
  lookup that makes bare names safe under long holds.

## Impact

- **Code:** `slack-bridge/index.js` — the subscribe callback stops acking; `enqueueBus` /
  `flushBusQueue` / `deliverIdleGated` carry and settle the `JsMsg`; a `working()` sweep beside
  the existing flush and reaper sweeps. `slack-bridge/transports/nats-transport.js` — no
  signature change (`onMessage` already receives `m` with `ack`/`working`/`nak`/`term`); possibly
  a queue group for 4.5.
- **Config:** an extension-cadence and max-extensions bound, both derived from the existing
  `NATS_A2A_ACK_WAIT_MS` / `NATS_A2A_MAX_DELIVER` rather than adding new knobs where avoidable.
- **Risk:** this is the one change that can *stall* the bus — a message that is never acked and
  never naked pins the consumer. The poison bound (`max_deliver`, already set to 100) is the
  backstop, and the design note covers the stall modes explicitly.
- **Compatibility:** invisible to agents and to `agent-send.sh`. No wire change.
- **Depends on:** `nats-a2a-bus-transport` (shipped, archived 2026-08-17). Independent of
  `bus-typed-envelopes`.
