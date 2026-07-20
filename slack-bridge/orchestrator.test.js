// Unit tests for the pure `status`-command formatters in orchestrator.js.
// Run: `npm test` (node:test, zero deps). Deterministic — a fixed `now` is passed
// so durations don't depend on wall-clock.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { statusLabel, fmtAgo, formatFleetStatus, formatAgentStatus, advanceDone, capWithMarker, formatRelay, parseRelay, parsePresence, formatPresence, toInstance, applyPresence, ownersOf, ownerOf, presenceCollisions, reachability, RELAY_SENTINEL, PRESENCE_SENTINEL, parseAddress, parseAddressedLine, workspaceMatches } from './orchestrator.js';

// The addressed-bus parser lives in index.js (it needs no orchestrator state), but its
// shape is a shared contract with formatRelay/parsePresence — a relay or presence line must
// NEVER parse as an addressed `<addr>: body` delivery. Mirror the regex here so a change
// that breaks that invariant fails a test. The prefix is captured whole (may contain '/');
// parseAddress() splits it into host/workspace/name. First char stays alphanumeric, which is
// what guards the ':'-prefixed relay/presence sentinels from ever matching.
const ADDR_RE = /^([A-Za-z0-9][\w./-]*)\s*:\s*([\s\S]+)$/;

const NOW_S = 1_000_000;          // fixed "now" in seconds
const NOW_MS = NOW_S * 1000;
const ago = (secs) => String(NOW_S - secs);

test('statusLabel maps @waiting to active / waiting / idle', () => {
  assert.equal(statusLabel('0').key, 'active');
  assert.equal(statusLabel('').key, 'active');
  assert.equal(statusLabel(undefined).key, 'active');
  assert.equal(statusLabel('1').key, 'waiting');
  assert.equal(statusLabel('2').key, 'idle');
  assert.equal(statusLabel('0').emoji, ':large_green_circle:');
  assert.equal(statusLabel('1').emoji, ':large_yellow_circle:');
  assert.equal(statusLabel('2').emoji, ':white_circle:');
  assert.equal(statusLabel('1').text, 'waiting on you');
});

test('parseAddressedLine: pane handle survives (colon-truncation bug fix)', () => {
  // The original bug: `wQ:pF: …` truncated to token `wQ` → resolveByName('wQ') → silent drop.
  assert.deepEqual(parseAddressedLine('wQ:pF: ↩ from general: hello world'),
    { token: 'wQ:pF', body: '↩ from general: hello world' });
  // Bare name unchanged.
  assert.deepEqual(parseAddressedLine('scripts: ↩ from general: ping'),
    { token: 'scripts', body: '↩ from general: ping' });
  // Non-greedy stops at the FIRST colon-space — a body containing ": " is preserved intact.
  assert.deepEqual(parseAddressedLine('scripts: ↩ from x: TODO: fix it'),
    { token: 'scripts', body: '↩ from x: TODO: fix it' });
  // Workspaced / host-qualified token (contains '/') is captured whole for parseAddress.
  assert.equal(parseAddressedLine('chatbot/feedback/example-service: hi').token, 'chatbot/feedback/example-service');
  // Bare slot number.
  assert.equal(parseAddressedLine('3: do the thing').token, '3');
});

test('parseAddressedLine + parseAddress: FQDN targets (cross-host A2A) parse whole', () => {
  // The FQDN grammar is [host/][workspace/]name. The parser must keep the WHOLE prefix as
  // the token (it contains '/'), and it must NOT collide with the host-local pane-handle
  // branch (wN:pN). parseAddress then splits it right-to-left against known hosts.
  const HANDLE_RE = /^w[A-Za-z0-9]+:p[A-Za-z0-9]+$/;
  const known = new Set(['mac', 'nexus']);            // presence-announced hosts
  const pa = (t) => parseAddress(t, { knownHosts: known, selfHost: 'mac' });

  // host/name → cross-host by name
  let p = parseAddressedLine('mac/general: hello');
  assert.equal(p.token, 'mac/general');
  assert.equal(HANDLE_RE.test(p.token), false);
  assert.deepEqual(pa(p.token), { host: 'mac', workspace: '', name: 'general' });

  // workspace/name → local, bucket-scoped (unknown first segment = workspace, not host)
  p = parseAddressedLine('search/example-service: hi');
  assert.deepEqual(pa(p.token), { host: '', workspace: 'search', name: 'example-service' });

  // host/workspace(/…)/name → full FQDN with a multi-segment workspace
  p = parseAddressedLine('mac/mission/spark-reclaim/agent7: go');
  assert.equal(p.token, 'mac/mission/spark-reclaim/agent7');
  assert.deepEqual(pa(p.token), { host: 'mac', workspace: 'mission/spark-reclaim', name: 'agent7' });

  // A pane handle is NOT an FQDN (host-local) — must take the handle branch, not parseAddress.
  assert.equal(HANDLE_RE.test(parseAddressedLine('wQ:pF: ping').token), true);
});

