// Unit tests for the provider-agnostic `/nexus` dispatch.
//
// These could not exist before core.js: the dispatch lived in index.js, which opens a
// Socket Mode connection at module load. Every behaviour asserted here was previously
// verified only by structural inspection or by driving a live Slack/Discord client.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createNexusCore } from './core.js';
import { ephemeral } from './providers/types.js';
import { fleetPanel } from './orchestrator.js';

const AGENT_OPTS = [{ text: 'database', value: 'w1:p2' }, { text: 'api', value: 'w1:p3' }];

// Deps with recording stubs. Overrides let a test replace exactly what it cares about.
function makeCore(overrides = {}) {
  const calls = [];
  const rec = (name, ret) => (...a) => { calls.push([name, ...a]); return ret; };
  const deps = {
    spawnEnabled: true,
    nexusChannel: 'C-CONTROL',
    spawnAllowlistFile: '/nonexistent-allowlist.json',
    localAgentOptions: () => AGENT_OPTS,
    paneName: (pane) => ({ 'w1:p2': 'database', 'w1:p3': 'api' }[pane] || pane),
    statusText: rec('statusText', 'STATUS'),
    agentsSummaryText: rec('agentsSummaryText', ephemeral('AGENTS')),
    doPeek: rec('doPeek', ephemeral('PEEK')),
    doClearCmd: rec('doClearCmd', ephemeral('CLEAR')),
    doStopCmd: rec('doStopCmd', ephemeral('STOP')),
    doKeepCmd: rec('doKeepCmd', ephemeral('KEEP')),
    doMsgCmd: rec('doMsgCmd', ephemeral('MSG')),
    doSpawn: rec('doSpawn', undefined),
    doRestore: rec('doRestore', undefined),
    nexusMsgPrefix: (uid) => `<@${uid}>: `,
    ledgerCmd: rec('ledgerCmd', []),
    log: rec('log', undefined),
    ...overrides,
  };
  const core = createNexusCore(deps);
  const replies = [];
  const reply = async (m) => { replies.push(m); };
  const cmd = (name, rawArgs = '', extra = {}) => ({
    name, rawArgs, args: rawArgs ? rawArgs.split(/\s+/) : [], userId: 'U1', channelId: 'C-INVOKE', ...extra,
  });
  return { core, calls, replies, reply, cmd, deps };
}

// ── commands ─────────────────────────────────────────────────────────────────
test('home/help/empty all render the fleet panel', async () => {
  for (const sub of ['home', 'help', '']) {
    const { core, replies, reply, cmd } = makeCore();
    await core.dispatchNexusCommand(cmd(sub), reply);
    assert.equal(replies.length, 1);
    assert.deepEqual(replies[0], fleetPanel({
      agents: [{ name: 'database', pane: 'w1:p2', label: 'database' }, { name: 'api', pane: 'w1:p3', label: 'api' }],
      spawnEnabled: true,
    }));
  }
});

test('status passes the joined args through and wraps the result ephemerally', async () => {
  const { core, calls, replies, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('status', 'database all'), reply);
  assert.deepEqual(calls.find((c) => c[0] === 'statusText'), ['statusText', 'database all']);
  assert.deepEqual(replies[0], ephemeral('STATUS'));
});

test('agents detects the --local flag from rawArgs', async () => {
  for (const [raw, expected] of [['--local', true], ['', false], ['--localish', false]]) {
    const { core, calls, reply, cmd } = makeCore();
    await core.dispatchNexusCommand(cmd('agents', raw), reply);
    assert.equal(calls.find((c) => c[0] === 'agentsSummaryText')[1], expected, `raw=${raw}`);
  }
});

test('peek/clear/stop/keep forward positional args verbatim', async () => {
  const { core, calls, replies, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('peek', 'w1:p2 40'), reply);
  await core.dispatchNexusCommand(cmd('clear', 'w1:p2'), reply);
  await core.dispatchNexusCommand(cmd('stop', 'w1:p2'), reply);
  await core.dispatchNexusCommand(cmd('keep', 'w1:p2 on'), reply);
  assert.deepEqual(calls.find((c) => c[0] === 'doPeek'), ['doPeek', 'w1:p2', '40']);
  assert.deepEqual(calls.find((c) => c[0] === 'doClearCmd'), ['doClearCmd', 'w1:p2']);
  assert.deepEqual(calls.find((c) => c[0] === 'doStopCmd'), ['doStopCmd', 'w1:p2']);
  assert.deepEqual(calls.find((c) => c[0] === 'doKeepCmd'), ['doKeepCmd', 'w1:p2', 'on']);
  assert.deepEqual(replies.map((r) => r.text), ['PEEK', 'CLEAR', 'STOP', 'KEEP']);
});

