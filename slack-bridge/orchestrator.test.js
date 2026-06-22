// Unit tests for the pure `status`-command formatters in orchestrator.js.
// Run: `npm test` (node:test, zero deps). Deterministic — a fixed `now` is passed
// so durations don't depend on wall-clock.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { statusLabel, fmtAgo, formatFleetStatus, formatAgentStatus, advanceDone, capWithMarker } from './orchestrator.js';

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

test('fmtAgo renders compact durations', () => {
  assert.equal(fmtAgo(0), '0s');
  assert.equal(fmtAgo(45), '45s');
  assert.equal(fmtAgo(240), '4m');
  assert.equal(fmtAgo(7600), '2h6m');
  assert.equal(fmtAgo(-5), '0s');     // clamp
  assert.equal(fmtAgo('nope'), '0s'); // non-numeric
});

const A = { name: 'infrastructure', slot: '3', cwd: '/home/u/repos/flashback-fleet/infrastructure', waiting: '0', lastTool: ago(240) };
const B = { name: 'requisition-device-cli', slot: '4', cwd: '/home/u/repos/flashback-fleet/requisition-device-cli', waiting: '1', waitSince: ago(120) };
const C = { name: 'integration-tests', slot: '5', cwd: '/x/integration-tests', waiting: '2', lastTool: ago(600) };

test('formatFleetStatus: header counts, per-line state, slot ordering', () => {
  const out = formatFleetStatus([C, A, B], { now: NOW_MS, stuckMin: 10 });
  // header
  assert.match(out, /\*nexus fleet\* · 3 agents/);
  assert.match(out, /:large_green_circle:1 :white_circle:1 :large_yellow_circle:1/);
  // per-line content
  assert.match(out, /:large_green_circle: infrastructure \(3\) · working · 4m · flashback-fleet\/infrastructure/);
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
  assert.match(out, /repo: flashback-fleet\/infrastructure @ main/);
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
