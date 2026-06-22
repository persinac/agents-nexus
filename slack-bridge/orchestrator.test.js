// Unit tests for the pure `status`-command formatters in orchestrator.js.
// Run: `npm test` (node:test, zero deps). Deterministic — a fixed `now` is passed
// so durations don't depend on wall-clock.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { statusLabel, fmtAgo, formatFleetStatus, formatAgentStatus } from './orchestrator.js';

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
