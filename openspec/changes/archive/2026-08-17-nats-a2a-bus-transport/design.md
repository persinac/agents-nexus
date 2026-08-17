## Context

The A2A bus today is Slack. In `slack-bridge/index.js` the medium is exactly two touchpoints:

- **Publish** — `web.chat.postMessage(...)` inside `POST /send` (`:1667`), `POST /relay` (`:1702`), and presence publish (`:1040`).
- **Subscribe** — `SocketModeClient` (`:174`) → `handleBusMessage` (`:1290`).

Everything else is medium-agnostic and stays: FQDN addressing (`orchestrator.js` `parseAddress`, `qualifyFrom` — the `[host/][workspace/]name` grammar), presence (`presenceMap`, `applyPresence`, `ownerOf`, `presenceCollisions`), the delivery layer (`deliverToName/Slot/Pane` → `deliverViaScript` → `agent-send.sh` local delivery over send-keys or the SDK inbox), the `@waiting` idle-gate + in-memory `busQueue`/`flushBusQueue`, and the human round-trip (`/notify`, `resolveRequest`, thread-tracking, nudge cards).

Slack plays two roles that this change separates:
1. **A2A bus** (`#nexus-agents`) — machine↔machine. This is what must scale; it moves.
2. **Human notify/reply** (`#nexus`, `/notify`, thread-tracking, `/relay`) — humans live in Slack; this stays.

Hard constraints that survive the swap: `agent-send.sh` (symlinked `~/.tmux/agent-send.sh` on every host) stays byte-for-byte — it POSTs `:8788/send` and the bridge decides the medium. The delivery layer and the `@waiting` semantics are the arbiter/reaper-shared contract and do not change. The `slack-agent-bus` change already established the durable-buffer requirement (idle-gated, non-interrupting delivery) and left a disk-persisted queue explicitly deferred and cross-host delivery (its task 5.4) pending a second real host — both are directly addressed here.

## Goals / Non-Goals

**Goals:**
- Move the A2A bus off Slack onto a durable transport that scales to the whole fleet without a per-participant messaging app.
- Keep addressing, presence semantics, the delivery layer, and idle-gating identical across transports — the swap is confined to publish/subscribe/presence.
- Make the durable buffer survive a bridge restart (close the deferred in-memory-queue gap) by expressing the idle-gate as JetStream acknowledgement.
- Keep same-host `send-keys` instant and independent of any broker.
- Leave the human notify/reply leg on Slack, unchanged.
- Opt-in per host, dual-runnable during migration, rollback by a flag + restart. `agent-send.sh` unchanged.

**Non-Goals:**
- Moving the human-facing legs (`/notify`, threads, `/relay`) off Slack.
- Changing `agent-send.sh`, the delivery helpers, or the `@waiting` idle contract.
- NATS cluster HA / multi-region — single node first; a 3-node cluster is a follow-on.
- A self-serve credential-issuing UI — rollout uses manually-minted, subject-scoped creds; self-serve minting is a follow-on for true 1000-scale.
- Replacing agent-memory or Langfuse; changing the spawn branch or human-control routing.
- Guaranteeing global message ordering beyond JetStream's per-subject ordering (which already exceeds Slack best-effort).

## Decisions

