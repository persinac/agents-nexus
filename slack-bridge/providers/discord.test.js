import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign as edSign } from 'crypto';
import { PROVIDERS, message, ephemeral, text, button, select, actionRow } from './types.js';
import {
  INTERACTION, CALLBACK, EPHEMERAL_FLAG, NEXUS_COMMAND,
  publicKeyFromHex, verifyEd25519, verifyInteractionSignature,
  packCustomId, unpackCustomId, messageToDiscord, slackTextToDiscord, capContent, MAX_CONTENT,
  interactionToCommand, interactionToAction, DiscordAdapter,
} from './discord.js';

// ── Ed25519 verification ─────────────────────────────────────────────────────
test('verifyEd25519 matches RFC 8032 test vector 1', () => {
  // The real point of this test is the hand-built SPKI DER wrapper in discord.js: if
  // that 12-byte prefix is wrong, every signature silently fails and every Discord
  // request 401s. A known-good vector catches that; a self-generated keypair would not,
  // since a wrong-but-consistent wrapper would still round-trip.
  const publicKey = 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a';
  const signature = 'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b';
  assert.equal(verifyEd25519({ publicKey, signature, message: Buffer.alloc(0) }), true);
});

test('publicKeyFromHex rejects anything that is not 32 bytes', () => {
  assert.throws(() => publicKeyFromHex('abcd'), /32 bytes/);
  assert.throws(() => publicKeyFromHex(''), /32 bytes/);
  // Buffer.from(...,'hex') truncates at the first invalid pair instead of throwing —
  // the length check is what turns that into a loud failure.
  assert.throws(() => publicKeyFromHex('zz'.repeat(32)), /32 bytes/);
});

test('verifyEd25519 returns false (never throws) on malformed input', () => {
  const publicKey = 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a';
  assert.equal(verifyEd25519({ publicKey, signature: 'beef', message: '' }), false);   // short sig
  assert.equal(verifyEd25519({ publicKey: 'nope', signature: 'ab'.repeat(64), message: '' }), false);
  assert.equal(verifyEd25519({}), false);
});

// A throwaway app keypair, used the way Discord uses one.
function fakeApp() {
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const raw = publicKey.export({ format: 'der', type: 'spki' }).subarray(-32);  // strip SPKI header
  return {
    publicKeyHex: raw.toString('hex'),
    sign: (msg) => edSign(null, Buffer.isBuffer(msg) ? msg : Buffer.from(msg, 'utf8'), privateKey).toString('hex'),
  };
}

test('verifyInteractionSignature signs timestamp || rawBody', () => {
  const app = fakeApp();
  const timestamp = '1723500000';
  const rawBody = JSON.stringify({ type: 1 });
  const signature = app.sign(timestamp + rawBody);
  assert.equal(verifyInteractionSignature({ publicKey: app.publicKeyHex, signature, timestamp, rawBody }), true);
  // Replaying the same signature under a different timestamp must fail.
  assert.equal(verifyInteractionSignature({ publicKey: app.publicKeyHex, signature, timestamp: '1723500001', rawBody }), false);
  // Any body mutation fails — including a semantically identical re-serialization.
  assert.equal(verifyInteractionSignature({ publicKey: app.publicKeyHex, signature, timestamp, rawBody: '{"type":1} ' }), false);
  assert.equal(verifyInteractionSignature({ publicKey: app.publicKeyHex, signature: '', timestamp, rawBody }), false);
});

test('verifyInteractionSignature is byte-exact over multi-byte UTF-8', () => {
  // The reason the HTTP route must use Buffer.concat and not `body += chunk`: a chunk
  // boundary inside a multi-byte character corrupts the decoded string, and the
  // signature check then fails intermittently under load. Verifying over the buffer
  // form and the string form must agree.
  const app = fakeApp();
  const timestamp = '1723500000';
  const rawBody = JSON.stringify({ msg: 'héllo 🌍 — ünicode' });
  const buf = Buffer.from(rawBody, 'utf8');
  const signature = app.sign(Buffer.concat([Buffer.from(timestamp, 'utf8'), buf]));
  assert.equal(verifyInteractionSignature({ publicKey: app.publicKeyHex, signature, timestamp, rawBody: buf }), true);
  assert.equal(verifyInteractionSignature({ publicKey: app.publicKeyHex, signature, timestamp, rawBody }), true);
  // A truncated buffer (the failure a split chunk would produce) must not verify.
  assert.equal(verifyInteractionSignature({ publicKey: app.publicKeyHex, signature, timestamp, rawBody: buf.subarray(0, buf.length - 1) }), false);
});