test('parseAddressedLine: sentinels + no-space regression never parse as addressed', () => {
  // Presence/relay sentinels start with ':' → the ^[A-Za-z0-9] anchor rejects them.
  assert.equal(parseAddressedLine(`${PRESENCE_SENTINEL} {"host":"x"}`), null);
  assert.equal(parseAddressedLine(`${RELAY_SENTINEL} from x: hi`), null);
  // Documented, accepted regression: no space after the colon is NOT addressed.
  assert.equal(parseAddressedLine('general:hi'), null);
  // Not addressed at all.
  assert.equal(parseAddressedLine('just some text'), null);
  assert.equal(parseAddressedLine(''), null);
});

test('fmtAgo renders compact durations', () => {
  assert.equal(fmtAgo(0), '0s');
  assert.equal(fmtAgo(45), '45s');
  assert.equal(fmtAgo(240), '4m');
  assert.equal(fmtAgo(7600), '2h6m');
  assert.equal(fmtAgo(-5), '0s');     // clamp
  assert.equal(fmtAgo('nope'), '0s'); // non-numeric
});

const A = { name: 'infrastructure', slot: '3', cwd: '/home/u/repos/example-repo/infrastructure', waiting: '0', lastTool: ago(240) };
const B = { name: 'requisition-device-cli', slot: '4', cwd: '/home/u/repos/example-repo/requisition-device-cli', waiting: '1', waitSince: ago(120) };
const C = { name: 'integration-tests', slot: '5', cwd: '/x/integration-tests', waiting: '2', lastTool: ago(600) };

test('formatFleetStatus: header counts, per-line state, slot ordering', () => {
  const out = formatFleetStatus([C, A, B], { now: NOW_MS, stuckMin: 10 });
  // header
  assert.match(out, /\*nexus fleet\* · 3 agents/);
  assert.match(out, /:large_green_circle:1 :white_circle:1 :large_yellow_circle:1/);
  // per-line content
  assert.match(out, /:large_green_circle: infrastructure \(3\) · working · 4m · example-repo\/infrastructure/);
  assert.match(out, /:large_yellow_circle: requisition-device-cli \(4\) · waiting on you · 2m/);
  assert.match(out, /:white_circle: integration-tests \(5\) · idle · 10m/);
  // sorted by slot even though passed C, A, B
  assert.ok(out.indexOf('infrastructure (3)') < out.indexOf('requisition-device-cli (4)'));
  assert.ok(out.indexOf('requisition-device-cli (4)') < out.indexOf('integration-tests (5)'));
});

test('formatFleetStatus: empty fleet', () => {
  assert.match(formatFleetStatus([], { now: NOW_MS }), /no active agents/);
});

test('formatFleetStatus: stuck flag only when working past the threshold', () => {
  const stuck = { name: 'wedged', slot: '2', waiting: '0', lastTool: ago(1200) }; // 20m, > 10m
  assert.match(formatFleetStatus([stuck], { now: NOW_MS, stuckMin: 10 }), /:warning: stuck 20m/);
  const fresh = { name: 'busy', slot: '2', waiting: '0', lastTool: ago(300) };    // 5m, < 10m
  assert.doesNotMatch(formatFleetStatus([fresh], { now: NOW_MS, stuckMin: 10 }), /stuck/);
  // a waiting agent is never "stuck" even if it's been a while
  const longWait = { name: 'paused', slot: '2', waiting: '1', waitSince: ago(3000) };
  assert.doesNotMatch(formatFleetStatus([longWait], { now: NOW_MS, stuckMin: 10 }), /stuck/);
});

test('formatAgentStatus: single-agent detail with branch + last tool', () => {
  const out = formatAgentStatus({ ...A, branch: 'main' }, { now: NOW_MS, stuckMin: 10 });
  assert.match(out, /:large_green_circle: \*infrastructure\* \(slot 3\) · working · 4m/);
  assert.match(out, /repo: example-repo\/infrastructure @ main/);
  assert.match(out, /last tool: 4m ago/);
});

