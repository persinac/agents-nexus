## Status (2026-07-20, branch message-medium)

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
- [ ] 1.4 TLS + subject-scoped creds — connect() accepts `NATS_CREDS` (NKEY/JWT) / `NATS_TOKEN` / user+pass and TLS via the URL scheme; the least-privilege subject scope + mint path are documented but NOT yet exercised (prod hardening / rollout)

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
- [ ] 4.5 Bare-name single-owner: an empty host currently defaults to `selfHost` (host-local, matching the Slack "owning host" contract). KV bare→FQDN resolution + the queue-group race safety net are deferred

## 5. Ack-based idle-gate (the restart-durable buffer)

- [ ] 5.1 Deliver-then-ack — CURRENT: **ack-on-receive** (hand to `handleBusMessage`, then ack). Ack-on-idle (ack only after inject at `@waiting=2`) is the follow-up
- [ ] 5.2 Hold-while-busy via in-progress (`working()`) acks — deferred with 5.1
- [x] 5.3 Poison-message bound: consumer `max_deliver` set (default 100)
- [ ] 5.4 Restart-durability verify — pending ack-on-idle (today a hold lives in the in-memory `busQueue`; the stream + redelivery is the coarse backstop)

## 6. Presence via KV

- [x] 6.1 `presenceUpsert` wired to a nats-mode heartbeat (upserts `loadRegistry()` FQDN-keyed); verified live (itest)
- [x] 6.2 `presenceSnapshot` reads the bucket back to records; verified live (itest); TTL ages out entries (bucket-level)
- [ ] 6.3 `/agents` + bare-name resolution reading from KV — deferred (the Slack `presenceMap`/`reachability` path is unchanged; folding KV into it is the follow-up)

## 7. Config, auth, permissions & docs

- [x] 7.1 Env added to `.env.example`: `NEXUS_BUS_TRANSPORT`, `NATS_URL`, `NATS_A2A_STREAM`, `NATS_A2A_SUBJECT_PREFIX`, `NATS_PRESENCE_KV`, `NATS_CREDS`/`NATS_TOKEN`/`NATS_USER`+`NATS_PASS`, `NATS_PORT`/`NATS_MONITOR_PORT`
- [x] 7.2 Documented in `docs/slack-bridge.md` (#nats-transport: seam, mapping table, migration, verification status) + `docs/agent-bus-roadmap.md` (Phase G marked as landing via this change)
- [x] 7.3 No new agent-side permission: `agent-send.sh` unchanged (POSTs `:8788/send`); the NATS client + creds live only in the bridge

## 8. Dual-run, cutover & rollback

- [ ] 8.1 Dual-run on a real host (bridge + broker + Slack tokens) with a Slack shadow-publish window — needs a provisioned box
- [~] 8.2 Same-host round-trip over NATS — verified at the **transport** level (itest: publish→consumer→envelope); full bridge path (`/send`→publish→consumer→`handleBusMessage`→send-keys) pending a live bridge
- [x] 8.3 Offline delivery — verified (itest: publish-before-subscribe backlog drains from the durable consumer = recipient "was down")
- [~] 8.4 KV presence — upsert/snapshot/delete verified (itest); surfacing in `/agents` + collision view is 6.3 (deferred)
- [ ] 8.5 Cross-host: a second host's bridge on NATS → broker-routed delivery (closes `slack-agent-bus` task 5.4)
- [ ] 8.6 Fleet cutover host-by-host; human notify/reply stays on Slack
- [~] 8.7 Rollback: default `slack` mode proven unchanged via boot-smoke; full live rollback (`NEXUS_BUS_TRANSPORT=slack` + restart on a running bridge) pending a live bridge
