#!/usr/bin/env node
// substrated — read-path daemon for the tmux<->herdr substrate seam.
//
// Hot readers (slack-bridge per bus tick, arbiter poll, overseer, window-status)
// must not spawn a subprocess per read. This daemon holds a cached view of fleet
// agent state and serves it over a tiny local HTTP API in the SAME tmux-dialect
// contract those readers already parse — so `substrate query`/`pane-opt` become a
// single cheap HTTP GET in herdr mode instead of a `herdr` CLI call each time.
//
//   NEXUS_SUBSTRATE=tmux  → serve by shelling tmux (exercisable before cutover)
//   NEXUS_SUBSTRATE=herdr → subscribe to herdr agent_status PUSHES for instant state
//                           (the idle-gate sees "idle" the moment it happens), with a
//                           periodic `agent.list` reconcile for the roster (+ nexus-state
//                           sidecar for epochs/tags) that also serves as the fallback
//
// Read API (127.0.0.1:SUBSTRATED_PORT, default 8422):
//   GET /windows            → lines: index|name|@waiting|path|command|@wait_type
//   GET /pane?id=..&opt=@X  → one window-option value (text)
//   GET /health             → {ok, backend, agents, herdrConnected}
//
// herdr protocol notes (from docs/herdr-spike.md): the server closes the socket
// after each non-subscription response, so each poll opens a fresh connection.
// @waiting maps from agent_status (working=0/blocked=1/idle|done=2); @wait_type
// rides in state_labels.blocked; epochs/tags (@wait_since/@last_tool/@keep/
// @cohort/@orchestrator) live in the sidecar (herdr has no arbitrary KV bag).

import http from 'http';
import net from 'net';
import { execFileSync } from 'child_process';
import { readFileSync } from 'fs';
import os from 'os';
import path from 'path';

const BACKEND = process.env.NEXUS_SUBSTRATE || 'herdr';  // default herdr; set NEXUS_SUBSTRATE=tmux for the legacy fallback
const PORT = parseInt(process.env.SUBSTRATED_PORT || '8422', 10);
const SESSION = process.env.TMUX_AGENT_SESSION || process.env.TMUX_SESSION || 'agents';
const HERDR_SOCK = process.env.HERDR_SOCKET_PATH || path.join(os.homedir(), '.config/herdr/herdr.sock');
const STATE_DIR = process.env.NEXUS_HERDR_STATE || path.join(os.homedir(), '.config/herdr/nexus-state');
const POLL_MS = parseInt(process.env.SUBSTRATED_POLL_MS || '1000', 10);
const WMAP = { working: '0', blocked: '1', idle: '2', done: '2' };

let cache = [];            // [{index,name,waiting,path,command,waitType,paneId}]
let herdrConnected = false;
// Push layer (Tier 2): one long-lived events.subscribe connection pushes agent_status
// transitions so the idle-gate sees "idle" the instant it happens instead of up to a
// poll-interval late. The poll (below) is demoted to roster reconciliation + fallback.
let subSock = null;         // live subscription socket (null when down)
let subHealthy = false;     // true once subscribed/receiving; gates poll-vs-push ownership of @waiting
let subPanesKey = '';       // sorted pane-id set currently subscribed (re-sub when it changes)
const RECONNECT_MS = parseInt(process.env.SUBSTRATED_RECONNECT_MS || '1000', 10);
const keyOf = (ids) => ids.slice().sort().join(',');

// One-shot herdr socket request (the server closes the connection after replying).
function herdrRequest(method, params = {}) {
  return new Promise((resolve, reject) => {
    const sock = net.connect(HERDR_SOCK);
    let buf = '';
    const t = setTimeout(() => { sock.destroy(); reject(new Error('herdr timeout')); }, 2000);
    sock.on('connect', () => sock.write(JSON.stringify({ id: 'sd', method, params }) + '\n'));
    sock.on('data', (d) => {
      buf += d;
      const nl = buf.indexOf('\n');
      if (nl >= 0) { clearTimeout(t); sock.end(); try { resolve(JSON.parse(buf.slice(0, nl))); } catch (e) { reject(e); } }
    });
    sock.on('error', (e) => { clearTimeout(t); reject(e); });
  });
}

function sidecar(paneId, key) {
  try {
    const f = path.join(STATE_DIR, paneId.replace(/[:/]/g, '_'));
    for (const ln of readFileSync(f, 'utf8').split('\n')) {
      if (ln.startsWith(key + '=')) return ln.slice(key.length + 1).trim();
    }
  } catch { /* no sidecar yet */ }
  return '';
}

// Roster reconciliation (poll). agent.list is authoritative for the roster and for
// name/cwd. For panes already tracked by a HEALTHY push subscription, push owns the
// live @waiting/@wait_type — don't clobber a fresh event with a staler snapshot; for
// NEW panes, or when push is down, seed those fields from agent.list. Re-subscribes
// whenever the pane set changes (agent spawned/exited, or a herdr restart churned the
// handles).
async function reconcileRoster() {
  try {
    const resp = await herdrRequest('agent.list');
    const agents = resp?.result?.agents || [];
    cache = agents.filter((a) => a.pane_id).map((a) => {
      const paneId = a.pane_id;
      const prev = cache.find((c) => c.paneId === paneId);
      const usePush = subHealthy && prev;   // push owns live state for already-tracked panes
      return {
        // a.name is the human name (herdr agent-start name / rename); a.agent is the
        // detected TYPE ("claude"). Prefer the human name so enumeration + the Slack
        // orchestrator's routing see "example-service", not "claude".
        index: paneId,
        name: a.name || a.agent || '',
        waiting: usePush ? prev.waiting : (WMAP[a.agent_status] ?? ''),
        path: a.foreground_cwd || '',
        command: 'claude',
        waitType: usePush ? prev.waitType : ((a.state_labels || {}).blocked || sidecar(paneId, 'wait_type') || ''),
        paneId,
      };
    });
    herdrConnected = true;
    const ids = cache.map((c) => c.paneId);
    if (keyOf(ids) !== subPanesKey) startSubscription(ids);
  } catch {
    herdrConnected = false;   // keep the last-known cache; readers fall back if stale
  }
}