test('formatAgentStatus: null agent', () => {
  assert.equal(formatAgentStatus(null), ':warning: no such agent');
});

const DONE_OPTS = { stableMs: 20_000, ttlMs: 1_800_000 };
const fresh = () => ({ at: 0, sawWorking: false, idleSince: null });

test('advanceDone: fires after work then a stable idle period', () => {
  let e = fresh();
  ({ entry: e } = advanceDone(e, { waiting: '0', worked: true }, 1_000, DONE_OPTS)); // working
  assert.equal(e.sawWorking, true);
  let r = advanceDone(e, { waiting: '2', worked: false }, 2_000, DONE_OPTS);          // idle starts
  assert.equal(r.action, 'keep');
  assert.equal(r.entry.idleSince, 2_000);
  r = advanceDone(r.entry, { waiting: '2', worked: false }, 2_000 + 20_000, DONE_OPTS); // idle held
  assert.equal(r.action, 'fire');
});

test('advanceDone: auto-mode flicker (0→2→0→2) does not fire prematurely', () => {
  let e = fresh();
  ({ entry: e } = advanceDone(e, { waiting: '0', worked: true }, 100, DONE_OPTS));
  ({ entry: e } = advanceDone(e, { waiting: '2', worked: false }, 200, DONE_OPTS));   // idleSince=200
  ({ entry: e } = advanceDone(e, { waiting: '0', worked: true }, 300, DONE_OPTS));    // back to work — reset
  assert.equal(e.idleSince, null);
  ({ entry: e } = advanceDone(e, { waiting: '2', worked: false }, 400, DONE_OPTS));   // idleSince=400
  let r = advanceDone(e, { waiting: '2', worked: false }, 400 + 19_999, DONE_OPTS);   // not yet
  assert.equal(r.action, 'keep');
  r = advanceDone(r.entry, { waiting: '2', worked: false }, 400 + 20_000, DONE_OPTS); // now
  assert.equal(r.action, 'fire');
});

test('advanceDone: messaged while idle waits for work, not the pre-work idle', () => {
  let e = fresh();
  // first poll: still idle, no work yet -> must NOT start the timer
  let r = advanceDone(e, { waiting: '2', worked: false }, 1_000, DONE_OPTS);
  assert.equal(r.action, 'keep');
  assert.equal(r.entry.idleSince, null);
  assert.equal(r.entry.sawWorking, false);
  // work detected via @last_tool delta even though @waiting reads idle on this poll
  ({ entry: e } = advanceDone(r.entry, { waiting: '2', worked: true }, 2_000, DONE_OPTS));
  assert.equal(e.sawWorking, true);
  assert.equal(e.idleSince, 2_000);
  r = advanceDone(e, { waiting: '2', worked: false }, 2_000 + 20_000, DONE_OPTS);
  assert.equal(r.action, 'fire');
});

test('advanceDone: a permission prompt is not "finished"', () => {
  let e = fresh();
  ({ entry: e } = advanceDone(e, { waiting: '0', worked: true }, 100, DONE_OPTS));
  let r = advanceDone(e, { waiting: '1', worked: false }, 200, DONE_OPTS);            // at permission
  assert.equal(r.action, 'keep');
  assert.equal(r.entry.idleSince, null);
  // answered, runs, then settles idle -> fires
  ({ entry: e } = advanceDone(r.entry, { waiting: '0', worked: true }, 300, DONE_OPTS));
  ({ entry: e } = advanceDone(e, { waiting: '2', worked: false }, 400, DONE_OPTS));
  r = advanceDone(e, { waiting: '2', worked: false }, 400 + 20_000, DONE_OPTS);
  assert.equal(r.action, 'fire');
});

test('advanceDone: drops a stale entry past its TTL', () => {
  const r = advanceDone({ at: 0, sawWorking: true, idleSince: null }, { waiting: '0', worked: true }, 1_800_001, DONE_OPTS);
  assert.equal(r.action, 'drop');
});

test('capWithMarker: unchanged within the cap', () => {
  assert.equal(capWithMarker('hello', 10), 'hello');
  assert.equal(capWithMarker('exactly-ten', 'exactly-ten'.length), 'exactly-ten'); // boundary
  assert.equal(capWithMarker('', 5), '');
  assert.equal(capWithMarker(null, 5), '');        // no marker on empty
  assert.equal(capWithMarker(undefined, 5), '');
});