test('msg preserves the rest of the line verbatim and prefixes the sender', async () => {
  const { core, calls, reply, cmd } = makeCore();
  // Internal spacing after the agent name must survive — this is the subtle one.
  await core.dispatchNexusCommand(cmd('msg', 'database hello  there   world'), reply);
  assert.deepEqual(calls.find((c) => c[0] === 'doMsgCmd'),
    ['doMsgCmd', 'database', '<@U1>: hello  there   world']);
});

test('msg with no body sends an empty string, not a bare prefix', async () => {
  const { core, calls, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('msg', 'database'), reply);
  assert.deepEqual(calls.find((c) => c[0] === 'doMsgCmd'), ['doMsgCmd', 'database', '']);
});

test('message is an alias for msg', async () => {
  const { core, calls, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('message', 'api hi'), reply);
  assert.equal(calls.find((c) => c[0] === 'doMsgCmd')[1], 'api');
});

// ── spawn ────────────────────────────────────────────────────────────────────
test('spawn is refused when disabled, without touching doSpawn', async () => {
  const { core, calls, replies, reply, cmd } = makeCore({ spawnEnabled: false });
  await core.dispatchNexusCommand(cmd('spawn', 'anything'), reply);
  assert.match(replies[0].text, /spawn disabled/);
  assert.equal(calls.some((c) => c[0] === 'doSpawn'), false);
});

test('spawn with an unknown repo refuses BEFORE the optimistic ack', async () => {
  // The ordering matters: a rocket emoji goes over response_url where the invoker sees
  // it, while doSpawn's rejection posts to the control channel they may not watch — so
  // an unvalidated name would read as success.
  const { core, calls, replies, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('spawn', 'not-a-real-repo'), reply);
  assert.equal(calls.some((c) => c[0] === 'doSpawn'), false);
  assert.equal(replies.length, 1);
  assert.doesNotMatch(replies[0].text, /:rocket:/);
});

test('spawn with no repo lists nothing when the allowlist is empty', async () => {
  const { core, replies, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('spawn'), reply);
  assert.match(replies[0].text, /no spawnable repos configured/);
});

test('restore is refused when disabled', async () => {
  const { core, calls, replies, reply, cmd } = makeCore({ spawnEnabled: false });
  await core.dispatchNexusCommand(cmd('restore', 'repo'), reply);
  assert.match(replies[0].text, /restore disabled/);
  assert.equal(calls.some((c) => c[0] === 'doRestore'), false);
});

test('restore with a repo acks then restores into the CONTROL channel', async () => {
  const { core, calls, replies, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('restore', 'myrepo'), reply);
  assert.match(replies[0].text, /restoring/);
  // Not the invoking channel: chat.postMessage there 404s when the bot isn't a member.
  assert.deepEqual(calls.find((c) => c[0] === 'doRestore'),
    ['doRestore', 'C-CONTROL', undefined, 'myrepo', 'U1']);
});

test('restore with no repo lists dormant agents from the ledger', async () => {
  const { core, replies, reply, cmd } = makeCore({
    ledgerCmd: async () => [{ repo: 'alpha' }, { name: 'beta' }],
  });
  await core.dispatchNexusCommand(cmd('restore'), reply);
  assert.match(replies[0].text, /dormant: alpha, beta/);
});

test('restore handles a non-array ledger response without throwing', async () => {
  const { core, replies, reply, cmd } = makeCore({ ledgerCmd: async () => null });
  await core.dispatchNexusCommand(cmd('restore'), reply);
  assert.match(replies[0].text, /no dormant agents/);
});

// ── errors + unknown ─────────────────────────────────────────────────────────
test('an unknown subcommand lists the valid ones', async () => {
  const { core, replies, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('frobnicate'), reply);
  assert.match(replies[0].text, /Unknown `frobnicate`/);
  assert.match(replies[0].text, /status · agents · peek/);
});

test('a thrown helper becomes a warning reply, not an unhandled rejection', async () => {
  const logged = [];
  const { core, replies, reply, cmd } = makeCore({
    statusText: async () => { throw new Error('substrate unreachable'); },
    log: (m) => logged.push(m),
  });
  await core.dispatchNexusCommand(cmd('status'), reply);
  assert.deepEqual(replies[0], ephemeral(':warning: substrate unreachable'));
  assert.match(logged[0], /slash status failed: substrate unreachable/);
});

