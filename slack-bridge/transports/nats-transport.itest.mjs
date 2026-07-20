// Throwaway integration test for NatsTransport against a live nats-server (JetStream).
// Requires a broker on nats://127.0.0.1:4222 (docker run -d -p 4222:4222 nats:latest -js).
// Run: node transports/nats-transport.itest.mjs   (NOT part of `npm test`).
import assert from 'node:assert/strict';
import { createNatsTransport } from './nats-transport.js';

const SELF = 'F4HFKXH56W';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failed = false;
const ok = (m) => console.log(`  ✔ ${m}`);
const fail = (m, e) => { failed = true; console.error(`  �’ ${m}: ${e?.message || e}`); };

const t = createNatsTransport({
  selfHost: SELF,
  url: 'nats://127.0.0.1:4222',
  streamName: 'NEXUS_A2A_IT',
  kvBucket: 'nexus_presence_it',
  presenceTtlMs: 60_000,
  ackWaitMs: 10_000,
});

const received = [];

try {
  await t.connect();
  ok('connect + provision (stream + KV)');

  // 1) Offline-durability: publish BEFORE subscribing. A durable consumer with
  //    DeliverPolicy.All must still get the backlog when it later attaches.
  await t.publish({ host: SELF, workspace: '', name: 'agents-nexus' },
    { to: 'agents-nexus', from: 'tester', msg: 'backlog-4242' });
  ok('publish to own-host subject (pre-subscribe backlog)');

  await t.subscribe(async (envelope, msg) => { received.push(envelope); msg.ack(); });
  ok('subscribe (durable consumer bound)');

  // 2) Live publish after subscribing.
  await t.publish({ host: SELF, workspace: 'search/r12n', name: 'svc-r12n' },
    { to: 'search/r12n/svc-r12n', from: 'tester', msg: 'live-7777' });
  ok('publish fqdn subject (live)');

  // 3) A message for a DIFFERENT host must NOT arrive on our host-filtered consumer.
  await t.publish({ host: 'someotherbox', workspace: '', name: 'general' },
    { to: 'someotherbox/general', from: 'tester', msg: 'not-mine-0000' });
  ok('publish to a foreign host subject');

  for (let i = 0; i < 40 && received.length < 2; i += 1) await sleep(100);

  const msgs = received.map((e) => e.msg).sort();
  assert.deepEqual(msgs, ['backlog-4242', 'live-7777'], `got: ${JSON.stringify(msgs)}`);
  ok('received backlog + live, and ONLY own-host messages (isolation holds)');
  assert.equal(received.find((e) => e.msg === 'live-7777').from, 'tester');
  ok('envelope round-trips { to, from, msg }');

  // 4) Presence KV upsert + snapshot round-trip.
  await t.presenceUpsert([
    { name: 'agents-nexus', workspace: '', pane: 'wA:p1' },
    { name: 'svc-r12n', workspace: 'search/r12n', pane: 'wB:p2' },
  ]);
  const snap = await t.presenceSnapshot();
  const names = snap.map((s) => s.name).sort();
  assert.deepEqual(names, ['agents-nexus', 'svc-r12n'], `snap: ${JSON.stringify(names)}`);
  assert.equal(snap.find((s) => s.name === 'svc-r12n').workspace, 'search/r12n');
  assert.equal(snap.every((s) => s.host === SELF), true);
  ok('presence KV upsert + snapshot round-trip (FQDN-keyed)');

  await t.presenceDelete({ workspace: '', name: 'agents-nexus' });
  const snap2 = await t.presenceSnapshot();
  assert.equal(snap2.find((s) => s.name === 'agents-nexus'), undefined);
  ok('presence delete removes the entry');
} catch (e) {
  fail('integration', e);
} finally {
  await t.close().catch(() => {});
}

console.log(failed ? 'INTEGRATION: FAIL' : 'INTEGRATION: PASS');
process.exit(failed ? 1 : 0);