test('capWithMarker: visible marker + dropped count when over', () => {
  const out = capWithMarker('abcdefghij', 4);      // 10 chars, cap 4 -> drop 6
  assert.equal(out, 'abcd …[truncated 6 chars]');
  assert.ok(out.startsWith('abcd'));
  assert.match(out, /\[truncated 6 chars\]$/);
});

test('formatRelay: sentinel prefix + from@host attribution + verbatim body', () => {
  const out = formatRelay({ from: 'example-service', host: 'alex', text: 'line one\nline two' });
  assert.ok(out.startsWith(RELAY_SENTINEL), 'must start with the relay sentinel');
  assert.match(out, /↩ relay from example-service@alex:/);
  assert.ok(out.includes('line one\nline two'), 'body preserved verbatim, incl. newline');
});

test('formatRelay: missing fields degrade to unknown, never throw', () => {
  const out = formatRelay({});
  assert.ok(out.startsWith(RELAY_SENTINEL));
  assert.match(out, /from unknown@unknown:/);
});

test('parseRelay: round-trips formatRelay (from is the full who@host line)', () => {
  const text = 'multi\nline\noutput';
  const round = parseRelay(formatRelay({ from: 'agent-x', host: 'buddy', text }));
  assert.equal(round.from, 'agent-x@buddy'); // best-effort attribution = the whole who
  assert.equal(round.text, text);            // body round-trips exactly
});

test('parseRelay: returns null for a non-relay line', () => {
  assert.equal(parseRelay('general: hi'), null);
  assert.equal(parseRelay('just prose'), null);
  assert.equal(parseRelay(''), null);
  assert.equal(parseRelay(null), null);
});

test('INVARIANT: a relay never parses as an addressed delivery', () => {
  // The exact failure the sentinel exists to prevent: relayed output that starts
  // with `word:` (a pasted `TODO: fix …`) must NOT be read as a delivery to `TODO`.
  const relay = formatRelay({ from: 'a', host: 'h', text: 'TODO: fix the flaky test\nand ship it' });
  assert.equal(ADDR_RE.test(relay), false, 'relay must not match the addressed-delivery regex');
  assert.ok(parseRelay(relay), 'but it must parse as a relay');
});

test('INVARIANT: a presence snapshot never parses as an addressed delivery', () => {
  const snap = '::nexus-presence:: {"v":1,"host":"alex","agents":["general"],"ts":1}';
  assert.equal(ADDR_RE.test(snap), false);
  assert.ok(parsePresence(snap), 'but it must parse as presence');
});

test('addressed parser: captures the full address prefix + body (parseAddress splits it)', () => {
  const bare = 'general: hello there'.match(ADDR_RE);
  assert.equal(bare[1], 'general');       // whole address prefix
  assert.equal(bare[2], 'hello there');
  const qual = 'buddy/general: hello'.match(ADDR_RE);
  assert.equal(qual[1], 'buddy/general'); // full prefix — parseAddress splits host vs workspace
  assert.equal(qual[2], 'hello');
  const deep = 'mission/spark-reclaim/agent7: go'.match(ADDR_RE);
  assert.equal(deep[1], 'mission/spark-reclaim/agent7'); // multi-segment now allowed
  assert.equal(deep[2], 'go');
});

// --- parseAddress: the right-to-left workspace/host grammar (Inc 0) --------
// Mirror of agent-resolve.sh's nx_parse_addr. Last segment = name; first
// segment = host ONLY if known, else the whole prefix is the workspace label.
test('parseAddress: thin bare name', () => {
  assert.deepEqual(parseAddress('example-service', { selfHost: 'mac' }),
    { host: '', workspace: '', name: 'example-service' });
});

test('parseAddress: workspace/name (unknown first segment = workspace)', () => {
  assert.deepEqual(parseAddress('search/example-service', { selfHost: 'mac' }),
    { host: '', workspace: 'search', name: 'example-service' });
});

test('parseAddress: category/slug/name (label itself contains a slash)', () => {
  assert.deepEqual(parseAddress('mission/spark-reclaim/agent7', { selfHost: 'mac' }),
    { host: '', workspace: 'mission/spark-reclaim', name: 'agent7' });
});

test('parseAddress: host/name when first segment is the self host', () => {
  assert.deepEqual(parseAddress('mac/general', { selfHost: 'mac' }),
    { host: 'mac', workspace: '', name: 'general' });
});

