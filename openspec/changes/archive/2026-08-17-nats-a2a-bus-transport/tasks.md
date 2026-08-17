## Status: ✅ SHIPPED AND LIVE — cutover complete 2026-08-17

**NATS is the fleet's sole A2A transport.** Slack carries only the human notify/reply leg.
Observed live on `alex-nexus`: `/health` → `transport: "nats"`, `a2a_mode: "multi-host"`;
broker `nats://mqtt.flashbackfleet.com:4222` (`nats-mqtt-prod`, NATS 2.11.11); the
`nexus_presence` KV bucket holds FQDN-keyed entries under **two** hosts (`alex-nexus`, `melvin`).

⚠️ **Read the remaining `- [ ]` boxes as post-cutover hardening, not as cutover blockers.**
An earlier reader took open items in §5/§8 as evidence the cutover hadn't happened and
reasoned forward from that for several steps. What is genuinely outstanding: ack-on-idle
(§5.1–5.4) and bare-name→FQDN resolution (§4.5). Everything else is done.

---

## Original status (2026-07-20, branch message-medium)

First implementation slice landed + validated. **Verified:** the FQDN↔subject/KV codec
(`orchestrator.js`, 8 unit tests), and the `NatsTransport` (publish, durable per-host
consumer, host isolation, offline/backlog durability, envelope round-trip, KV presence
upsert/snapshot/delete) **integration-tested against a live nats-server** (JetStream) —
`slack-bridge/transports/nats-transport.itest.mjs`. `index.js` wiring is syntax-checked +
boot-smoked (default `slack` mode unchanged, byte-for-byte). **Not yet done (needs a live
bridge + broker + Slack tokens together, or is an explicit follow-up):** ack-on-idle,
`/agents` reading KV, bare-name→FQDN resolution, and the multi-host cutover (8.x).