### A transport seam selected by `NEXUS_BUS_TRANSPORT`, default `slack`
Extract a `Transport` interface — `connect()`, `close()`, `health()`, `publish(fqdn, envelope)`, `subscribe(selfHost, onMessage)`, `presenceUpsert(agents)`, `presenceSnapshot()` — and provide `SlackTransport` (wraps today's `postMessage` + `SocketModeClient`) and `NatsTransport`. `handleBusMessage`, `/send`, and presence call the interface, never Slack directly. Default `slack` so a host with the flag unset behaves exactly as today. **Alternative considered:** a second standalone bus daemon on its own port — rejected; it would duplicate the bridge's registry access, delivery helpers, and `@waiting` plumbing for no benefit. The seam keeps one process and one delivery path.

### Subjects encode the FQDN; each bridge subscribes only its own host subtree
Publish to `nexus.a2a.<host>.<workspace>.<name>`; a bridge subscribes `nexus.a2a.<self-host>.>`. The broker routes each message to exactly the owning host's consumer — no fleet-wide fan-out, no "is `to` mine?" scan on every host for every message (the Slack model). This is the core scaling change: N participants cost the broker N subscriptions, not N×N channel re-ingestion. **Alternative considered:** one wildcard subject all bridges consume + client-side filter (mirrors Slack) — rejected; it re-imposes the fan-out we are trying to escape. **Decision to make (open):** the FQDN↔subject codec. Subjects are dot-delimited and disallow ` . * >`; workspaces are path-ish (`search/r12n/svc-r12n`) and names/hosts are token-ish. Leaning: a deterministic, reversible, token-wise escape (map `/`→a subtoken separator, escape reserved chars) that keeps subjects human-readable for debugging, unit-tested beside `parseAddress`.

### JetStream stream is the durable inbox and the audit record
A stream `NEXUS_A2A` binds `nexus.a2a.>` with age/size-bounded retention. Each host-bridge is a **durable consumer** filtered to its host subtree. An offline recipient's messages persist in the stream; on reconnect the bridge resumes from its acked cursor and drains them — no loss, no external dedup. The stream is also the replay/audit log (retention window = the audit window). **Alternative considered:** core NATS (no JetStream) + keep the Slack channel as the durable log — rejected; it splits durability across two systems and keeps PHI in Slack. JetStream makes the transport self-contained.

### The idle-gate is JetStream acknowledgement (closes the restart gap)
Today a busy recipient's message sits in an in-memory `busQueue` and is flushed at `@waiting=2`; a bridge restart mid-buffer loses it (the Slack change's deferred item). Under NATS: the consumer receives a message but the bridge **acks only after** it injects at an idle prompt. A busy recipient → the message is not acked and stays owned by the consumer; the bridge extends the lease with in-progress (`working`) acks so `AckWait` does not redeliver prematurely; on idle it delivers then acks. A bridge restart mid-hold → the un-acked message is redelivered and re-held. The channel-as-backstop becomes stream-as-backstop, and the buffer is now restart-durable. **Alternative considered:** ack-on-receive + keep the in-memory idle queue (JetStream only as transport + a coarse cross-restart backstop) — simpler, but it preserves the restart gap and duplicates hold-state. Leaning ack-on-idle; the risk is a long-busy agent holding a lease for hours (see Risks). Both are behind the same `@waiting` signal, so the delivery contract is unchanged either way.

### Single-owner delivery: FQDN subject first, KV-resolve for bare names, queue group as safety net
A qualified target (`host/name` or `host/workspace/name`) publishes to that host's subject — the subject *is* the owner, so no election. A **bare** name that more than one host claims is resolved to an FQDN via the presence KV before publish; if a race still lets two hosts consume the same logical target, they share a **queue group** so the broker delivers to exactly one. This replaces the lexically-smallest-host `ownerOf` rule with broker-guaranteed single delivery. **Alternative considered:** pure queue-group with no resolution (broker picks an arbitrary owner) — rejected as the primary path because it is non-deterministic and hides collisions; KV-resolve keeps ownership legible and still surfaces collisions. FQDN addressing (already the recommended convention) sidesteps the whole question.

### Presence is a JetStream KV bucket keyed by FQDN with TTL
Each bridge upserts a KV entry per live local agent (`<host>/<workspace>/<name>` → `{ts, pane, ...}`) on startup, on a heartbeat, and on a registry `fs.watch` change; the entry carries a TTL of ~2× the heartbeat. Departure = TTL expiry (self-healing, no tombstone) — the same aging the in-memory `presenceMap` does today, but server-side and consistent across all bridges. `/agents` and bare-name resolution read `keys()`/`get()`; a KV watch gives live updates. **Alternative considered:** keep the channel gossip snapshots (`::nexus-presence::`) but publish them over NATS — rejected; it keeps a bespoke full-state-snapshot parser and per-bridge eventual consistency when KV gives a shared, TTL'd, watchable store for less code. `orchestrator.js` `presenceCollisions` still runs over the assembled view for the collision warning.

### Human-facing legs stay on Slack
`/notify`, `resolveRequest`, thread-tracking, the dormant nudge cards, and `/relay` (agent→human) are genuinely human-facing and low-volume; they keep the `WebClient`. Only `/send`, presence, and the `handleBusMessage` inbound path move to the seam. **Alternative considered:** move `/relay` to NATS too for uniformity — deferred; a human reads it in Slack, so it belongs on the human medium. Splitting cleanly on "machine vs human" keeps the migration bounded.

### `agent-send.sh` and the delivery layer are untouched
The script still POSTs `:8788/send {to,from,msg}`; the bridge's `/send` publishes via the active transport. Local same-host `send-keys`, the `SLACK_A2A_SAMEHOST=channel` routing, the bare-digit/`%pane` local-stay rules, and the SDK inbox delivery all stay. **Alternative considered:** teach `agent-send.sh` to speak NATS directly — rejected; it would put a NATS client + creds on every agent shell, multiply the auth surface, and bypass the bridge's idle-gate and presence. The bridge stays the single chokepoint.

## Risks / Trade-offs