// ── actions ──────────────────────────────────────────────────────────────────
test('nx:pick EDITS the panel with the chosen agent', async () => {
  const { core } = makeCore();
  const edits = []; const posts = [];
  await core.dispatchNexusAction({ actionId: 'nx:pick', value: 'w1:p2' },
    { edit: async (m) => edits.push(m), post: async (m) => posts.push(m) });
  assert.equal(posts.length, 0, 'a pick must edit in place, never post a new message');
  assert.deepEqual(edits[0], fleetPanel({
    agents: [{ name: 'database', pane: 'w1:p2', label: 'database' }, { name: 'api', pane: 'w1:p3', label: 'api' }],
    picked: { name: 'database', pane: 'w1:p2' },
    spawnEnabled: true,
  }));
});

test('nx:pick with no value clears the selection', async () => {
  const { core } = makeCore();
  const edits = [];
  await core.dispatchNexusAction({ actionId: 'nx:pick', value: '' },
    { edit: async (m) => edits.push(m), post: async () => {} });
  assert.equal(edits[0].blocks.length, fleetPanel({ agents: [{ name: 'database', pane: 'w1:p2', label: 'database' }, { name: 'api', pane: 'w1:p3', label: 'api' }] }).blocks.length);
});

test('nx:do:* POSTS beside the panel rather than replacing it', async () => {
  const cases = [
    ['fleetstatus', 'STATUS'], ['status', 'STATUS'], ['peek', 'PEEK'],
    ['clear', 'CLEAR'], ['stop', 'STOP'], ['keepon', 'KEEP'], ['keepoff', 'KEEP'],
  ];
  for (const [act, expected] of cases) {
    const { core } = makeCore();
    const edits = []; const posts = [];
    await core.dispatchNexusAction({ actionId: `nx:do:${act}`, value: 'w1:p2' },
      { edit: async (m) => edits.push(m), post: async (m) => posts.push(m) });
    assert.equal(edits.length, 0, `${act} must not replace the panel`);
    assert.deepEqual(posts[0], ephemeral(expected), act);
  }
});

test('nx:do:keepon / keepoff pass the right on/off flag', async () => {
  for (const [act, flag] of [['keepon', 'on'], ['keepoff', 'off']]) {
    const { core, calls } = makeCore();
    await core.dispatchNexusAction({ actionId: `nx:do:${act}`, value: 'w1:p2' },
      { edit: async () => {}, post: async () => {} });
    assert.deepEqual(calls.find((c) => c[0] === 'doKeepCmd'), ['doKeepCmd', 'w1:p2', flag]);
  }
});

test('an unrecognised nx:do action reports itself instead of failing silently', async () => {
  const { core } = makeCore();
  const posts = [];
  await core.dispatchNexusAction({ actionId: 'nx:do:teleport', value: 'w1:p2' },
    { edit: async () => {}, post: async (m) => posts.push(m) });
  assert.deepEqual(posts[0], ephemeral('unknown action teleport'));
});

test('a null action and an unrelated action id are both no-ops', async () => {
  const { core } = makeCore();
  const edits = []; const posts = [];
  const out = { edit: async (m) => edits.push(m), post: async (m) => posts.push(m) };
  await core.dispatchNexusAction(null, out);
  await core.dispatchNexusAction({ actionId: 'approve:1', value: '1' }, out);
  assert.deepEqual([edits.length, posts.length], [0, 0]);
});

test('a thrown action helper posts a warning', async () => {
  const logged = [];
  const { core } = makeCore({
    doPeek: async () => { throw new Error('pane gone'); },
    log: (m) => logged.push(m),
  });
  const posts = [];
  await core.dispatchNexusAction({ actionId: 'nx:do:peek', value: 'w1:p2' },
    { edit: async () => {}, post: async (m) => posts.push(m) });
  assert.deepEqual(posts[0], ephemeral(':warning: pane gone'));
  assert.match(logged[0], /panel action nx:do:peek failed: pane gone/);
});

// ── the seam itself ──────────────────────────────────────────────────────────
test('no platform types cross the boundary — the core is driven entirely by stubs', async () => {
  // If this file ever needs a Slack or Discord import to exercise the dispatch, the
  // seam has leaked.
  const { core, replies, reply, cmd } = makeCore();
  await core.dispatchNexusCommand(cmd('status'), reply);
  assert.equal(replies.length, 1);
  assert.deepEqual(Object.keys(replies[0]).sort(), ['ephemeral', 'text']);
});