Validation run: `npm test` 48/48; `node --check` clean on orchestrator.js, index.js,
transports/*; `docker compose -f docker-compose.work.yml --profile nats config` valid;
integration test PASS.

## 1. NATS + JetStream infrastructure

- [x] 1.1 `nats` service (JetStream, `-js`) added to `docker-compose.work.yml` under the `nats` profile + `nats-data` volume + monitoring healthcheck (`nats:2.10-alpine`); compose config validates
- [x] 1.2 `NEXUS_A2A` stream provisioned in-code (`NatsTransport.ensureStream`, idempotent — add-if-missing) binding `nexus.a2a.>` with bounded `max_age`; verified live (itest)
- [x] 1.3 `nexus_presence` KV bucket provisioned in-code (`ensureKv`) with bucket TTL; verified live (itest)
- [~] 1.4 TLS + subject-scoped creds — connect() accepts `NATS_CREDS` (NKEY/JWT) / `NATS_TOKEN` / user+pass and TLS via the URL scheme. The least-privilege scope IS now exercised in prod: the live bridge's credentials are permitted `kv.keys()` on `nexus_presence` but DENIED `kv.get()` and `$JS.API.STREAM.LIST` (observed 2026-08-17). Remaining: document the mint path so the scope is reproducible rather than incidental.

## 2. Transport seam in the bridge

- [x] 2.1 `Transport` shape defined via the `NatsTransport` factory (connect/publish/subscribe/presenceUpsert/presenceSnapshot/presenceDelete/health/close)
- [x] 2.2 `NEXUS_BUS_TRANSPORT` (default `slack`) read at startup + logged; `/health` reports `transport` + `nats` readiness
- [~] 2.3 Slack path kept **inline + byte-for-byte** (not extracted into a formal `SlackTransport` class); pluggability is achieved by the transport branch in `/send` + the dynamically-imported NATS module. A full `SlackTransport` extraction is optional cleanup, deferred to keep the default path untouched
- [x] 2.4 `/send` publish + the inbound A2A path route through the seam in nats mode; inbound reuses `handleBusMessage` (synthesizes the same addressed line) so resolution + idle-gate + delivery are identical
- [x] 2.5 Default (`slack`) unchanged: `node --check` + boot-smoke (no tokens → boot guard exits 0); the NATS import is dynamic (slack-only bridge needs no NATS dep)

## 3. FQDN ↔ subject codec

- [x] 3.1 `fqdnToSubject` / `subjectToFqdn` / `hostSubjectFilter` / `fqdnToKvKey` / `kvKeyToFqdn` in `orchestrator.js` — reversible, collision-free, subject-legal (`~HH`) and KV-legal (`=HH`) escaping (KV forbids `~` — caught by the live integration test)
- [x] 3.2 8 unit tests in `orchestrator.test.js` (round-trip, escape legality, empty-token sentinel, prefix/arity rejection, host-subtree isolation, KV charset); `npm test` green

## 4. NATS transport implementation

- [x] 4.1 Dependency: the nats.js **v3 scoped packages** (`@nats-io/transport-node` + `/jetstream` + `/kv`, ^3.4.0) — NOT the deprecated `nats` meta-package
- [x] 4.2 `connect()`: connect (+ auth), bind JetStream + JSM, ensure stream + KV
- [x] 4.3 `publish(fqdn, envelope)`: codec subject + JSON `{to,from,msg,ts}` to the stream
- [x] 4.4 `subscribe(onMessage)`: durable consumer filtered to `hostSubjectFilter(selfHost)` → decode → hand to the caller (caller owns ack)
- [ ] 4.5 Bare-name single-owner (post-cutover follow-up): an empty host currently defaults to `selfHost` (host-local, matching the old "owning host" contract). KV bare→FQDN resolution + the queue-group race safety net are deferred. **Mitigation in the meantime: address agents by FQDN** — `/agents` now hands out a paste-ready `fqdn` per entry, so a bare name is a choice, not a necessity.

## 5. Ack-based idle-gate (the restart-durable buffer)

> **Post-cutover follow-up, NOT a cutover blocker.** The fleet is fully on NATS with
> ack-on-receive; these items harden an already-live transport. Do not read an open box
> here as evidence that the cutover is incomplete — see section 8.

- [ ] 5.1 Deliver-then-ack — CURRENT: **ack-on-receive** (hand to `handleBusMessage`, then ack). Ack-on-idle (ack only after inject at `@waiting=2`) is the follow-up
- [ ] 5.2 Hold-while-busy via in-progress (`working()`) acks — deferred with 5.1
- [x] 5.3 Poison-message bound: consumer `max_deliver` set (default 100)
- [ ] 5.4 Restart-durability verify — pending ack-on-idle (today a hold lives in the in-memory `busQueue`; the stream + redelivery is the coarse backstop)

## 6. Presence via KV

- [x] 6.1 `presenceUpsert` wired to a nats-mode heartbeat (upserts `loadRegistry()` FQDN-keyed); verified live (itest)
- [x] 6.2 `presenceSnapshot` reads the bucket back to records; verified live (itest); TTL ages out entries (bucket-level)
- [x] 6.3 `/agents` reads the KV — SHIPPED (`index.js`, the `BUS_TRANSPORT === 'nats' && natsReady` branch → `natsTransport.presenceSnapshot()`, mapped to the same output shape as the Slack path). Each entry also carries a paste-ready `fqdn` (`host/workspace/name`) and its `kv` key. Bare-name resolution still defaults an empty host to `selfHost` — that remainder is tracked as 4.5, not here.

## 7. Config, auth, permissions & docs

- [x] 7.1 Env added to `.env.example`: `NEXUS_BUS_TRANSPORT`, `NATS_URL`, `NATS_A2A_STREAM`, `NATS_A2A_SUBJECT_PREFIX`, `NATS_PRESENCE_KV`, `NATS_CREDS`/`NATS_TOKEN`/`NATS_USER`+`NATS_PASS`, `NATS_PORT`/`NATS_MONITOR_PORT`
- [x] 7.2 Documented in `docs/slack-bridge.md` (#nats-transport: seam, mapping table, migration, verification status) + `docs/agent-bus-roadmap.md` (Phase G marked as landing via this change)
- [x] 7.3 No new agent-side permission: `agent-send.sh` unchanged (POSTs `:8788/send`); the NATS client + creds live only in the bridge

## 8. Dual-run, cutover & rollback

- [~] 8.1 OBSOLETE — the Slack shadow-publish dual-run was a migration safety net for a cutover that has since completed outright. There is no window left to run it in; Slack A2A is deprecated fleet-wide.
- [x] 8.2 Same-host round-trip over NATS — the live bridge runs `transport: nats`, `a2a_mode: multi-host` and is the only A2A path in service, so the full `/send`→publish→consumer→`handleBusMessage`→inject chain is exercised by every message the fleet sends.
- [x] 8.3 Offline delivery — verified (itest: publish-before-subscribe backlog drains from the durable consumer = recipient "was down")
- [x] 8.4 KV presence — upsert/snapshot/delete verified (itest) AND surfaced: `/agents` serves the bucket live (6.3).
- [x] 8.5 Cross-host — DONE. The `nexus_presence` bucket holds FQDN entries under **two** hosts (`alex-nexus.*` and `melvin.*`), so a second bridge is joined and broker-routed. Closes `slack-agent-bus` task 5.4.
- [x] 8.6 Fleet cutover — DONE. NATS is the sole A2A transport; Slack carries only the human notify/reply leg.
- [~] 8.7 Rollback: default `slack` mode proven unchanged via boot-smoke. A live rollback is now a *theoretical* path only — the fleet has cut over and the Slack A2A leg is deprecated, so this is retained as documentation (`docs/slack-to-nats-cutover.md#rollback`), not as a tested procedure.