// ── custom_id packing ────────────────────────────────────────────────────────
test('packCustomId / unpackCustomId round-trip', () => {
  assert.equal(packCustomId('nx:do:peek', 'w1:p2'), 'nx:do:peek|w1:p2');
  assert.deepEqual(unpackCustomId('nx:do:peek|w1:p2'), { actionId: 'nx:do:peek', value: 'w1:p2' });
  // No payload → id only, and unpack mirrors it into value (matching button()'s default).
  assert.equal(packCustomId('nx:refresh', null), 'nx:refresh');
  assert.equal(packCustomId('nx:refresh', 'nx:refresh'), 'nx:refresh');
  assert.deepEqual(unpackCustomId('nx:refresh'), { actionId: 'nx:refresh', value: 'nx:refresh' });
  // Only the FIRST separator splits — pane ids may themselves contain one.
  assert.deepEqual(unpackCustomId('nx:do|a|b'), { actionId: 'nx:do', value: 'a|b' });
  assert.ok(packCustomId('x'.repeat(200), 'y').length <= 100);
});

// ── render ───────────────────────────────────────────────────────────────────
// ── Slack → Discord text dialect ─────────────────────────────────────────────
test('slackTextToDiscord converts known emoji shortcodes', () => {
  assert.equal(slackTextToDiscord(':large_green_circle: ok'), '🟢 ok');
  assert.equal(slackTextToDiscord(':white_circle:2 :large_yellow_circle:0'), '⚪2 🟡0');
  assert.equal(slackTextToDiscord(':warning: bad'), '⚠️ bad');
});

test('slackTextToDiscord leaves UNKNOWN shortcodes alone — action ids contain colons', () => {
  // The critical case: a blanket /:\w+:/ strip would turn `nx:do:peek` into `nxpeek`.
  assert.equal(slackTextToDiscord('nx:do:peek'), 'nx:do:peek');
  assert.equal(slackTextToDiscord('unknown :not_an_emoji: here'), 'unknown :not_an_emoji: here');
  assert.equal(slackTextToDiscord('nx:open:msg and :rocket:'), 'nx:open:msg and 🚀');
});

test('slackTextToDiscord upgrades Slack single-asterisk bold to Discord double', () => {
  assert.equal(slackTextToDiscord('*nexus fleet* · 5 agents'), '**nexus fleet** · 5 agents');
  assert.equal(slackTextToDiscord('a *bold* word.'), 'a **bold** word.');
  // Not bold: bare asterisks, globs, spaced asterisks.
  assert.equal(slackTextToDiscord('2 * 3 * 4'), '2 * 3 * 4');
  assert.equal(slackTextToDiscord('src/*.js'), 'src/*.js');
});

test('slackTextToDiscord never rewrites inside code spans or blocks', () => {
  assert.equal(slackTextToDiscord('run `nx:do:peek` now'), 'run `nx:do:peek` now');
  assert.equal(slackTextToDiscord('`*not bold*` but *this is*'), '`*not bold*` but **this is**');
  assert.equal(slackTextToDiscord('```\n:warning: *x*\n```'), '```\n:warning: *x*\n```');
  // Mixed: conversion still happens outside the fence.
  assert.equal(slackTextToDiscord(':rocket: `:rocket:`'), '🚀 `:rocket:`');
});

test('slackTextToDiscord is a no-op on plain text and null-safe', () => {
  assert.equal(slackTextToDiscord('nothing special'), 'nothing special');
  assert.equal(slackTextToDiscord(''), '');
  assert.equal(slackTextToDiscord(null), null);
});