test('parseAddress: host from knownHosts (case-insensitive, original case kept)', () => {
  assert.deepEqual(parseAddress('Buddy/general', { selfHost: 'mac', knownHosts: new Set(['buddy']) }),
    { host: 'Buddy', workspace: '', name: 'general' });
});

test('parseAddress: host/workspace/name (explicit cross-host + workspace)', () => {
  assert.deepEqual(parseAddress('mac/mission/x/agent7', { selfHost: 'mac' }),
    { host: 'mac', workspace: 'mission/x', name: 'agent7' });
});

test('parseAddress: unknown first segment is a workspace, not a host', () => {
  assert.deepEqual(parseAddress('otherpc/general', { selfHost: 'mac' }),
    { host: '', workspace: 'otherpc', name: 'general' });
});

// --- workspaceMatches: full-label or slug, empty = no filter --------------
test('workspaceMatches: full-label, slug, empty-filter, and miss', () => {
  assert.equal(workspaceMatches('mission/spark-reclaim', 'mission/spark-reclaim'), true); // full
  assert.equal(workspaceMatches('mission/spark-reclaim', 'spark-reclaim'), true);          // slug
  assert.equal(workspaceMatches('search', 'search'), true);
  assert.equal(workspaceMatches('search', 'mission'), false);
  assert.equal(workspaceMatches('mission/x', ''), true);                                   // no filter
});

// --- Presence: instance-identity model (FQDN v2 + v1 back-compat) ----------
// The bug this fixes: presence stored a per-host SET of bare names, so two same-named
// agents on one host collapsed and became unaddressable (silent drop). v2 carries
// {name, workspace, pane} records; two same-named instances stay distinct.

test('toInstance: normalizes v1 strings and v2 records', () => {
  assert.deepEqual(toInstance('general'), { name: 'general', workspace: '', pane: '' });
  assert.deepEqual(toInstance({ name: 'general', workspace: 'interactive', pane: 'w3:pK' }),
    { name: 'general', workspace: 'interactive', pane: 'w3:pK' });
  assert.deepEqual(toInstance({ name: 'x' }), { name: 'x', workspace: '', pane: '' }); // partial record
});

test('formatPresence: v1 bare names by default; v2 = names + instances (back-compat both ways)', () => {
  const insts = [{ name: 'general', workspace: 'interactive', pane: 'w3:pK' }];
  const v1 = formatPresence({ host: 'alex', agents: insts, ts: 1 });
  assert.ok(v1.startsWith(PRESENCE_SENTINEL));
  assert.deepEqual(JSON.parse(v1.slice(PRESENCE_SENTINEL.length).trim()),
    { v: 1, host: 'alex', agents: ['general'], ts: 1 });
  const v2obj = JSON.parse(formatPresence({ host: 'alex', agents: insts, ts: 1 }, { fqdn: true }).slice(PRESENCE_SENTINEL.length).trim());
  assert.deepEqual(v2obj, { v: 2, host: 'alex', agents: ['general'],
    instances: [{ name: 'general', workspace: 'interactive', pane: 'w3:pK' }], ts: 1 });
  // BACK-COMPAT: a v1 bridge does `obj.agents.map(String)` — with names there it never
  // sees "[object Object]"; it just misses the workspace/pane it doesn't understand.
  assert.deepEqual(v2obj.agents.map(String), ['general']);
});

test('parsePresence: reads v1, new-v2 (prefers instances), and legacy-v2 records', () => {
  const v1 = parsePresence('::nexus-presence:: {"v":1,"host":"alex","agents":["general","db"],"ts":5}');
  assert.equal(v1.v, 1);
  assert.deepEqual(v1.agents, [{ name: 'general', workspace: '', pane: '' }, { name: 'db', workspace: '', pane: '' }]);
  // new v2 wire: bare-name `agents` (v1-readable) + rich `instances` (v2 reads these)
  const v2 = parsePresence('::nexus-presence:: {"v":2,"host":"alex","agents":["general"],"instances":[{"name":"general","workspace":"interactive","pane":"w3:pK"}],"ts":5}');
  assert.equal(v2.v, 2);
  assert.deepEqual(v2.agents, [{ name: 'general', workspace: 'interactive', pane: 'w3:pK' }]); // prefers instances
  // legacy v2 (agents were records, no instances field) still parses via toInstance
  const legacy = parsePresence('::nexus-presence:: {"v":2,"host":"alex","agents":[{"name":"g","workspace":"w","pane":"p"}],"ts":5}');
  assert.deepEqual(legacy.agents, [{ name: 'g', workspace: 'w', pane: 'p' }]);
  const round = parsePresence(formatPresence({ host: 'alex', agents: v2.agents, ts: 9 }, { fqdn: true }));
  assert.deepEqual(round.agents, v2.agents); // format → parse round-trips
});

