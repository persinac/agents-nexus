import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PROVIDERS, message, text, ephemeral, button, select, actionRow } from './types.js';
import { messageToBlockKit, slashToCommand, interactiveToAction, SlackAdapter } from './slack.js';

// The exact shape index.js produces today, so we can assert byte-identical rendering.
const eph = (t) => ({ response_type: 'ephemeral', text: t });

// ── model ────────────────────────────────────────────────────────────────────
test('message() drops absent/empty fields', () => {
  assert.deepEqual(message({}), {});
  assert.deepEqual(message({ text: 'hi' }), { text: 'hi' });
  assert.deepEqual(message({ text: 'hi', ephemeral: false }), { text: 'hi' });
  assert.deepEqual(message({ text: 'hi', ephemeral: true }), { text: 'hi', ephemeral: true });
  assert.deepEqual(message({ text: 'x', components: [] }), { text: 'x' }); // empty arrays dropped
  assert.deepEqual(message({ text: 'x', blocks: [] }), { text: 'x' });
});

test('text() / ephemeral() constructors', () => {
  assert.deepEqual(text('hi'), { text: 'hi' });
  assert.deepEqual(ephemeral('hi'), { text: 'hi', ephemeral: true });
});

test('component builders', () => {
  assert.deepEqual(button({ id: 'nx:go', text: 'Go' }), { type: 'button', id: 'nx:go', text: 'Go', value: 'nx:go' });
  assert.deepEqual(button({ id: 'nx:go', text: 'Go', value: 'v', style: 'primary' }),
    { type: 'button', id: 'nx:go', text: 'Go', value: 'v', style: 'primary' });
  const s = select({ id: 'nx:pick', placeholder: 'Pick', options: [{ text: 'A', value: 'a' }] });
  assert.equal(s.type, 'select');
  assert.equal(s.options.length, 1);
  const row = actionRow(button({ id: 'b', text: 'B' }));
  assert.equal(row.type, 'row');
  assert.equal(row.elements.length, 1);
});

// ── render ───────────────────────────────────────────────────────────────────
test('messageToBlockKit is byte-identical to eph() for ephemeral text', () => {
  assert.deepEqual(messageToBlockKit(ephemeral('spawning `x`…')), eph('spawning `x`…'));
});

test('messageToBlockKit in_channel default', () => {
  assert.deepEqual(messageToBlockKit(text('hello')), { response_type: 'in_channel', text: 'hello' });
});

test('messageToBlockKit passes raw blocks through verbatim (panel escape hatch)', () => {
  const raw = [{ type: 'section', text: { type: 'mrkdwn', text: 'panel' } }];
  const out = messageToBlockKit(message({ text: 'Nexus Fleet Control', ephemeral: true, blocks: raw }));
  assert.deepEqual(out, { response_type: 'ephemeral', text: 'Nexus Fleet Control', blocks: raw });
  assert.equal(out.blocks, raw); // same reference — no re-serialization
});

test('messageToBlockKit renders components → Block Kit actions', () => {
  const m = message({
    text: 'pick',
    components: [actionRow(
      button({ id: 'nx:do', text: 'Do', value: 'go', style: 'primary' }),
      select({ id: 'nx:pick', placeholder: 'Agent', options: [{ text: 'alpha', value: 'a' }] }),
    )],
  });
  const out = messageToBlockKit(m);
  assert.equal(out.blocks.length, 1);
  assert.equal(out.blocks[0].type, 'actions');
  const [btn, sel] = out.blocks[0].elements;
  assert.deepEqual(btn, { type: 'button', text: { type: 'plain_text', text: 'Do' }, action_id: 'nx:do', value: 'go', style: 'primary' });
  assert.equal(sel.type, 'static_select');
  assert.equal(sel.action_id, 'nx:pick');
  assert.deepEqual(sel.options, [{ text: { type: 'plain_text', text: 'alpha' }, value: 'a' }]);
});

test('messageToBlockKit prefers raw blocks over components when both present', () => {
  const raw = [{ type: 'divider' }];
  const out = messageToBlockKit(message({ blocks: raw, components: [actionRow(button({ id: 'x', text: 'X' }))] }));
  assert.deepEqual(out.blocks, raw);
});

// ── parse: slash → Command ────────────────────────────────────────────────────
test('slashToCommand splits subcommand + args', () => {
  const c = slashToCommand({ text: 'status foo bar', user_id: 'U1', channel_id: 'C1', response_url: 'https://r' });
  assert.equal(c.name, 'status');
  assert.deepEqual(c.args, ['foo', 'bar']);
  assert.equal(c.rawArgs, 'foo bar');
  assert.equal(c.userId, 'U1');
  assert.equal(c.channelId, 'C1');
  assert.equal(c.replyToken, 'https://r');
  assert.equal(c.provider, PROVIDERS.SLACK);
});

