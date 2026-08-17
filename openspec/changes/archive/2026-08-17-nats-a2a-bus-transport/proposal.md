## Why

The A2A bus rides on Slack: each host's bridge holds a Socket Mode connection to a Slack app and every inter-agent message transits a shared `#nexus-agents` channel. That was the right bootstrap — a free durable log, a human UI, and a presence substrate in one — but it does not scale to a company-wide fleet:

- **Socket Mode caps concurrent connections per app (~10).** One shared app cannot hold hundreds of bridge sockets, so the design is forced into one Slack app per participant. This is the "1000 bots" bottleneck.
- **One app per participant** means hundreds of Slack apps to create and get security-approved in a Vanta-audited workspace — a provisioning and audit-surface burden.
- **`chat.postMessage` rate limits (~1 msg/s/channel)** throttle a chatty mesh, and every host re-ingests every message off one channel (fleet-wide fan-out) rather than receiving only its own traffic.
- **Agent output lands in Slack** — a PHI-adjacent audit liability for a healthcare org.

The bridge already separates **transport** (publish via `chat.postMessage`; subscribe via `SocketModeClient`) from **routing + delivery** (FQDN addressing in `orchestrator.js`, presence/owner-election, and the `deliverToName/Slot/Pane` layer over send-keys / SDK inbox). So the medium can be swapped without touching addressing or delivery. This change introduces a NATS + JetStream transport as the durable A2A store, selected by a flag, and leaves the human notify/reply leg on Slack.

## What Changes

- **Pluggable transport seam.** A `NEXUS_BUS_TRANSPORT={slack|nats}` flag (default `slack`) selects the A2A medium behind a small transport interface (`publish` / `subscribe` / presence / lifecycle). `SlackTransport` wraps today's behavior byte-for-byte; `NatsTransport` is new. Routing, presence semantics, the delivery layer, and `@waiting` idle-gating stay medium-agnostic and unchanged.
- **NATS subjects for FQDN addressing.** A2A messages publish to a deterministic subject derived from the target FQDN — `nexus.a2a.<host>.<workspace>.<name>`. Each bridge subscribes only to its own host subtree (`nexus.a2a.<self-host>.>`), so the broker routes point-to-owner instead of fanning every message out to every host.
- **JetStream stream as the durable inbox.** A stream persists A2A traffic; a recipient host that is offline receives its buffered messages on reconnect from its durable consumer cursor — no loss, no re-processing. This also becomes the replay/audit record (retention-bounded), replacing "Slack channel history."
- **Ack-based idle-gate.** The idle-gate is expressed as JetStream acknowledgement: a message is not acked until it is delivered to the recipient at an idle prompt (`@waiting=2`); a busy recipient's message stays held in the stream and survives a bridge restart — closing the in-memory-queue restart gap the Slack bus left deferred.
- **Single-owner delivery for duplicate names.** FQDN-qualified targets route to exactly one host subject (the subject *is* the owner). A bare name claimed by multiple hosts resolves to an FQDN via presence, with a shared queue group as the race safety net — replacing the lexically-smallest-host `ownerOf` election.
- **Presence via JetStream KV.** A KV bucket keyed by FQDN with per-entry TTL replaces the channel gossip protocol; any bridge builds the reachability directory (`/agents`) and resolves bare names from it, and a departed agent's entry expires without a tombstone.
- **Human-facing legs stay on Slack.** `/notify`, thread-tracking, the human `#nexus` round-trip (`resolveRequest`, dormant-agent nudge cards), and `/relay` (agent→human) remain on the Slack `WebClient`. Only the A2A path (`/send`, presence, `handleBusMessage` inbound) moves to the transport seam.
- **`agent-send.sh` is unchanged.** It still POSTs `:8788/send`; transport selection is entirely bridge-side.
- **Out of scope:** NATS cluster HA / multi-region (single node first), a self-serve credential-issuing UI (rollout uses minted creds), and any change to the delivery layer, the spawn branch, or the human-control routing.

## Capabilities

### New Capabilities
- `agent-bus-transport`: A pluggable transport seam in the bridge selected by `NEXUS_BUS_TRANSPORT`, decoupling A2A publish/subscribe/presence from routing, delivery, and idle-gating so the medium can be swapped (and dual-run, then rolled back) without touching addressing or the delivery layer.
- `nats-jetstream-a2a-bus`: A NATS + JetStream A2A transport — subject-based FQDN addressing with per-host subscription, a JetStream stream as the durable inbox for offline recipients + audit, ack-based idle-gated delivery, single-owner delivery for duplicate names, and TTL KV presence. Scales past the Slack per-app connection cap and onboards a participant with a credential rather than a messaging app.

### Modified Capabilities
- `slack-agent-bus`: The A2A publish path, the cross-host delivery mechanism, and the buffered-delivery durability guarantee become transport-mediated. `/send` publishes via the active transport; cross-host delivery is broker-routed under NATS (no fleet-wide fan-out); buffered delivery is backed by JetStream ack and survives a bridge restart. External contracts (owner delivers, non-owners don't; busy recipients are not interrupted) are preserved.

## Impact

- **Code:** `slack-bridge/index.js` — extract a `Transport` interface; refactor `/send`, presence publish/consume, and the `handleBusMessage` inbound feed to go through it; `SlackTransport` (existing behavior) + `NatsTransport` (new). New `slack-bridge/transports/` (or similar) module. `orchestrator.js` — add a tested FQDN↔subject codec beside `parseAddress`/`qualifyFrom`; extend `orchestrator.test.js`. No change to `agent-send.sh`, the delivery helpers, or the `@waiting` idle semantics.
- **Config:** new env — `NEXUS_BUS_TRANSPORT` (default `slack`), `NATS_URL`, `NATS_CREDS` (or user/token), `NATS_A2A_STREAM`, `NATS_A2A_SUBJECT_PREFIX` (`nexus.a2a`), `NATS_PRESENCE_KV`, presence heartbeat/TTL, and ack-wait / in-progress-heartbeat tuning for the idle-gate. Documented in `docs/slack-bridge.md` + `docs/agent-bus-roadmap.md`.
- **Dependencies:** the `nats` npm package (nats.js — includes JetStream + KV; pin the latest stable) added to `slack-bridge/package.json`. New infra: a `nats-server` with JetStream enabled — deployable in the existing `docker-compose.work.yml` stack (network `agents-nexus-work_default`) for a single-host pilot, and reachable by all bridges (e.g. on the Linux "nexus" box) for cross-host.
- **Security / compliance:** self-hosted broker with TLS and per-connection subject-scoped authorization (NKEY/JWT). A participant's credential can be scoped to its own host subtree — least privilege, and agent traffic leaves Slack. Stream retention (max-age / max-bytes) sets the audit window; compliance input needed.
- **Operational:** cross-host A2A now depends on the NATS broker being up (same failure shape as "Slack down" today, but self-owned; JetStream persists and bridges drain on reconnect). Same-host `send-keys` stays Slack- and NATS-independent. HA is a later 3-node cluster. The change is inert with `NEXUS_BUS_TRANSPORT=slack`, so there is zero behavior change until a host opts in, and rollback is a flag + restart.
- **Closes:** the pending cross-host verification item in `slack-agent-bus` (its task 5.4) — true multi-host A2A becomes broker-routed and testable end-to-end.