test('messageToDiscord applies the dialect conversion to content', () => {
  // The real status line from the first live Discord run.
  const line = '*nexus fleet* · 5 agents · :large_green_circle:2 :white_circle:3 :large_yellow_circle:0';
  assert.equal(messageToDiscord(ephemeral(line)).content,
    '**nexus fleet** · 5 agents · 🟢2 ⚪3 🟡0');
});

test('capContent enforces Discord 2000-char limit', () => {
  // Over the limit Discord 400s and the entire reply is lost, not trimmed — a 40-line
  // `peek` capture blows straight past it while being perfectly fine on Slack.
  assert.equal(capContent('short'), 'short');
  const long = 'x'.repeat(5000);
  const out = capContent(long);
  assert.ok(out.length <= MAX_CONTENT, `got ${out.length}`);
  assert.match(out, /truncated$/);
  assert.equal(capContent('a'.repeat(MAX_CONTENT)).length, MAX_CONTENT);   // exactly at limit is untouched
});

test('capContent closes an unbalanced code fence when it truncates', () => {
  // Cutting mid-block would leave ``` unclosed and swallow the marker into a code span.
  const out = capContent('```\n' + 'y'.repeat(5000));
  assert.ok(out.length <= MAX_CONTENT);
  assert.equal((out.match(/```/g) || []).length % 2, 0, 'fences must be balanced');
});

test('messageToDiscord caps long content instead of letting Discord 400', () => {
  const out = messageToDiscord(ephemeral('z'.repeat(9000)));
  assert.ok(out.content.length <= MAX_CONTENT);
  assert.equal(out.flags, 64);
});

test('logHttpFailure reports field-level detail and our payload size', async () => {
  const lines = [];
  const ad = new DiscordAdapter({ log: (m) => lines.push(m) });
  await ad.logHttpFailure('follow-up post', {
    status: 400,
    json: async () => ({ code: 50035, errors: { content: { _errors: [{ code: 'BASE_TYPE_MAX_LENGTH', message: 'Must be 2000 or fewer in length.' }] } } }),
  }, { content: 'x'.repeat(3000) });
  assert.match(lines[0], /HTTP 400/);
  assert.match(lines[0], /contentLen=3000/);
  assert.match(lines[0], /BASE_TYPE_MAX_LENGTH/);
});

test('logHttpFailure survives a non-JSON body', async () => {
  const lines = [];
  const ad = new DiscordAdapter({ log: (m) => lines.push(m) });
  await ad.logHttpFailure('follow-up edit', { status: 502 }, {});
  assert.match(lines[0], /HTTP 502/);
});

test('messageToDiscord maps text + ephemeral flag', () => {
  assert.deepEqual(messageToDiscord(ephemeral('done')), { content: 'done', flags: EPHEMERAL_FLAG });
  assert.deepEqual(messageToDiscord(text('hello')), { content: 'hello' });
  assert.deepEqual(messageToDiscord({}), {});
});

test('messageToDiscord ignores Slack blocks but keeps the text fallback', () => {
  const m = message({ text: 'Nexus Fleet Control', ephemeral: true, blocks: [{ type: 'divider' }] });
  assert.deepEqual(messageToDiscord(m), { content: 'Nexus Fleet Control', flags: EPHEMERAL_FLAG });
});

test('messageToDiscord renders buttons into an action row', () => {
  const m = message({ text: 'pick', components: [actionRow(
    button({ id: 'nx:do:peek', text: 'Peek', value: 'w1:p2' }),
    button({ id: 'nx:do:stop', text: 'Stop', value: 'w1:p2', style: 'danger' }),
    button({ id: 'nx:refresh', text: 'Refresh', style: 'primary' }),
  )] });
  const out = messageToDiscord(m);
  assert.equal(out.components.length, 1);
  assert.equal(out.components[0].type, 1);
  assert.deepEqual(out.components[0].components, [
    { type: 2, style: 2, label: 'Peek', custom_id: 'nx:do:peek|w1:p2' },
    { type: 2, style: 4, label: 'Stop', custom_id: 'nx:do:stop|w1:p2' },
    { type: 2, style: 1, label: 'Refresh', custom_id: 'nx:refresh' },
  ]);
});

test('messageToDiscord gives a select its own row (Discord forbids mixing)', () => {
  const m = message({ components: [actionRow(
    button({ id: 'a', text: 'A' }),
    select({ id: 'nx:pick', placeholder: 'Agent', options: [{ text: 'db', value: 'w1:p2' }] }),
    button({ id: 'b', text: 'B' }),
  )] });
  const rows = messageToDiscord(m).components;
  assert.equal(rows.length, 3);
  assert.equal(rows[0].components[0].custom_id, 'a');          // buttons before the select
  assert.equal(rows[1].components.length, 1);                   // select alone
  assert.deepEqual(rows[1].components[0], {
    type: 3, custom_id: 'nx:pick', placeholder: 'Agent',
    options: [{ label: 'db', value: 'w1:p2' }],
  });
  assert.equal(rows[2].components[0].custom_id, 'b');          // buttons after it
});

test('messageToDiscord chunks >5 buttons and caps at 5 rows', () => {
  const many = Array.from({ length: 12 }, (_, i) => button({ id: `b${i}`, text: `B${i}` }));
  const rows = messageToDiscord(message({ components: [actionRow(...many)] })).components;
  assert.deepEqual(rows.map((r) => r.components.length), [5, 5, 2]);
  const tooMany = Array.from({ length: 40 }, (_, i) => button({ id: `b${i}`, text: `B${i}` }));
  assert.equal(messageToDiscord(message({ components: [actionRow(...tooMany)] })).components.length, 5);
});

test('messageToDiscord truncates a select to 25 options', () => {
  const opts = Array.from({ length: 40 }, (_, i) => ({ text: `a${i}`, value: `v${i}` }));
  const rows = messageToDiscord(message({ components: [actionRow(select({ id: 's', options: opts }))] })).components;
  assert.equal(rows[0].components[0].options.length, 25);
});

// ── parse ────────────────────────────────────────────────────────────────────
test('interactionToCommand mirrors the Slack parse', () => {
  const c = interactionToCommand({
    type: INTERACTION.APPLICATION_COMMAND, token: 'tok', channel_id: 'C1',
    member: { user: { id: 'U1' } },
    data: { name: 'nexus', options: [{ name: 'args', value: 'peek alpha 40' }] },
  });
  assert.equal(c.name, 'peek');
  assert.deepEqual(c.args, ['alpha', '40']);
  assert.equal(c.rawArgs, 'alpha 40');
  assert.equal(c.userId, 'U1');
  assert.equal(c.channelId, 'C1');
  assert.equal(c.replyToken, 'tok');
  assert.equal(c.provider, PROVIDERS.DISCORD);
});

test('interactionToCommand: no args option → home; DM invoker at user, not member', () => {
  assert.equal(interactionToCommand({ data: { name: 'nexus' } }).name, 'home');
  assert.equal(interactionToCommand({ data: { options: [{ name: 'args', value: '' }] } }).name, 'home');
  assert.equal(interactionToCommand({ user: { id: 'U-dm' }, data: {} }).userId, 'U-dm');
  assert.equal(interactionToCommand({ member: { user: { id: 'U-guild' } }, user: { id: 'ignored' }, data: {} }).userId, 'U-guild');
});

test('interactionToAction reads a button custom_id and a select value', () => {
  const btn = interactionToAction({
    type: INTERACTION.MESSAGE_COMPONENT, token: 't', member: { user: { id: 'U2' } },
    data: { custom_id: 'nx:do:stop|w1:p2' },
  });
  assert.equal(btn.actionId, 'nx:do:stop');
  assert.equal(btn.value, 'w1:p2');
  assert.equal(btn.provider, PROVIDERS.DISCORD);

  const sel = interactionToAction({
    type: INTERACTION.MESSAGE_COMPONENT, token: 't', user: { id: 'U3' },
    data: { custom_id: 'nx:pick', values: ['w2:p9'] },
  });
  assert.equal(sel.actionId, 'nx:pick');
  assert.equal(sel.value, 'w2:p9');   // from values[], not the custom_id
});

test('interactionToAction returns null for non-component interactions', () => {
  assert.equal(interactionToAction({ type: INTERACTION.APPLICATION_COMMAND, data: { custom_id: 'x' } }), null);
  assert.equal(interactionToAction({ type: INTERACTION.MESSAGE_COMPONENT, data: {} }), null);
  assert.equal(interactionToAction({}), null);
});

// ── adapter ──────────────────────────────────────────────────────────────────
function configuredAdapter(extra = {}) {
  const app = fakeApp();
  const calls = [];
  const ad = new DiscordAdapter({
    publicKey: app.publicKeyHex, botToken: 'bot-tok', appId: 'app-123',
    fetchImpl: async (url, init) => { calls.push({ url, init }); return { ok: true, status: 200 }; },
    ...extra,
  });
  const signed = (obj, timestamp = '1723500000') => {
    const rawBody = JSON.stringify(obj);
    return { rawBody, timestamp, signature: app.sign(timestamp + rawBody) };
  };
  return { ad, app, calls, signed };
}

test('DiscordAdapter is inert without config', async () => {
  const ad = new DiscordAdapter({});
  assert.equal(ad.enabled, false);
  assert.equal(await ad.start(), false);
  const res = await ad.handleInteraction({ rawBody: '{}', signature: 'x', timestamp: '1' });
  assert.equal(res.status, 404);
  // Partial config is still inert — a public key with no bot token cannot follow up.
  assert.equal(new DiscordAdapter({ publicKey: 'a'.repeat(64), appId: 'x' }).enabled, false);
});

test('DiscordAdapter.handleInteraction rejects a bad signature with 401', async () => {
  const { ad, signed } = configuredAdapter();
  const s = signed({ type: INTERACTION.PING });
  // Discord probes with a deliberately invalid signature when you save the endpoint
  // URL, and refuses the endpoint unless it gets a 401.
  const res = await ad.handleInteraction({ ...s, signature: 'ab'.repeat(64) });
  assert.equal(res.status, 401);
  assert.equal(res.work, null);
});

test('DiscordAdapter.handleInteraction answers PING with PONG', async () => {
  const { ad, signed } = configuredAdapter();
  const res = await ad.handleInteraction(signed({ type: INTERACTION.PING }));
  assert.equal(res.status, 200);
  assert.deepEqual(res.json, { type: CALLBACK.PONG });
  assert.equal(res.work, null);
});

test('DiscordAdapter.handleInteraction defers a command, then runs the handler in work()', async () => {
  const { ad, signed } = configuredAdapter();
  let got = null;
  ad.onCommand((c) => { got = c; });
  const res = await ad.handleInteraction(signed({
    type: INTERACTION.APPLICATION_COMMAND, token: 'tok', member: { user: { id: 'U1' } },
    data: { name: 'nexus', options: [{ name: 'args', value: 'status all' }] },
  }));
  // The ack must be returned WITHOUT waiting on the handler — that is the 3s deadline.
  assert.deepEqual(res.json, { type: CALLBACK.DEFERRED_CHANNEL_MESSAGE, data: { flags: EPHEMERAL_FLAG } });
  assert.equal(got, null, 'handler must not run before the ack is returned');
  await res.work();
  assert.equal(got.name, 'status');
  assert.equal(got.replyToken, 'tok');
});

test('DiscordAdapter.handleInteraction defers a component update, then runs onAction', async () => {
  const { ad, signed } = configuredAdapter();
  let got = null;
  ad.onAction((a) => { got = a; });
  const res = await ad.handleInteraction(signed({
    type: INTERACTION.MESSAGE_COMPONENT, token: 't', user: { id: 'U5' },
    data: { custom_id: 'nx:pick', values: ['w1:p2'] },
  }));
  assert.deepEqual(res.json, { type: CALLBACK.DEFERRED_UPDATE_MESSAGE });
  await res.work();
  assert.equal(got.actionId, 'nx:pick');
  assert.equal(got.value, 'w1:p2');
});

test('DiscordAdapter.handleInteraction 400s malformed and unsupported bodies', async () => {
  const { ad, app } = configuredAdapter();
  const ts = '1723500000';
  const bad = 'not json';
  assert.equal((await ad.handleInteraction({ rawBody: bad, timestamp: ts, signature: app.sign(ts + bad) })).status, 400);
  const body = JSON.stringify({ type: INTERACTION.AUTOCOMPLETE });
  assert.equal((await ad.handleInteraction({ rawBody: body, timestamp: ts, signature: app.sign(ts + body) })).status, 400);
});

test('DiscordAdapter.start bulk-overwrites /nexus (idempotent PUT)', async () => {
  const { ad, calls } = configuredAdapter();
  assert.equal(await ad.start(), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://discord.com/api/v10/applications/app-123/commands');
  assert.equal(calls[0].init.method, 'PUT');
  // The body IS the complete command set, so re-running can never duplicate.
  const sent = JSON.parse(calls[0].init.body);
  assert.deepEqual(sent, [NEXUS_COMMAND]);
  assert.equal(sent[0].options[0].type, 3);
  assert.equal(calls[0].init.headers.Authorization, 'Bot bot-tok');
});

test('DiscordAdapter.start registers to a guild when DISCORD_GUILD_ID is set', async () => {
  const lines = [];
  const { ad, calls } = configuredAdapter({ guildId: 'G777', log: (m) => lines.push(m) });
  assert.equal(await ad.start(), true);
  assert.equal(calls[0].url, 'https://discord.com/api/v10/applications/app-123/guilds/G777/commands');
  assert.equal(calls[0].init.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[0].init.body), [NEXUS_COMMAND]);
  assert.match(lines[0], /guild G777/);
  assert.match(lines[0], /immediately/);
});

test('DiscordAdapter.start falls back to global scope with no guild id', async () => {
  const lines = [];
  const { ad, calls } = configuredAdapter({ log: (m) => lines.push(m) });
  await ad.start();
  assert.equal(calls[0].url, 'https://discord.com/api/v10/applications/app-123/commands');
  // The propagation delay is the whole reason the guild knob exists — say so in the log.
  assert.match(lines[0], /global/);
  assert.match(lines[0], /1h/);
});

test('DiscordAdapter.start reports failure without echoing the response body', async () => {
  const lines = [];
  const { ad } = configuredAdapter({
    fetchImpl: async () => ({ ok: false, status: 401 }),
    log: (m) => lines.push(m),
  });
  assert.equal(await ad.start(), false);
  assert.equal(lines.length, 1);
  assert.match(lines[0], /HTTP 401/);
});

test('DiscordAdapter.start throws on a malformed public key rather than 401ing forever', async () => {
  const ad = new DiscordAdapter({ publicKey: 'oops', botToken: 't', appId: 'a', fetchImpl: async () => ({ ok: true }) });
  await assert.rejects(() => ad.start(), /32 bytes/);
});

test('DiscordAdapter.reply edits @original and drops flags', async () => {
  const { ad, calls } = configuredAdapter();
  await ad.reply('tok-abc', ephemeral('all good'));
  assert.equal(calls[0].url, 'https://discord.com/api/v10/webhooks/app-123/tok-abc/messages/@original');
  assert.equal(calls[0].init.method, 'PATCH');
  // flags are fixed by the deferred ack; re-sending them would be ignored.
  assert.deepEqual(JSON.parse(calls[0].init.body), { content: 'all good' });
  await ad.reply('', ephemeral('x'));
  assert.equal(calls.length, 1, 'no token → no request');
});

test('DiscordAdapter.followUp posts a new message and keeps flags', async () => {
  const { ad, calls } = configuredAdapter();
  await ad.followUp('tok-abc', ephemeral('extra'));
  assert.equal(calls[0].url, 'https://discord.com/api/v10/webhooks/app-123/tok-abc');
  assert.equal(calls[0].init.method, 'POST');
  // A follow-up (unlike the @original edit) honours flags, so ephemeral survives.
  assert.deepEqual(JSON.parse(calls[0].init.body), { content: 'extra', flags: EPHEMERAL_FLAG });
});

test('DiscordAdapter.send posts to a channel with bot auth', async () => {
  const { ad, calls } = configuredAdapter();
  await ad.send('C9', text('hi'));
  assert.equal(calls[0].url, 'https://discord.com/api/v10/channels/C9/messages');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers.Authorization, 'Bot bot-tok');
  assert.deepEqual(JSON.parse(calls[0].init.body), { content: 'hi' });
});