// Apply one pushed message to the cache in place. Recognizes the subscription ack and
// the pane.agent_status_changed push; anything else is ignored. Updating @waiting here
// (regardless of subHealthy) is what makes the idle-gate instant.
function applyEvent(msg) {
  if (!msg) return;
  if (msg.result?.type === 'subscription_started' || msg.type === 'subscription_started' || msg.event === 'subscription_started') {
    subHealthy = true; herdrConnected = true; return;
  }
  if ((msg.event || msg.type) !== 'pane.agent_status_changed') return;
  subHealthy = true; herdrConnected = true;
  const d = msg.data || {};
  const w = cache.find((c) => c.paneId === d.pane_id);
  if (!w) return;   // unknown pane → the next reconcile adds it and re-subscribes
  if (d.agent_status != null && WMAP[d.agent_status] != null) w.waiting = WMAP[d.agent_status];
  if (d.state_labels && 'blocked' in d.state_labels) w.waitType = d.state_labels.blocked || '';
}

// Tear down the subscription socket WITHOUT triggering the auto-reconnect (used when
// the roster changed and we're about to re-subscribe with the new pane set).
function stopSubscription() {
  if (subSock) {
    const s = subSock; subSock = null;
    s.removeAllListeners('close'); s.removeAllListeners('error');
    try { s.destroy(); } catch { /* already gone */ }
  }
  subHealthy = false; subPanesKey = '';
}

// Open a single long-lived events.subscribe connection covering every known pane.
// herdr requires a pane_id per subscription, so we subscribe per pane over one socket
// and re-establish when the roster changes. An unexpected close/error schedules a
// reconnect; the poll stays the fallback while push is down.
function startSubscription(paneIds) {
  stopSubscription();
  if (!paneIds.length) return;              // nothing to watch yet; reconcile will call again
  subPanesKey = keyOf(paneIds);             // optimistic: prevents a duplicate re-sub before connect
  const sock = net.connect(HERDR_SOCK);
  subSock = sock;
  let buf = '';
  sock.on('connect', () => {
    const subscriptions = paneIds.map((pid) => ({ type: 'pane.agent_status_changed', pane_id: pid }));
    sock.write(JSON.stringify({ id: 'sub', method: 'events.subscribe', params: { subscriptions } }) + '\n');
  });
  sock.on('data', (d) => {
    buf += d;
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
      if (line.trim()) { try { applyEvent(JSON.parse(line)); } catch { /* skip malformed line */ } }
    }
  });
  sock.on('error', () => { /* 'close' fires next and handles the reconnect */ });
  sock.on('close', () => {
    if (subSock === sock) {                 // unexpected drop (not a roster re-sub) → reconnect
      subSock = null; subHealthy = false; subPanesKey = '';
      setTimeout(() => startSubscription(cache.map((c) => c.paneId).filter(Boolean)), RECONNECT_MS);
    }
  });
}

function tmuxWindowsLines() {
  try {
    const out = execFileSync('tmux', ['list-windows', '-t', SESSION, '-F',
      '#{window_index}|#{window_name}|#{@waiting}|#{pane_current_path}|#{pane_current_command}|#{@wait_type}'],
      { encoding: 'utf8', timeout: 2000 }).trim();
    return out ? out.split('\n') : [];
  } catch { return []; }
}

function windowsLines() {
  if (BACKEND === 'herdr') return cache.map((w) => [w.index, w.name, w.waiting, w.path, w.command, w.waitType].join('|'));
  return tmuxWindowsLines();
}

function paneOpt(id, opt) {
  if (BACKEND === 'herdr') {
    const w = cache.find((c) => c.paneId === id);
    if (opt === '@waiting') return w ? w.waiting : '';
    if (opt === '@wait_type') return (w && w.waitType) || sidecar(id, 'wait_type');
    return sidecar(id, opt.replace(/^@/, ''));   // @wait_since/@last_tool/@keep/@cohort/@orchestrator
  }
  try { return execFileSync('tmux', ['show-options', '-wqv', '-t', id, opt], { encoding: 'utf8', timeout: 2000 }).trim(); }
  catch { return ''; }
}

http.createServer((req, res) => {
  const u = new URL(req.url, 'http://localhost');
  if (u.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, backend: BACKEND, agents: cache.length, herdrConnected, pushConnected: subHealthy }));
  } else if (u.pathname === '/windows') {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    const body = windowsLines().join('\n');   // trailing newline matches `tmux list-windows`
    res.end(body ? body + '\n' : '');
  } else if (u.pathname === '/pane') {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end(paneOpt(u.searchParams.get('id') || '', u.searchParams.get('opt') || ''));
  } else {
    res.writeHead(404); res.end('not found');
  }
}).listen(PORT, '127.0.0.1', () => console.error(`substrated on 127.0.0.1:${PORT} backend=${BACKEND}`));

// Bootstrap: seed the cache + open the push subscription, then reconcile on POLL_MS.
// The first reconcileRoster() populates the roster and calls startSubscription() once
// panes are known; thereafter push keeps @waiting instant and the poll reconciles.
if (BACKEND === 'herdr') { reconcileRoster(); setInterval(reconcileRoster, POLL_MS); }
