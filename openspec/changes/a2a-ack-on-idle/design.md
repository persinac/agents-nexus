## Context

The transport seam (`nats-a2a-bus-transport`) shipped with **ack-on-receive** as a deliberate
simplification: get the fleet onto NATS, keep the existing in-memory idle gate, harden later.
"Later" is now, and the gap is narrow but real — durability stops at the bridge's front door.

Two independent mechanisms currently overlap and must be merged into one:

| | JetStream consumer | in-memory `busQueue` |
|---|---|---|
| holds for | a recipient that hasn't acked | a pane that isn't `@waiting=2` |
| survives restart | yes | **no** |
| bound | `max_deliver` (100) | `BUS_QUEUE_MAX` per pane, drop-oldest |
| on give-up | redeliver, then dead-letter | silent drop + a log line |

The change is to make the second a *view* of the first rather than a parallel copy.

## Goals / Non-Goals

**Goals.** A held message survives a bridge restart. A bounded queue evicts by returning work to
the stream, not by destroying it. A long hold does not get redelivered underneath us.

**Non-Goals.** Changing when we *inject* (`@waiting=2` + human-typing guard stay exactly as they
are). Synchronous delivery receipts — that is Phase A. Touching the Slack A2A path, which is
deprecated.

## Decisions

### Ack at the settle point, not the parse point

`ack`/`nak`/`term` move out of the subscribe callback and become the terminal operation of the
delivery state machine. Every path must settle exactly once:

| outcome | settle | why |
|---|---|---|
| injected at idle prompt | `ack` | done |
| recipient busy | *(none yet)* + periodic `working()` | still ours, lease held |
| evicted at queue cap | `nak` | back to the stream, redelivered later |
| pane died while holding | `nak` | another attempt / another host may own it |
| not addressed to this host | `term` | never ours; redelivery cannot help |
| undecodable envelope | `term` | already the behavior in `nats-transport.js` |
| held past max extensions | `nak` | let the stream re-schedule rather than pin it here |

**The invariant to test is "settled exactly once on every path."** The failure mode this change
introduces — and the reason it was deferred rather than dashed off — is a message that is neither
acked nor naked, which pins the consumer's ack floor and stalls the subject. `max_deliver: 100`
bounds a poison message, but it does not bound a *leaked* one.

### Lease extension: `working()` on a cadence, with a ceiling

`ackWaitMs` defaults to 5 minutes; an agent can easily be busy longer. A sweep beside the existing
`flushBusQueue` calls `working()` on each held message at roughly `ackWaitMs / 3`, which is the
usual safety factor for a heartbeat against a lease.

The ceiling matters more than the cadence. Extending forever converts "durable hold" into "this
one process holds this message until it dies" — which is the exact property we are trying to
remove. After N extensions (default: enough to cover ~1 hour) the message is `nak`ed. It returns
to the stream, and if the recipient is still busy on redelivery it is simply held again, by
whichever bridge instance is alive then. `max_deliver: 100` remains the poison backstop.

### Eviction returns work to the stream

`enqueueBus`'s drop-oldest exists so one perpetually-busy agent cannot grow memory without bound.
That reasoning survives; only the disposal changes. `nak` the evicted message instead of
`shift()`ing it into the void. The queue stays bounded, and the message is redelivered rather
than destroyed.

This also lets the log line stop lying. Today's `— dropped oldest (still in channel)` is a
Slack-era claim: there is no `#nexus-agents` fallback under NATS to go read it back from.

### Bare names must resolve before holds get long

Today an empty host defaults to `selfHost`, which was safe when holds were sub-second. Once a
message can be held for an hour, two hosts claiming the same bare name can both hold a copy of
it, and both eventually inject. Resolve the bare name to an FQDN via the presence KV at publish
time (the paste-ready `fqdn` is already on every `/agents` entry), and keep a shared queue group
as the safety net for the resolve-race window.

## Risks / Trade-offs

- **A leaked message stalls a subject.** Mitigated by the settle-exactly-once test matrix above,
  the extension ceiling, and `max_deliver`. This is the risk that justifies the whole design note.
- **Memory profile changes shape.** Holding `JsMsg` handles rather than strings keeps more alive
  per queued item. `BUS_QUEUE_MAX` still bounds it, and the ceiling bounds duration.
- **Redelivery is not ordered.** A `nak`ed message re-enters the stream and may arrive after a
  newer one. A2A is already unordered per-sender across hosts, so this does not regress a
  property anyone had.

## Open Questions

- Should an evicted (`nak`ed) message be delayed on redelivery (`nak(delay)`) to avoid a hot loop
  against an agent that is busy for hours? Leaning yes, with a backoff derived from how many
  times it has already been redelivered.
- Does the human-typing guard deserve a *shorter* extension ceiling than the busy-agent case? A
  human mid-draft is a seconds-to-minutes condition; an agent mid-task is minutes-to-hours.