- **New always-on infra dependency.** Cross-host A2A now needs the NATS broker up. Same failure shape as "Slack down" today, but self-owned. Mitigation: JetStream persists and bridges drain on reconnect; same-host `send-keys` is broker-independent; a 3-node cluster is the HA follow-on.
- **Ack-on-idle vs a long-busy agent.** A recipient busy for hours holds an un-acked message; a too-short `AckWait` redelivers, a too-long one delays a genuine redelivery after a crash. Mitigation: in-progress (`working`) ack heartbeats to extend the lease while `@waiting≠2`, a generous `AckWait`, and a max-deliver + dead-letter so a poison message cannot loop forever. Fallback: ack-on-receive + in-memory gate.
- **Subject-codec correctness.** A non-reversible or collision-prone FQDN↔subject codec misroutes silently. Mitigation: a small, pure, unit-tested codec (extend `orchestrator.test.js`) with round-trip property tests; reserved-char escaping verified against real host/workspace/name samples.
- **Credential provisioning at 1000-scale.** Manual creds do not scale; the win over Slack evaporates if issuing a cred is as heavy as approving an app. Mitigation: subject-scoped NKEY/JWT with a documented mint path now; a self-serve issuer (reuse an IdP) as the scale follow-on. Even manual creds are lighter than a security-reviewed Slack app.
- **Migration double-delivery.** During dual-run, Slack and NATS could both deliver the same message. Mitigation: exactly one transport is the *subscriber-of-record* per host at a time; the other is publish-only shadow (audit). Cut delivery + presence together per host.
- **Split-brain presence during a mixed window.** If some hosts are on Slack presence and some on KV, reachability splits. Mitigation: migrate presence with delivery per host; optionally bridge KV↔channel presence for the cutover window; keep the window short.
- **PHI now in the JetStream stream.** Agent output is durable in NATS. Mitigation: self-hosted + TLS + subject-scoped access + a bounded retention window; document the retention/compliance posture — arguably stronger than Slack, but it needs an explicit decision, not a default.
- **Impersonation via self-reported `from`.** Unchanged from today. Mitigation: subject-scoped creds let the broker constrain which host a connection may claim; validating `from`↔connection is a hardening follow-on.

## Migration Plan

1. Stand up `nats-server` with JetStream (single node in `docker-compose.work.yml` for the pilot); create the `NEXUS_A2A` stream (`nexus.a2a.>`, bounded retention) and the `nexus_presence` KV bucket; enable TLS + a bootstrap subject-scoped credential.
2. Add the `Transport` seam to the bridge; route `/send`, presence, and `handleBusMessage` through it. Ship `SlackTransport` = today's behavior. With `NEXUS_BUS_TRANSPORT=slack` (default) there is zero behavior change.
3. Implement `NatsTransport` (connect + JetStream + KV; publish to the host subject; durable per-host consumer → `onMessage` → existing `deliver*`; ack-on-idle with in-progress heartbeats; KV presence upsert/snapshot). Add the `nats` npm dep (latest stable). Add the FQDN↔subject codec + unit tests.
4. Dual-run on this host: `NEXUS_BUS_TRANSPORT=nats` with a Slack shadow-publish for the audit window. Verify a `--via-slack`-style round-trip now goes NATS→deliver; the idle-gate holds a busy recipient and drains on idle; **offline delivery** (stop the recipient bridge, send, restart → it drains from JetStream); **restart-durability** (restart mid-hold → message re-held); KV presence populates `/agents`.
5. Bring up a second host's bridge on NATS → verify true cross-host over the broker (closes `slack-agent-bus` task 5.4).
6. Cut the fleet over host-by-host: `NEXUS_BUS_TRANSPORT=nats`; leave the human notify/reply leg on Slack throughout.
7. Rollback at any point: `NEXUS_BUS_TRANSPORT=slack` + restart → the bridge is back on the Slack bus with the in-memory idle-gate.

## Open Questions

- **Ack model:** ack-on-idle (JetStream = the durable buffer, closes the restart gap) vs ack-on-receive + in-memory gate (simpler, keeps the gap). Leaning ack-on-idle — confirm `AckWait` + in-progress-heartbeat tuning against a realistically long-busy agent, and set max-deliver + a dead-letter.
- **Subject codec:** exact FQDN↔subject encoding for workspace paths and reserved chars — human-readable token-escape (leaning) vs an opaque base32url of the FQDN. Must be reversible and collision-free.
- **Where NATS lives for cross-person 1000-scale:** single node on the nexus box for the pilot; who owns the production cluster, its network exposure (VPN/Tailscale vs public+TLS), and credential issuance?
- **Auth model:** one shared account with subject-scoped permissions vs per-user NKEY/JWT; and the self-serve minting path for scale.
- **Stream retention:** the A2A audit window (max-age / max-bytes) — needs compliance input given PHI-adjacent content.
- **`/relay` placement:** keep all human-facing traffic on Slack (leaning) or unify onto NATS later?
- **Bare-name delivery:** KV-resolve-then-publish (deterministic, leaning) vs queue-group-only (broker-arbitrary) — and whether to keep the queue group purely as a race safety net.
- **Presence during the mixed-transport window:** migrate presence strictly per-host with delivery, or bridge KV↔channel presence for the cutover?