test('applyPresence: stores records, collapses exact dups, honors ts ordering', () => {
  const map = new Map();
  applyPresence(map, { host: 'alex', agents: [
    { name: 'general', workspace: 'interactive', pane: 'w3:pK' },
    { name: 'general', workspace: 'agents-nexus/routing', pane: 'wA:p5' },
    { name: 'general', workspace: 'interactive', pane: 'w3:pK' }, // exact dup → collapsed
  ], ts: 2 }, { now: 100 });
  assert.equal(map.get('alex').agents.length, 2);            // two distinct instances kept
  applyPresence(map, { host: 'alex', agents: [{ name: 'db', workspace: '', pane: 'w1:p1' }], ts: 1 }, { now: 200 });
  assert.equal(map.get('alex').agents.length, 2);            // older ts ignored
  applyPresence(map, { host: 'alex', agents: [{ name: 'db', workspace: '', pane: 'w1:p1' }], ts: 3 }, { now: 300 });
  assert.equal(map.get('alex').agents.length, 1);            // newer full-state snapshot replaces
});

test('presenceCollisions: intra-host + cross-host identity; NOT different-workspace', () => {
  const map = new Map();
  // two 'general' on ONE host in DIFFERENT workspaces → distinct, NOT a collision
  applyPresence(map, { host: 'alex', agents: [
    { name: 'general', workspace: 'interactive', pane: 'w3:pK' },
    { name: 'general', workspace: 'routing', pane: 'wA:p5' },
  ], ts: 1 });
  assert.equal(presenceCollisions(map).length, 0);
  // a SECOND interactive/general on alex → intra-host identity collision
  applyPresence(map, { host: 'alex', agents: [
    { name: 'general', workspace: 'interactive', pane: 'w3:pK' },
    { name: 'general', workspace: 'interactive', pane: 'w9:pZ' },
    { name: 'general', workspace: 'routing', pane: 'wA:p5' },
  ], ts: 2 });
  const cols = presenceCollisions(map);
  assert.equal(cols.length, 1);
  assert.equal(cols[0].name, 'general');
  assert.equal(cols[0].workspace, 'interactive');
  assert.equal(cols[0].count, 2);
});

test('presenceCollisions: v1 (no workspace) still flags cross-host same name (back-compat)', () => {
  const map = new Map();
  applyPresence(map, { host: 'alex', agents: ['general'], ts: 1 });
  applyPresence(map, { host: 'buddy', agents: ['general'], ts: 1 });
  const cols = presenceCollisions(map);
  assert.equal(cols.length, 1);
  assert.equal(cols[0].name, 'general');
  assert.deepEqual(cols[0].hosts, ['alex', 'buddy']);
});

test('reachability: one row per instance; two same-named on one host = two rows', () => {
  const map = new Map();
  applyPresence(map, { host: 'alex', agents: [
    { name: 'general', workspace: 'interactive', pane: 'w3:pK' },
    { name: 'general', workspace: 'routing', pane: 'wA:p5' },
  ], ts: 1 });
  const rows = reachability(map);
  assert.equal(rows.length, 2);
  assert.ok(rows.every((r) => r.name === 'general' && r.host === 'alex'));
  assert.deepEqual(rows.map((r) => r.workspace).sort(), ['interactive', 'routing']);
  assert.ok(rows.every((r) => r.pane));               // each carries its pane
  assert.ok(rows.every((r) => r.collided === false)); // different workspaces → not collided
});

test('ownersOf / ownerOf: name-level ownership on records, lexically-smallest host', () => {
  const map = new Map();
  applyPresence(map, { host: 'nexus', agents: [{ name: 'general', workspace: 'x', pane: 'w1:p1' }], ts: 1 });
  applyPresence(map, { host: 'buddy', agents: [{ name: 'general', workspace: 'y', pane: 'w1:p1' }], ts: 1 });
  assert.deepEqual(ownersOf(map, 'general'), ['buddy', 'nexus']);
  assert.equal(ownerOf(map, 'general'), 'buddy');
  assert.equal(ownerOf(map, 'nope'), null);
});