test('slashToCommand empty text defaults to home', () => {
  assert.equal(slashToCommand({ text: '' }).name, 'home');
  assert.equal(slashToCommand({}).name, 'home');
  assert.equal(slashToCommand({ text: '   ' }).name, 'home');
});

test('slashToCommand lowercases the subcommand and preserves arg text', () => {
  const c = slashToCommand({ text: 'MSG agents-nexus Hello There' });
  assert.equal(c.name, 'msg');
  assert.deepEqual(c.args, ['agents-nexus', 'Hello', 'There']);
  assert.equal(c.rawArgs, 'agents-nexus Hello There');
});

test('slashToCommand handles a flag arg', () => {
  const c = slashToCommand({ text: 'agents --local' });
  assert.equal(c.name, 'agents');
  assert.deepEqual(c.args, ['--local']);
});

// ── parse: interactive → Action ───────────────────────────────────────────────
test('interactiveToAction maps a block_actions envelope', () => {
  const a = interactiveToAction({
    type: 'block_actions',
    user: { id: 'U9' },
    response_url: 'https://r2',
    state: { values: { blk: { act: { value: 'v' } } } },
    actions: [{ action_id: 'nx:do', value: 'go' }],
  });
  assert.equal(a.actionId, 'nx:do');
  assert.equal(a.value, 'go');
  assert.equal(a.userId, 'U9');
  assert.equal(a.replyToken, 'https://r2');
  assert.deepEqual(a.state, { blk: { act: { value: 'v' } } });
  assert.equal(a.provider, PROVIDERS.SLACK);
});

test('interactiveToAction returns null for non-block_actions and empties', () => {
  assert.equal(interactiveToAction({ type: 'view_submission' }), null);
  assert.equal(interactiveToAction({ type: 'block_actions', actions: [] }), null);
  assert.equal(interactiveToAction({}), null);
});

// ── SlackAdapter (mock socket / web / post) ───────────────────────────────────
function fakeSocket() {
  const handlers = {};
  return {
    on: (ev, h) => { handlers[ev] = h; },
    emit: (ev, arg) => (handlers[ev] ? handlers[ev](arg) : undefined),
  };
}

test('SlackAdapter.start: slash acks then emits a Command', async () => {
  const socket = fakeSocket();
  let acked = false; let got = null;
  const ad = new SlackAdapter({ socket });
  ad.onCommand((c) => { got = c; });
  await ad.start();
  await socket.emit('slash_commands', { body: { text: 'peek alpha 30', response_url: 'https://r' }, ack: async () => { acked = true; } });
  assert.ok(acked);
  assert.equal(got.name, 'peek');
  assert.deepEqual(got.args, ['alpha', '30']);
});

test('SlackAdapter.start: block_actions acks then emits an Action; view_submission ignored', async () => {
  const socket = fakeSocket();
  let acks = 0; let got = null;
  const ad = new SlackAdapter({ socket });
  ad.onAction((a) => { got = a; });
  await ad.start();
  await socket.emit('interactive', { body: { type: 'block_actions', actions: [{ action_id: 'nx:refresh', value: 'r' }] }, ack: async () => { acks += 1; } });
  assert.equal(acks, 1);
  assert.equal(got.actionId, 'nx:refresh');
  // view_submission must NOT be acked/dispatched by the adapter (index.js owns it).
  got = null;
  await socket.emit('interactive', { body: { type: 'view_submission' }, ack: async () => { acks += 1; } });
  assert.equal(acks, 1);
  assert.equal(got, null);
});

test('SlackAdapter.reply posts the rendered payload to the reply token', async () => {
  const calls = [];
  const ad = new SlackAdapter({ post: async (url, payload) => calls.push([url, payload]) });
  await ad.reply('https://response', ephemeral('done'));
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], ['https://response', { response_type: 'ephemeral', text: 'done' }]);
  // No token → no post.
  await ad.reply('', ephemeral('x'));
  assert.equal(calls.length, 1);
});

test('SlackAdapter.send posts to a channel via chat.postMessage', async () => {
  const posts = [];
  const web = { chat: { postMessage: async (p) => posts.push(p) } };
  const ad = new SlackAdapter({ web });
  await ad.send('C123', text('hi'));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].channel, 'C123');
  assert.equal(posts[0].text, 'hi');
});
