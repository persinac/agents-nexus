import { test } from 'node:test';
import assert from 'node:assert/strict';
import { text, ephemeral } from './types.js';
import { Notifier } from './notifier.js';

// A stand-in provider that records what it was asked to send.
function fakeProvider(name, { fail = false } = {}) {
  const sent = [];
  return {
    name,
    sent,
    async send(target, m) {
      if (fail) throw new Error(`${name} is down`);
      sent.push({ target, m });
    },
  };
}

test('register ignores falsey/unnamed providers and dedupes by name', () => {
  const n = new Notifier();
  assert.equal(n.register(fakeProvider('slack')), true);
  assert.equal(n.register(null), false);
  assert.equal(n.register({}), false);            // no name
  assert.deepEqual(n.names, ['slack']);
  // Re-registering the same name replaces rather than duplicating, so a second
  // start() call is idempotent.
  const replacement = fakeProvider('slack');
  n.register(replacement);
  assert.deepEqual(n.names, ['slack']);
  assert.equal(n.get('slack'), replacement);
});

test('resolve defaults to the originating provider only', () => {
  const n = new Notifier({ providers: [fakeProvider('slack'), fakeProvider('discord')] });
  // The default matters: broadcasting by default would double every notification the
  // moment a second provider is registered.
  assert.deepEqual(n.resolve({ origin: 'discord' }), ['discord']);
  assert.deepEqual(n.resolve({}), []);
  assert.deepEqual(n.resolve({ origin: 'nope' }), []);
});

test('resolve honours explicit list, then all, then origin', () => {
  const n = new Notifier({ providers: [fakeProvider('slack'), fakeProvider('discord')] });
  assert.deepEqual(n.resolve({ all: true }), ['slack', 'discord']);
  assert.deepEqual(n.resolve({ providers: ['discord'] }), ['discord']);
  // Explicit wins over all; unknown names are dropped, not errors.
  assert.deepEqual(n.resolve({ providers: ['discord'], all: true }), ['discord']);
  assert.deepEqual(n.resolve({ providers: ['ghost'] }), []);
});

test('fanout sends to each resolved provider with its own target', async () => {
  const slack = fakeProvider('slack');
  const discord = fakeProvider('discord');
  const n = new Notifier({ providers: [slack, discord] });
  const m = text('agent finished');
  const res = await n.fanout(m, { all: true, targets: { slack: 'C123', discord: '99887' } });
  assert.deepEqual(res, [{ provider: 'slack', ok: true }, { provider: 'discord', ok: true }]);
  // A Slack channel id is meaningless to Discord — each gets its own.
  assert.deepEqual(slack.sent, [{ target: 'C123', m }]);
  assert.deepEqual(discord.sent, [{ target: '99887', m }]);
});

test('fanout isolates failures — one provider down cannot suppress another', async () => {
  const slack = fakeProvider('slack');
  const discord = fakeProvider('discord', { fail: true });
  const lines = [];
  const n = new Notifier({ providers: [slack, discord], log: (l) => lines.push(l) });
  const res = await n.fanout(text('permission needed'), { all: true, targets: { slack: 'C1', discord: 'D1' } });
  // Slack still received it. This is the whole point: a Discord outage must not
  // swallow a card that also had to reach Slack.
  assert.equal(slack.sent.length, 1);
  assert.deepEqual(res.find((r) => r.provider === 'slack'), { provider: 'slack', ok: true });
  const d = res.find((r) => r.provider === 'discord');
  assert.equal(d.ok, false);
  assert.match(d.error, /discord is down/);
  assert.ok(lines.some((l) => /discord send failed/.test(l)));
});

test('fanout reports a missing target as skipped rather than dropping it silently', async () => {
  const slack = fakeProvider('slack');
  const discord = fakeProvider('discord');
  const lines = [];
  const n = new Notifier({ providers: [slack, discord], log: (l) => lines.push(l) });
  const res = await n.fanout(text('hi'), { all: true, targets: { slack: 'C1' } });   // no discord target
  assert.deepEqual(res.find((r) => r.provider === 'discord'), { provider: 'discord', ok: false, skipped: true });
  assert.equal(discord.sent.length, 0);
  // A missing target is a config bug; silence is how those survive for months.
  assert.ok(lines.some((l) => /no target configured for discord/.test(l)));
});

test('fanout with nothing resolved is a no-op', async () => {
  const slack = fakeProvider('slack');
  const n = new Notifier({ providers: [slack] });
  assert.deepEqual(await n.fanout(text('x'), {}), []);
  assert.deepEqual(await n.fanout(text('x'), { origin: 'ghost', targets: { ghost: 'z' } }), []);
  assert.equal(slack.sent.length, 0);
});

test('fanout passes the Message through untouched (rendering is the provider\'s job)', async () => {
  const slack = fakeProvider('slack');
  const n = new Notifier({ providers: [slack] });
  const m = ephemeral('only you');
  await n.fanout(m, { origin: 'slack', targets: { slack: 'C1' } });
  // The Notifier must not render — messageToBlockKit / messageToDiscord own that, and
  // pre-rendering here would defeat the whole seam.
  assert.equal(slack.sent[0].m, m);
});
