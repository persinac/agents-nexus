/**
 * Nexus Slack Bridge
 *
 * Two-way link between a Slack control channel and the running tmux agents.
 *
 *   Inbound  : Slack #nexus message / thread reply / DM  ->  tmux send-keys to an agent
 *   Outbound : an agent needs input (Notification hook)  ->  POST /notify  ->  Slack post
 *
 * Routing (precedence):
 *   1. Reply in a thread the bot started  -> that thread's agent (round-trip happy path)
 *   2. Top-level "name: text" / "slot: text" / "@bot name: text" -> registry lookup
 *   3. Otherwise -> a one-line usage hint, no delivery
 *
 * Delivery reuses ~/.tmux/agent-send.sh (slot/name resolution + send-keys).
 * Connects over Socket Mode, so no public URL / tunnel is required.
 */

import http from 'http';
import { fileURLToPath } from 'url';
import { dirname, join, resolve } from 'path';
import { execFileSync } from 'child_process';
import {
  readFileSync, writeFileSync, renameSync, existsSync, readdirSync,
} from 'fs';
import { homedir } from 'os';
import { SocketModeClient, LogLevel } from '@slack/socket-mode';
import { WebClient } from '@slack/web-api';
import * as orch from './orchestrator.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// --- .env loader (mirrors arbiter/index.js so SLACK_* are available standalone) ---
const _envPath = resolve(__dirname, '..', '.env');
if (existsSync(_envPath)) {
  for (const line of readFileSync(_envPath, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 0) continue;
    const k = trimmed.slice(0, eq).trim();
    if (!process.env[k]) process.env[k] = trimmed.slice(eq + 1).trim();
  }
}

const HOME = homedir();
const REGISTRY_DIR = join(HOME, '.tmux', 'registry');
const AGENT_SEND = join(HOME, '.tmux', 'agent-send.sh');
const THREAD_MAP_PATH = join(HOME, '.tmux', 'slack-threads.json');

const BOT_TOKEN = process.env.SLACK_BOT_TOKEN;
const APP_TOKEN = process.env.SLACK_APP_TOKEN;
const NEXUS_CHANNEL = process.env.SLACK_NEXUS_CHANNEL || '';
const PORT = parseInt(process.env.SLACK_BRIDGE_PORT || '8788', 10);
const THREAD_TTL_MS = 7 * 24 * 60 * 60 * 1000; // prune mappings older than 7 days
// Proactive stale-card sweep cadence. Cards answered IN SLACK self-resolve; this
// catches the ones answered locally in the CLI (or whose window closed).
const PRUNE_INTERVAL_MS = parseInt(process.env.SLACK_PRUNE_INTERVAL_MS || '10000', 10);

// Smart routing: when a message names no agent, an LLM (haiku — fast/cheap, same
// middle-man pattern as notify-classify.py) infers the target from the active
// agent list. Disabled with SLACK_ROUTE_ENABLED=0 or when no API key is present;
// below the confidence floor it asks instead of guessing.
const ROUTE_ENABLED = process.env.SLACK_ROUTE_ENABLED !== '0';
const ROUTE_MODEL = process.env.SLACK_ROUTE_MODEL || 'claude-haiku-4-5';
const ROUTE_MIN_CONFIDENCE = parseFloat(process.env.SLACK_ROUTE_MIN_CONFIDENCE || '0.6');
const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;

// --- Orchestrator spawn branch (opt-in; default OFF so this is inert until set) ---
// When a message matches no running agent, optionally resolve the repo (Spark),
// confirm via Block Kit, and spawn a seeded tmux agent — bounded by an allowlist,
// a per-repo in-flight lock, and a global rate-limit. Also enables restore of
// dormant (reaped) agents from the durable ledger. With the flag off, the
// routing fall-through keeps its original usage-hint behavior (zero change).
const SPAWN_ENABLED = process.env.SLACK_SPAWN_ENABLED === '1';
const AGENTS_NEXUS_DIR = process.env.AGENTS_NEXUS_DIR || join(HOME, 'repos', 'agents-nexus');
const SPAWN_SESSION = process.env.SLACK_SPAWN_SESSION || 'agents';
const SPAWN_ALLOWLIST_FILE = process.env.SLACK_SPAWN_ALLOWLIST_FILE || join(HOME, '.tmux', 'spawnable-repos.json');
const SPAWN_SUMMARIES_FILE = process.env.SLACK_SPAWN_SUMMARIES_FILE || join(HOME, '.tmux', 'spark-summaries.json'); // Spark-derived desc cache (scripts/spark-summary.py); hand-written desc overrides
const SPAWN_MIN_SCORE = parseFloat(process.env.SLACK_SPAWN_MIN_SCORE || '0'); // (legacy Spark resolver) permissive: the confirm card is the gate
const SPAWN_MIN_CONFIDENCE = parseFloat(process.env.SLACK_SPAWN_MIN_CONFIDENCE || '0.5'); // repo classifier floor
const SPAWN_RATE_MAX = parseInt(process.env.SLACK_SPAWN_RATE_MAX || '3', 10);
const SPAWN_RATE_WINDOW_MS = parseInt(process.env.SLACK_SPAWN_RATE_WINDOW_MS || '600000', 10); // 10 min
const SPAWN_CONFIRM_TTL_MS = parseInt(process.env.SLACK_SPAWN_CONFIRM_TTL_MS || '300000', 10); // 5 min
const NUDGE_MIN_INTERVAL_MS = parseInt(process.env.SLACK_NUDGE_MIN_INTERVAL_MS || '3600000', 10); // 1 h
const OPEN_CLAUDE = process.env.SLACK_OPEN_CLAUDE || join(HOME, '.tmux', 'open-claude.sh');
const SPARK_PYTHON = process.env.SLACK_SPARK_PYTHON || join(AGENTS_NEXUS_DIR, 'spark', '.venv', 'bin', 'python');
const SPARK_RESOLVE_SCRIPT = process.env.SLACK_SPARK_RESOLVE || join(AGENTS_NEXUS_DIR, 'scripts', 'spark-resolve.py');
const LEDGER_PYTHON = process.env.SLACK_LEDGER_PYTHON || 'python3';
const LEDGER_SCRIPT = process.env.SLACK_LEDGER_SCRIPT || join(AGENTS_NEXUS_DIR, 'scripts', 'agent-ledger.py');
const LEDGER_FILE = process.env.AGENT_LEDGER || join(HOME, '.tmux', 'agent-ledger.jsonl');

// Boot guard: on an un-provisioned box this is a clean no-op so launchd doesn't thrash.
if (!BOT_TOKEN || !APP_TOKEN) {
  console.log('[slack-bridge] SLACK_BOT_TOKEN / SLACK_APP_TOKEN not set — nothing to do, exiting 0.');
  process.exit(0);
}

const web = new WebClient(BOT_TOKEN);
const socket = new SocketModeClient({ appToken: APP_TOKEN, logLevel: LogLevel.INFO });

// Track Socket Mode connection state ourselves — @slack/socket-mode v2 fires
// 'connected'/'disconnected' events but does not expose a stable `.connected`
// property, so /health derives liveness from these handlers.
let socketConnected = false;

// Identity of our own bot, so we never act on our own posts (set at startup).
let selfUserId = null;
let selfBotId = null;

// ---------------------------------------------------------------------------
// Thread map: thread_ts -> { name, channel, pane, ts, createdAt }
// Keyed by the bot's parent-message ts. We store the agent NAME (stable) and
// re-resolve to a slot at delivery time, since slot numbers can drift.
// ---------------------------------------------------------------------------
let threadMap = loadThreadMap();

function loadThreadMap() {
  try {
    if (existsSync(THREAD_MAP_PATH)) {
      const obj = JSON.parse(readFileSync(THREAD_MAP_PATH, 'utf8'));
      return new Map(Object.entries(obj));
    }
  } catch (e) {
    console.error(`[slack-bridge] could not read thread map: ${e.message}`);
  }
  return new Map();
}

function saveThreadMap() {
  // Drop expired entries, then atomic write (tmp + rename).
  const now = Date.now();
  for (const [ts, v] of threadMap) {
    if (v.createdAt && now - v.createdAt > THREAD_TTL_MS) threadMap.delete(ts);
  }
  try {
    const tmp = `${THREAD_MAP_PATH}.tmp`;
    writeFileSync(tmp, JSON.stringify(Object.fromEntries(threadMap), null, 2));
    renameSync(tmp, THREAD_MAP_PATH);
  } catch (e) {
    console.error(`[slack-bridge] could not write thread map: ${e.message}`);
  }
}

// ---------------------------------------------------------------------------
// Registry: read ~/.tmux/registry/* (same format the arbiter parses).
// Returns [{ name, slot, pane }]. Slots are looked up fresh on every call.
// ---------------------------------------------------------------------------
function loadRegistry() {
  const out = [];
  if (!existsSync(REGISTRY_DIR)) return out;
  for (const f of readdirSync(REGISTRY_DIR)) {
    try {
      const c = readFileSync(join(REGISTRY_DIR, f), 'utf8');
      const name = (c.match(/^NAME=(.*)$/m) || [])[1];
      const slot = (c.match(/^SLOT=(\d+)/m) || [])[1];
      const pane = (c.match(/^PANE_ID=(.*)$/m) || [])[1];
      const cwd = (c.match(/^CWD=(.*)$/m) || [])[1];
      if (name && slot) out.push({ name: name.trim(), slot: slot.trim(), pane: (pane || '').trim(), cwd: (cwd || '').trim() });
    } catch { /* ignore unreadable registry file */ }
  }
  return out;
}

function resolveByName(name) {
  const lower = name.toLowerCase();
  return loadRegistry().find((a) => a.name.toLowerCase() === lower) || null;
}

function resolveBySlot(slot) {
  return loadRegistry().find((a) => a.slot === String(slot)) || null;
}

function liveAgentList() {
  const reg = loadRegistry();
  if (!reg.length) return '_no agents currently active_';
  return reg.map((a) => `\`${a.name}\` (slot ${a.slot})`).join(', ');
}

// ---------------------------------------------------------------------------
// Smart routing: classify an unaddressed message to the most likely agent.
// The Anthropic SDK is dynamic-imported so a deploy that hasn't run
// `npm install` yet (the dep is new) degrades to "ask" instead of crashing.
// ---------------------------------------------------------------------------
let _anthropic = null;          // cached client; false once we know it's unavailable
async function getAnthropic() {
  if (_anthropic !== null) return _anthropic || null;
  if (!ANTHROPIC_API_KEY) { _anthropic = false; return null; }
  try {
    const { default: Anthropic } = await import('@anthropic-ai/sdk');
    _anthropic = new Anthropic({ apiKey: ANTHROPIC_API_KEY });
  } catch (e) {
    console.error(`[slack-bridge] routing disabled — @anthropic-ai/sdk unavailable: ${e.message}`);
    _anthropic = false;
  }
  return _anthropic || null;
}

// Returns { agent, confidence, reason } or null. `agent` is "" when no clear match.
async function classifyTarget(text, agents) {
  const client = await getAnthropic();
  if (!client) return null;
  const roster = agents.map((a) => `- ${a.name}${a.cwd ? ` (${a.cwd})` : ''}`).join('\n');
  try {
    const resp = await client.messages.create({
      model: ROUTE_MODEL,
      max_tokens: 256,
      system: 'You route Slack messages to coding agents. You are given the list of currently active agents (name + working directory) and one message that did NOT explicitly name an agent. Decide which single agent the message is most likely directed at, by matching the message topic to an agent\'s repo/working directory. Set agent to the exact agent name, or "" if no agent is a clear match. confidence is 0..1.',
      messages: [{ role: 'user', content: `Active agents:\n${roster}\n\nMessage:\n${text}` }],
      output_config: {
        format: {
          type: 'json_schema',
          schema: {
            type: 'object',
            properties: {
              agent: { type: 'string' },
              confidence: { type: 'number' },
              reason: { type: 'string' },
            },
            required: ['agent', 'confidence', 'reason'],
            additionalProperties: false,
          },
        },
      },
    });
    const block = (resp.content || []).find((b) => b.type === 'text');
    return block ? JSON.parse(block.text) : null;
  } catch (e) {
    console.error(`[slack-bridge] routing classify failed: ${e.message}`);
    return null;
  }
}

// Spawn-branch resolver: pick which SPAWNABLE repo a message concerns, from the
// allowlist (name + description). This replaces a Spark index lookup — we only
// ever spawn allowlisted repos, so classifying within that small, described set
// is both sufficient and far more reliable than embedding a one-line message
// against a repo summary. Returns { repo, confidence, reason } or null.
async function classifyRepoForSpawn(text, entries) {
  const client = await getAnthropic();
  if (!client || !entries.length) return null;
  const roster = entries.map((e) => `- ${e.name}${e.desc ? `: ${e.desc}` : ''}`).join('\n');
  try {
    const resp = await client.messages.create({
      model: ROUTE_MODEL,
      max_tokens: 256,
      system: 'You decide which repository a Slack message is about, so a coding agent can be spun up there. You are given a list of spawnable repos (name + description) and one message. Pick the single repo the message most concerns by matching the topic to a repo\'s purpose. Set repo to the exact repo name from the list, or "" if none clearly fits. confidence is 0..1.',
      messages: [{ role: 'user', content: `Spawnable repos:\n${roster}\n\nMessage:\n${text}` }],
      output_config: {
        format: {
          type: 'json_schema',
          schema: {
            type: 'object',
            properties: {
              repo: { type: 'string' },
              confidence: { type: 'number' },
              reason: { type: 'string' },
            },
            required: ['repo', 'confidence', 'reason'],
            additionalProperties: false,
          },
        },
      },
    });
    const block = (resp.content || []).find((b) => b.type === 'text');
    return block ? JSON.parse(block.text) : null;
  } catch (e) {
    console.error(`[slack-bridge] spawn classify failed: ${e.message}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Slack text normalization: strip a leading bot mention, unescape entities,
// flatten <url|label> / <url> links so the agent sees clean text.
// ---------------------------------------------------------------------------
function cleanSlackText(text) {
  if (!text) return '';
  let t = text;
  // leading "<@U…>" mention(s)
  t = t.replace(/^\s*(<@[^>]+>\s*)+/, '');
  // <url|label> -> label ; <url> -> url ; <#C…|name> -> #name
  t = t.replace(/<([^>|]+)\|([^>]+)>/g, '$2').replace(/<([^>]+)>/g, '$1');
  // HTML entities Slack escapes
  t = t.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  return t.trim();
}

// ---------------------------------------------------------------------------
// Delivery: re-resolve the agent name to a current slot, then send-keys via
// the existing CLI. Returns { ok, slot, error }.
// ---------------------------------------------------------------------------
function deliverToPane(pane, text) {
  try {
    execFileSync(AGENT_SEND, [pane, text], { encoding: 'utf8', timeout: 5000 });
    return { ok: true, pane };
  } catch (e) {
    const msg = (e.stdout || '').toString().trim() || e.message;
    return { ok: false, pane, error: msg };
  }
}

function deliverToName(name, text) {
  const agent = resolveByName(name);
  if (!agent) return { ok: false, error: `agent \`${name}\` is no longer active` };
  // Prefer the exact pane id — registry slots drift and collide across windows.
  return agent.pane ? deliverToPane(agent.pane, text) : deliverToSlot(agent.slot, text);
}

function deliverToSlot(slot, text) {
  try {
    execFileSync(AGENT_SEND, [String(slot), text], { encoding: 'utf8', timeout: 5000 });
    return { ok: true, slot: String(slot) };
  } catch (e) {
    const msg = (e.stdout || '').toString().trim() || e.message;
    return { ok: false, slot: String(slot), error: msg };
  }
}

async function react(channel, ts, name) {
  try { await web.reactions.add({ channel, timestamp: ts, name }); } catch { /* dup / perms */ }
}

// Mirror a Slack-originated answer onto the agent's terminal — a tmux status-line
// flash on the target pane — so someone watching the CLI sees what was answered
// remotely. Best-effort and non-blocking (tmux may be off the bridge's PATH).
function flashPane(pane, label) {
  if (!pane) return;
  try {
    execFileSync('tmux', ['display-message', '-d', '6000', '-t', pane, `↩ Slack: ${label}`], { timeout: 3000 });
  } catch { /* tmux unreachable / pane gone — non-critical */ }
}

// Human-readable label for the flash, from the delivered keystroke/text.
function answerLabel(text) {
  const t = String(text).trim();
  if (t === '1') return 'approved';
  if (t === '2') return "approved (won't ask again)";
  if (t === '3') return 'denied';
  return `“${t.slice(0, 50)}”`;
}

// Current @wait_since on the pane's window (set by the Notification hook when a
// prompt appears, unset by PreToolUse once answered). We compare it to the value
// captured when the card was posted, so a Slack answer only lands on the SAME
// prompt — never a stale card or a different/newer prompt on that pane.
function paneWaitSince(pane) {
  if (!pane) return null;
  try {
    return execFileSync('tmux', ['display-message', '-t', pane, '-p', '#{@wait_since}'], { encoding: 'utf8', timeout: 3000 }).trim();
  } catch { return null; }
}

// ---------------------------------------------------------------------------
// Per-agent threads. Each agent gets ONE anchor message in #nexus; its requests
// post as replies under it. All state is derived from threadMap (each request
// entry carries `root` = its anchor ts) — an anchor exists exactly while the
// agent has >=1 pending request. On resolve we delete the request card (and the
// anchor when its last request clears), so the channel only shows live asks.
// ---------------------------------------------------------------------------
function rootForAgent(name) {
  for (const v of threadMap.values()) if (v.name === name && v.root) return v.root;
  return null;
}
function pendingCount(rootTs) {
  let n = 0; for (const v of threadMap.values()) if (v.root === rootTs) n += 1; return n;
}
function isAgentRoot(ts) {
  for (const v of threadMap.values()) if (v.root === ts) return true; return false;
}
function latestPendingForRoot(rootTs) {
  let best = null;
  for (const [ts, v] of threadMap) {
    if (v.root === rootTs && (!best || Number(ts) > Number(best.ts))) best = { ts, ...v };
  }
  return best;
}

// Deliver an answer to a tracked request, then remove the card (and the anchor if
// it was the last pending one). `deliverText` is the menu digit or free-form text.
async function resolveRequest(reqTs, channel, deliverText) {
  const entry = threadMap.get(reqTs);
  if (!entry) return { ok: false, error: 'this request is no longer tracked' };
  // Same-prompt guard: only inject if the pane is still sitting at THE prompt this
  // card was posted for. If it moved on (answered locally, a newer prompt, or idle),
  // its @wait_since won't match — so drop the card instead of firing a stray
  // keystroke into live work (the cross-window "phantom reject" bug).
  if (entry.pane && entry.wait_since && paneWaitSince(entry.pane) !== entry.wait_since) {
    threadMap.delete(reqTs);
    const ch = channel || entry.channel;
    try { await web.chat.delete({ channel: ch, ts: reqTs }); } catch { /* already gone / perms */ }
    if (entry.root && pendingCount(entry.root) === 0) {
      try { await web.chat.delete({ channel: entry.channel, ts: entry.root }); } catch { /* ignore */ }
    }
    saveThreadMap();
    return { ok: false, stale: true, error: 'that prompt is no longer active (already handled) — nothing delivered' };
  }
  const res = entry.pane ? deliverToPane(entry.pane, deliverText) : deliverToName(entry.name, deliverText);
  if (res.ok) {
    flashPane(entry.pane, answerLabel(deliverText));   // mirror the answer onto the agent's terminal
    threadMap.delete(reqTs);
    const ch = channel || entry.channel;
    try { await web.chat.delete({ channel: ch, ts: reqTs }); } catch { /* already gone / perms */ }
    if (entry.root && pendingCount(entry.root) === 0) {
      try { await web.chat.delete({ channel: entry.channel, ts: entry.root }); } catch { /* ignore */ }
    }
    saveThreadMap();
  }
  return res;
}

async function replyInThread(channel, thread_ts, text) {
  try { await web.chat.postMessage({ channel, thread_ts, text }); } catch (e) {
    console.error(`[slack-bridge] reply failed: ${e.message}`);
  }
}

// ---------------------------------------------------------------------------
// Proactive prune sweep. A card is normally removed when answered IN SLACK, but
// a prompt answered locally in the CLI/tmux (PreToolUse clears @wait_since) or an
// agent whose window was closed leaves the card orphaned in #nexus until TTL —
// the "12 stale prompts in a thread" problem. This applies the SAME staleness
// test as the same-prompt guard (paneWaitSince mismatch / pane gone) across all
// tracked cards and deletes the dead ones, so the channel only shows live asks.
async function pruneStaleCards() {
  let changed = false;
  // Snapshot the entries up front — we delete from threadMap while iterating.
  for (const [reqTs, entry] of [...threadMap]) {
    // Without the guard fields we can't tell live from stale — leave to TTL.
    if (!entry.pane || !entry.wait_since) continue;
    // paneWaitSince returns null if the pane/window is gone, or the current
    // @wait_since (different once the prompt is answered locally or superseded).
    if (paneWaitSince(entry.pane) === entry.wait_since) continue;  // still the live prompt — keep
    threadMap.delete(reqTs);
    changed = true;
    try { await web.chat.delete({ channel: entry.channel, ts: reqTs }); } catch { /* already gone / perms */ }
    if (entry.root && pendingCount(entry.root) === 0) {
      try { await web.chat.delete({ channel: entry.channel, ts: entry.root }); } catch { /* ignore */ }
    }
  }
  if (changed) saveThreadMap();
}

// Reentrancy-guarded loop body for setInterval — a slow sweep (many chat.delete
// calls) must not overlap the next tick.
let pruning = false;
async function pruneLoop() {
  if (pruning || threadMap.size === 0) return;
  pruning = true;
  try { await pruneStaleCards(); }
  catch (e) { console.error(`[slack-bridge] prune sweep error: ${e.message}`); }
  finally { pruning = false; }
}

// ---------------------------------------------------------------------------
// Orchestrator: confirm-gated spawn branch + resilience (restore/nudge).
// All state is in-memory; guardrails are evaluated before any tmux new-window.
// Inert unless SPAWN_ENABLED. See orchestrator.js for the pure pieces.
// ---------------------------------------------------------------------------
const inFlight = new Set();        // repos with a spawn in progress OR a known live agent (lock)
let spawnTimestamps = [];          // epoch-ms of recent spawns (global rate-limit ring)
const pendingSpawns = new Map();   // confirm-card ts -> { repo, path, channel, rootTs, requester, createdAt }
let lastNudgeAt = 0;

function ledgerCmd(args) {
  return orch.ledger(args, { python: LEDGER_PYTHON, script: LEDGER_SCRIPT, env: { AGENT_LEDGER: LEDGER_FILE } });
}

// Seed the lock set from the durable ledger's live entries (reconciled against
// the live registry) so a repo with a known agent is locked across a restart.
async function seedLocksOnStartup() {
  if (!SPAWN_ENABLED) return;
  try {
    const state = await orch.ledger(['reconcile', '--registry-dir', REGISTRY_DIR, '--json'],
      { python: LEDGER_PYTHON, script: LEDGER_SCRIPT, env: { AGENT_LEDGER: LEDGER_FILE } });
    for (const rec of (Array.isArray(state) ? state : [])) {
      if (rec && rec.state === 'live' && (rec.repo || rec.name)) inFlight.add(rec.repo || rec.name);
    }
    console.log(`[slack-bridge] orchestrator: seeded ${inFlight.size} lock(s) from ledger`);
  } catch (e) {
    console.error(`[slack-bridge] orchestrator: lock seed failed: ${e.message}`);
  }
}

// Is a spawn for this repo currently disallowed by the lock? (in-flight, or a
// live agent already serves it).
function repoLocked(repo, repoPath) {
  if (inFlight.has(repo)) return true;
  return orch.repoHasLiveAgent(loadRegistry(), repo, repoPath);
}

// Explicit spawn intent: a direct request to start an agent ("spin up / start /
// launch / spawn an agent on X"), as opposed to an ambient message about a repo.
// When present, the spawn flow runs BEFORE (and instead of) routing-to-a-running-
// agent, so an explicit ask is never silently swallowed by a live agent.
const SPAWN_INTENT_RE = /\b((spin|fire|boot)\s*up|spawn|launch|stand\s*up|kick\s*off|start|create|open|set\s*up|get|need|want)\s+(me\s+)?(an?|the|a\s+new|another|some)?\s*(new\s+|fresh\s+)?(claude\s+|coding\s+)?agent\b/i;
function hasSpawnIntent(text) { return SPAWN_INTENT_RE.test(text); }

function spawnableListMsg(entries) {
  return entries.length ? entries.map((e) => `\`${e.name}\``).join(', ') : '_none configured_';
}

// Spawn branch: resolve the repo (classify against the allowlist), gate it, and
// post a confirm card. `opts.explicit` = the user directly asked to spawn, so on
// a miss we explain what's spawnable (and return true) instead of falling through
// to routing / the generic usage hint.
async function offerSpawn(channel, threadTs, text, requester, opts = {}) {
  const explicit = !!opts.explicit;
  const allow = orch.loadAllowlist(SPAWN_ALLOWLIST_FILE);
  const entries = orch.mergeSummaries(orch.allowlistEntries(allow), orch.loadSummaries(SPAWN_SUMMARIES_FILE));
  console.log(`[orch-debug] offerSpawn${explicit ? '(explicit)' : ''} allowlist=[${entries.map((e) => e.name).join(',')}]`);
  if (!entries.length) {
    if (explicit) { await replyInThread(channel, threadTs, ':warning: no repos are on the spawnable allowlist yet.'); return true; }
    return false;                                    // nothing spawnable -> usage hint
  }
  const verdict = await classifyRepoForSpawn(text, entries);
  console.log(`[orch-debug] offerSpawn classify -> ${JSON.stringify(verdict)} (floor ${SPAWN_MIN_CONFIDENCE})`);
  if (!verdict || !verdict.repo || Number(verdict.confidence) < SPAWN_MIN_CONFIDENCE) {
    if (explicit) {
      await replyInThread(channel, threadTs,
        `:information_source: I can spin up an agent, but couldn't match that to a spawnable repo. Spawnable: ${spawnableListMsg(entries)}. Name one — e.g. \`spawn ${entries[0].name}\`.`);
      return true;
    }
    return false;                                    // no confident repo -> caller falls back to usage hint
  }
  const match = orch.matchAllowlist(allow, verdict.repo);
  if (!match) {
    console.log(`[orch-debug] classifier named non-allowlisted repo ${verdict.repo}`);
    if (explicit) { await replyInThread(channel, threadTs, `:information_source: \`${verdict.repo}\` isn't on the spawnable allowlist. Spawnable: ${spawnableListMsg(entries)}.`); return true; }
    return false;
  }
  return postSpawnConfirm(channel, threadTs, match, text, requester, verdict.confidence);
}

// Post the "Spin up an agent in <repo>? [🚀/No]" card for an already-resolved repo
// (shared by the classifier path and the explicit `spawn <repo>` command). Honors
// the per-repo lock; acquires it at post time, releases on No/timeout/failure.
async function postSpawnConfirm(channel, threadTs, match, seed, requester, score) {
  // Already serving this repo? Offer to address it instead of spawning a twin.
  if (repoLocked(match.name, match.path)) {
    console.log(`[orch-debug] ${match.name} locked/running -> address-it reply`);
    await replyInThread(channel, threadTs,
      `:information_source: \`${match.name}\` already has an agent (or a spawn in progress) — address it with \`${match.name}: …\`.`);
    return true;
  }
  inFlight.add(match.name);
  try {
    console.log(`[orch-debug] posting spawn confirm card for ${match.name}`);
    const blocks = orch.confirmCard({ repo: match.name, cwd: match.path, score, requester });
    const posted = await web.chat.postMessage({ channel, thread_ts: threadTs,
      text: `Spin up an agent in ${match.name}?`, blocks });
    pendingSpawns.set(posted.ts, {
      repo: match.name, path: match.path, seed, channel, rootTs: threadTs, requester, createdAt: Date.now(),
    });
  } catch (e) {
    inFlight.delete(match.name);                     // posting failed — don't leak the lock
    console.error(`[slack-bridge] spawn confirm post failed: ${e.message}`);
  }
  return true;
}

// Poll the registry until an agent for `repo` registers (so the live-agent check
// can take over the lock), or give up after ~12s.
async function awaitRegistration(repo, repoPath, tries = 24) {
  for (let i = 0; i < tries; i += 1) {
    if (orch.repoHasLiveAgent(loadRegistry(), repo, repoPath)) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

// Execute an approved spawn: rate-limit (re-checked at approval), tmux new-window,
// ledger, identity reply. Returns a human note for the card. Releases the lock.
async function performSpawn(pending) {
  const { repo, path: repoPath, channel, rootTs, requester, restore } = pending;
  const now = Date.now();
  const { recent, allowed } = orch.rateState(spawnTimestamps, SPAWN_RATE_MAX, SPAWN_RATE_WINDOW_MS, now);
  spawnTimestamps = recent;
  if (!allowed) {
    inFlight.delete(repo);
    return `:no_entry: rate-limited — ${SPAWN_RATE_MAX} spawns per ${Math.round(SPAWN_RATE_WINDOW_MS / 60000)} min reached. Try again shortly.`;
  }
  const res = await orch.spawnWindow({
    session: SPAWN_SESSION, name: repo, cwd: repoPath, slug: repo,
    seed: restore ? '' : pending.seed, restoreCheckpoint: restore ? pending.checkpoint : '',
    openClaude: OPEN_CLAUDE,
  });
  if (!res.ok) {
    inFlight.delete(repo);
    return `:warning: spawn failed: ${res.error}`;
  }
  spawnTimestamps.push(now);
  await ledgerCmd([restore ? 'restore' : 'spawn', '--repo', repo, '--name', repo,
    ...(pending.seed && !restore ? ['--seed', pending.seed] : []),
    ...(res.pane ? ['--pane', res.pane] : []), ...(res.slot ? ['--slot', res.slot] : [])]);
  // Hold the lock until the agent registers, then let the live-agent check own it.
  awaitRegistration(repo, repoPath).then((ok) => {
    inFlight.delete(repo);
    if (!ok) console.warn(`[slack-bridge] ${repo} spawned but did not register within timeout`);
  });
  const slotNote = res.slot ? ` (slot ${res.slot})` : '';
  if (requester) {
    await replyInThread(channel, rootTs, `:rocket: <@${requester}> spun up \`${repo}\`${slotNote} — it's starting now.`);
  }
  return restore
    ? `:leftwards_arrow_with_hook: restored \`${repo}\`${slotNote} from checkpoint`
    : `:rocket: spawned \`${repo}\`${slotNote}`;
}

// Yes/No on a spawn confirm card.
async function handleSpawnAction(action, channel, ts, body) {
  const pending = pendingSpawns.get(ts);
  if (!pending) {                                    // card expired or bridge restarted
    try { await web.chat.update({ channel, ts, text: 'expired',
      blocks: orch.resolvedCard(body.message, ':hourglass: this offer expired — message again to retry') }); } catch { /* ignore */ }
    return;
  }
  pendingSpawns.delete(ts);
  if (action.action_id === 'spawn:no') {
    inFlight.delete(pending.repo);                   // release the lock
    try { await web.chat.update({ channel, ts, text: 'cancelled',
      blocks: orch.resolvedCard(body.message, ':x: cancelled — no agent spawned') }); } catch { /* ignore */ }
    return;
  }
  pending.requester = (body.user && body.user.id) || pending.requester;
  const note = await performSpawn(pending);
  try { await web.chat.update({ channel, ts, text: 'done', blocks: orch.resolvedCard(body.message, note) }); } catch { /* ignore */ }
}

// Restore a dormant agent from the ledger, gated by the same guardrails.
async function doRestore(channel, threadTs, repo, requester) {
  const entry = await ledgerCmd(['get', '--repo', repo, '--json']);
  if (!entry || !entry.repo) {
    await replyInThread(channel, threadTs, `:information_source: no dormant agent recorded for \`${repo}\`.`);
    return;
  }
  if (entry.state === 'live' || repoLocked(repo, null)) {
    await replyInThread(channel, threadTs, `:information_source: \`${repo}\` is already running — address it with \`${repo}: …\`.`);
    return;
  }
  const match = orch.matchAllowlist(orch.loadAllowlist(SPAWN_ALLOWLIST_FILE), repo);
  if (!match) {
    await replyInThread(channel, threadTs, `:warning: \`${repo}\` is no longer on the spawnable-repo allowlist — cannot restore.`);
    return;
  }
  inFlight.add(match.name);
  const note = await performSpawn({
    repo: match.name, path: match.path, channel, rootTs: threadTs, requester,
    restore: true, checkpoint: '',   // open-claude rehydrates by slug (memory recall surfaces the reap note)
  });
  await replyInThread(channel, threadTs, note);
}

async function handleRestoreAction(action, channel, ts, body) {
  const repo = action.value;
  const requester = body.user && body.user.id;
  await doRestore(channel, ts, repo, requester);
  // Refresh the nudge card so the restored repo's button drops off.
  const dormant = await ledgerCmd(['list', '--state', 'dormant', '--json']);
  try {
    if (Array.isArray(dormant) && dormant.length) {
      await web.chat.update({ channel, ts, text: 'restore', blocks: orch.nudgeCard(dormant) });
    } else {
      await web.chat.update({ channel, ts, text: 'restored',
        blocks: orch.resolvedCard(body.message, ':white_check_mark: all dormant agents handled') });
    }
  } catch { /* ignore */ }
}

// Reconnect nudge: when dormant agents exist, offer to restore them — at most
// once per NUDGE_MIN_INTERVAL, triggered by an inbound message. Never auto-restores.
async function maybeNudge(channel) {
  if (!SPAWN_ENABLED || !NEXUS_CHANNEL) return;
  const now = Date.now();
  if (now - lastNudgeAt < NUDGE_MIN_INTERVAL_MS) return;
  const dormant = await ledgerCmd(['list', '--state', 'dormant', '--json']);
  if (!Array.isArray(dormant) || !dormant.length) return;
  lastNudgeAt = now;
  try { await web.chat.postMessage({ channel, text: `${dormant.length} dormant agent(s) — restore?`, blocks: orch.nudgeCard(dormant) }); }
  catch (e) { console.error(`[slack-bridge] nudge failed: ${e.message}`); }
}

// Expire stale confirm cards so a never-clicked card never holds a repo's lock.
async function expirePendingSpawns() {
  const now = Date.now();
  for (const [ts, p] of [...pendingSpawns]) {
    if (now - p.createdAt < SPAWN_CONFIRM_TTL_MS) continue;
    pendingSpawns.delete(ts);
    inFlight.delete(p.repo);
    try { await web.chat.update({ channel: p.channel, ts, text: 'expired',
      blocks: [{ type: 'section', text: { type: 'mrkdwn', text: `:hourglass: spawn offer for \`${p.repo}\` expired` } }] }); } catch { /* ignore */ }
  }
}

// Orchestrator slash-style commands typed in #nexus: `restore [repo]`,
// `keep <name> [on|off]`. Returns true if the message was a handled command.
async function orchestratorCommand(event, channel, text) {
  if (!SPAWN_ENABLED) return false;
  // Strict forms only, so a natural-language message that happens to start with
  // "restore"/"keep" (e.g. "restore the prod DB please") falls through to the
  // spawn branch instead of being mis-parsed as a command.
  const mRestore = text.match(/^restore(?:\s+(\S+))?\s*$/i);
  if (mRestore) {
    const repo = mRestore[1];
    if (repo) { await doRestore(channel, event.ts, repo, event.user); return true; }
    const dormant = await ledgerCmd(['list', '--state', 'dormant', '--json']);
    if (Array.isArray(dormant) && dormant.length) {
      await web.chat.postMessage({ channel, thread_ts: event.ts, text: 'restore which?', blocks: orch.nudgeCard(dormant) });
    } else {
      await replyInThread(channel, event.ts, ':information_source: no dormant agents to restore.');
    }
    return true;
  }
  const mKeep = text.match(/^keep\s+(\S+)(?:\s+(on|off|1|0|yes|no|true|false))?\s*$/i);
  if (mKeep) {
    const args = mKeep[2] ? [mKeep[1], mKeep[2]] : [mKeep[1]];
    const res = deliverViaScript(join(HOME, '.tmux', 'agent-keep.sh'), args);
    await replyInThread(channel, event.ts, res.ok ? `:pushpin: ${res.out || 'done'}` : `:warning: ${res.error}`);
    return true;
  }
  // `spawn <repo> [seed…]` — explicit, by-name spawn (no classification). The
  // rest of the line, if any, becomes the seed prompt for the new agent.
  const mSpawn = text.match(/^spawn\s+(\S+)(?:\s+([\s\S]+))?$/i);
  if (mSpawn) {
    const repoArg = mSpawn[1];
    const seed = (mSpawn[2] || '').trim();
    const allow = orch.loadAllowlist(SPAWN_ALLOWLIST_FILE);
    const match = orch.matchAllowlist(allow, repoArg);
    if (!match) {
      await replyInThread(channel, event.ts,
        `:information_source: \`${repoArg}\` isn't on the spawnable allowlist. Spawnable: ${spawnableListMsg(orch.allowlistEntries(allow))}.`);
      return true;
    }
    await postSpawnConfirm(channel, event.ts, match, seed || `Start work in ${match.name}.`, event.user, undefined);
    return true;
  }
  return false;
}

// Small synchronous helper to run a local script and capture stdout (keep cmd).
function deliverViaScript(script, args) {
  try {
    const out = execFileSync(script, args, { encoding: 'utf8', timeout: 5000 }).trim();
    return { ok: true, out };
  } catch (e) {
    return { ok: false, error: (e.stdout || e.message || '').toString().trim() };
  }
}

// ---------------------------------------------------------------------------
// Inbound message handler
// ---------------------------------------------------------------------------
const IGNORED_SUBTYPES = new Set([
  'bot_message', 'message_changed', 'message_deleted',
  'channel_join', 'channel_leave', 'channel_topic', 'channel_purpose',
]);

// Claude Code permission prompts (and elicitation dialogs) are a numbered select
// menu — by default: 1 = Yes, 2 = Yes + don't ask again, 3 = No. agent-send.sh
// sends a bare digit as a raw selection keystroke, so a typed word like "yes"
// never picks an option. For threads the hook tagged as a permission prompt, map
// a natural-language reply to that digit; return null to deliver verbatim
// (free-form guidance, or a normal question thread).
const PERMISSION_KINDS = new Set(['permission_prompt', 'elicitation_dialog']);

function permissionReplyToDigit(text) {
  const t = text.trim().toLowerCase().replace(/[.!\s]+$/, '');
  if (/^[123]$/.test(t)) return t;                                   // already a digit
  if (/(don'?t ask( again)?|always|remember (this|me))/.test(t)) return '2';
  if (/^(y|yes|yep|yeah|yup|ok|okay|sure|approve[d]?|allow|accept|proceed|go( ahead)?|do it)$/.test(t)) return '1';
  if (/^(n|no|nope|deny|denied|reject|decline|cancel|stop)$/.test(t)) return '3';
  return null;                                                       // no confident mapping
}

socket.on('message', async ({ event, ack }) => {
  try { await ack(); } catch { /* already acked */ }
  await handleMessage(event);
});

// Emoji reactions on a tracked permission-prompt message answer the menu directly:
// :one:/:two:/:three: -> that option; ✅/👍 -> approve (1); ❌/👎 -> deny (3).
const REACTION_DIGIT = {
  one: '1', two: '2', three: '3',
  white_check_mark: '1', heavy_check_mark: '1', '+1': '1', thumbsup: '1',
  x: '3', no_entry: '3', '-1': '3', thumbsdown: '3',
};

socket.on('reaction_added', async ({ event, ack }) => {
  try { await ack(); } catch { /* already acked */ }
  await handleReaction(event);
});

// Approve/Deny button clicks on a permission card. The button value is the menu
// digit; deliver it to the thread's agent, then resolve the card (drop buttons,
// show the outcome). Requires the Slack app to have Interactivity enabled.
socket.on('interactive', async ({ body, ack }) => {
  try { await ack(); } catch { /* already acked */ }
  await handleInteractive(body);
});

async function handleInteractive(body) {
  if (!body || body.type !== 'block_actions') return;
  const action = (body.actions || [])[0];
  if (!action) return;
  const chan = body.channel && body.channel.id;
  const mts = body.message && body.message.ts;
  // Orchestrator buttons (spawn confirm / restore) — routed before the
  // permission-digit path so their action_ids never collide with 1/2/3.
  if (SPAWN_ENABLED && typeof action.action_id === 'string') {
    if (action.action_id.startsWith('spawn:')) { await handleSpawnAction(action, chan, mts, body); return; }
    if (action.action_id.startsWith('restore:')) { await handleRestoreAction(action, chan, mts, body); return; }
  }
  const digit = action && action.value;
  if (!['1', '2', '3'].includes(digit)) return;
  const channel = body.channel && body.channel.id;
  const ts = body.message && body.message.ts;
  if (!ts || !threadMap.has(ts)) {
    if (channel && ts) {
      try {
        await web.chat.update({ channel, ts, text: 'request expired',
          blocks: _resolveCard(body.message, ':warning: no longer tracked (bridge restarted) — answer in the terminal') });
      } catch { /* ignore */ }
    }
    return;
  }
  // Deliver + remove the card (and the anchor if it was the last). On failure, keep
  // the card and surface the error in place.
  const res = await resolveRequest(ts, channel, digit);
  // On `stale` the card was already removed (prompt no longer active) — nothing to do.
  if (!res.ok && !res.stale && channel) {
    try { await web.chat.update({ channel, ts, text: 'error', blocks: _resolveCard(body.message, `:warning: ${res.error}`) }); } catch { /* ignore */ }
  }
}

// Rebuild a resolved card: keep the section block(s), drop the action buttons + hint,
// append the outcome as a context line.
function _resolveCard(message, note) {
  const kept = (((message || {}).blocks) || []).filter((b) => b.type === 'section');
  kept.push({ type: 'context', elements: [{ type: 'mrkdwn', text: note }] });
  return kept;
}

async function handleReaction(event) {
  if (!event || event.type !== 'reaction_added') return;
  if (selfUserId && event.user === selfUserId) return;        // ignore the bot's own reactions
  const digit = REACTION_DIGIT[event.reaction];
  if (!digit) return;
  const item = event.item;
  if (!item || item.type !== 'message' || !threadMap.has(item.ts)) return;  // tracked card?
  const res = await resolveRequest(item.ts, item.channel, digit);            // deliver + remove card
  if (!res.ok && !res.stale) {
    try { await replyInThread(item.channel, item.ts, `:warning: ${res.error}`); } catch { /* ignore */ }
  }
}

async function handleMessage(event) {
  if (!event) return;
  // --- self / noise filtering (prevent feedback loops) ---
  if (event.bot_id) return;
  if (event.subtype && IGNORED_SUBTYPES.has(event.subtype)) return;
  if (selfUserId && event.user === selfUserId) return;
  if (!event.text) return;

  // --- scope: only the nexus control channel + direct messages ---
  const inNexus = NEXUS_CHANNEL && event.channel === NEXUS_CHANNEL;
  const isDM = event.channel_type === 'im';
  if (!inNexus && !isDM) return;

  const channel = event.channel;
  const isReply = event.thread_ts && event.thread_ts !== event.ts;
  console.log(`[orch-debug] inbound ${inNexus ? 'nexus' : 'dm'} ${isReply ? 'reply' : 'top'} from ${event.user}: "${String(event.text).slice(0, 70)}"`);

  // Reconnect nudge: opportunistic, self-throttled (>=1h), never auto-restores.
  if (inNexus) maybeNudge(channel).catch(() => {});

  // 1. Reply inside a request thread -> answer that request. The reply may land on
  //    the request card directly (legacy top-level) or on the agent's anchor thread
  //    (per-agent threads) — in which case it answers the latest pending request.
  if (isReply) {
    let reqTs = null, entry = null;
    if (threadMap.has(event.thread_ts)) {
      reqTs = event.thread_ts; entry = threadMap.get(reqTs);
    } else if (isAgentRoot(event.thread_ts)) {
      const p = latestPendingForRoot(event.thread_ts);
      if (p) { reqTs = p.ts; entry = threadMap.get(reqTs); }
    }
    if (entry) {
      const raw = cleanSlackText(event.text);
      let deliverText = raw;
      if (PERMISSION_KINDS.has(entry.kind)) {
        const digit = permissionReplyToDigit(raw);
        if (!digit) {  // free-form text doesn't pick a menu option
          await replyInThread(channel, event.thread_ts,
            ':information_source: reply `1` / `2` / `3` (or use the buttons / a number reaction)');
          return;
        }
        deliverText = digit;
      }
      // resolveRequest delivers to the captured pane, then removes the card (+ anchor
      // if it was the last pending request for that agent).
      const res = await resolveRequest(reqTs, channel, deliverText);
      if (res.ok) {
        await react(channel, event.ts, 'white_check_mark');
      } else if (res.stale) {
        await react(channel, event.ts, 'heavy_check_mark');   // prompt already handled — card removed, nothing injected
      } else {
        await react(channel, event.ts, 'x');
        await replyInThread(channel, event.thread_ts, `:warning: ${res.error}`);
      }
      return;
    }
  }

  // 2. Addressed top-level: "name: text" or "slot: text" (after any @bot mention)
  const cleaned = cleanSlackText(event.text);
  const addr = cleaned.match(/^([A-Za-z0-9][\w.-]*)\s*:\s*([\s\S]+)$/);
  if (addr) {
    const target = addr[1];
    const text = addr[2].trim();
    const agent = /^\d+$/.test(target) ? resolveBySlot(target) : resolveByName(target);
    if (!agent) {
      await replyInThread(channel, event.ts,
        `:warning: no active agent \`${target}\`. Active: ${liveAgentList()}`);
      return;
    }
    const res = agent.pane ? deliverToPane(agent.pane, text) : deliverToSlot(agent.slot, text);
    if (res.ok) {
      flashPane(agent.pane, answerLabel(text));          // mirror the message onto the agent's terminal
      await react(channel, event.ts, 'white_check_mark');
    } else {
      await react(channel, event.ts, 'x');
      await replyInThread(channel, event.ts, `:warning: ${res.error}`);
    }
    return;
  }

  // 2b. Orchestrator commands (`restore [repo]`, `keep <name>`, `spawn <repo>`) —
  //     checked before the classifier so they aren't mis-routed to a running agent.
  if (await orchestratorCommand(event, channel, cleaned)) return;

  // 2c. Explicit spawn intent ("spin up an agent on X") -> go straight to the
  //     spawn flow, BEFORE routing. An explicit ask must win over route-to-a-
  //     running-agent; offerSpawn(explicit) always answers, so we never fall
  //     through to a misdirected route or the generic hint.
  if (SPAWN_ENABLED && hasSpawnIntent(cleaned)) {
    console.log(`[orch-debug] explicit spawn intent -> bypassing routing for "${cleaned.slice(0, 60)}"`);
    try {
      if (await offerSpawn(channel, event.ts, cleaned, event.user, { explicit: true })) return;
    } catch (e) {
      console.error(`[slack-bridge] explicit offerSpawn error: ${e.message}`);
    }
  }

  // 3. Unaddressed / untracked -> try to classify a target, else a usage hint.
  if (ROUTE_ENABLED) {
    const agents = loadRegistry();
    if (agents.length) {
      const verdict = await classifyTarget(cleaned, agents);
      console.log(`[orch-debug] route "${cleaned.slice(0, 60)}" -> classifyTarget ${JSON.stringify(verdict)} (floor ${ROUTE_MIN_CONFIDENCE})`);
      if (verdict && verdict.agent) {
        const match = agents.find((a) => a.name.toLowerCase() === String(verdict.agent).toLowerCase());
        if (match && Number(verdict.confidence) >= ROUTE_MIN_CONFIDENCE) {
          const res = match.pane ? deliverToPane(match.pane, cleaned) : deliverToSlot(match.slot, cleaned);
          console.log(`[orch-debug] routed to ${match.name} delivery=${JSON.stringify(res)}`);
          if (res.ok) {
            flashPane(match.pane, answerLabel(cleaned));
            await react(channel, event.ts, 'white_check_mark');
            await replyInThread(channel, event.ts,
              `:robot_face: routed to \`${match.name}\` (auto · ${Math.round(Number(verdict.confidence) * 100)}%). Use \`name: …\` to override.`);
            return;
          }
          // delivery failed — fall through to the spawn branch / usage hint below
        }
      }
    }
  }

  // 3b. No running agent matched. If enabled, offer to spawn the right one
  //     (resolve repo -> confirm -> spawn). offerSpawn returns false when the
  //     repo can't be resolved, so we fall back to the usage hint.
  if (SPAWN_ENABLED) {
    try {
      if (await offerSpawn(channel, event.ts, cleaned, event.user)) return;
    } catch (e) {
      console.error(`[slack-bridge] offerSpawn error: ${e.message}\n${e.stack}`);
    }
  }

  console.log(`[orch-debug] no match -> usage hint for "${cleaned.slice(0, 60)}"`);
  await replyInThread(channel, event.ts,
    `:information_source: Address an agent with \`name: your message\` (or reply in a thread I started). Active: ${liveAgentList()}`);
}

// ---------------------------------------------------------------------------
// Outbound: localhost HTTP for the Notification hook.
//   POST /notify { name, message, slot?, pane? }  -> post to #nexus, track thread
//   GET  /health
// ---------------------------------------------------------------------------
const httpServer = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');

  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, connected: socketConnected, threads: threadMap.size }));
    return;
  }

  if (req.method === 'POST' && url.pathname === '/notify') {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', async () => {
      res.setHeader('Content-Type', 'application/json');
      try {
        const { name, message, pane, kind, category, summary, wait_since } = JSON.parse(body || '{}');
        if (!name) { res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'name required' })); return; }
        if (!NEXUS_CHANNEL) { res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'SLACK_NEXUS_CHANNEL not set' })); return; }
        // Block Kit card: a section with the [category] + middle-man summary, then
        // (for permission prompts) Approve / Approve+don't-ask / Deny buttons whose
        // values are the menu digits. `text` is the notification fallback.
        const isPerm = (kind || '') === 'permission_prompt';
        const detail = summary
          ? `${category ? `*[${String(category).slice(0, 60)}]*\n` : ''}:robot_face: ${String(summary).slice(0, 1500)}`
          : (message || 'needs your input').toString().slice(0, 1500);
        const blocks = [
          { type: 'section', text: { type: 'mrkdwn', text: `:hourglass_flowing_sand: *${name}* needs input\n${detail}` } },
        ];
        if (isPerm) {
          blocks.push({
            type: 'actions', block_id: 'perm_actions',
            elements: [
              { type: 'button', action_id: 'perm:1', style: 'primary', value: '1', text: { type: 'plain_text', text: '✅ Approve', emoji: true } },
              { type: 'button', action_id: 'perm:2', value: '2', text: { type: 'plain_text', text: "Approve + don't ask", emoji: true } },
              { type: 'button', action_id: 'perm:3', style: 'danger', value: '3', text: { type: 'plain_text', text: '❌ Deny', emoji: true } },
            ],
          });
        }
        blocks.push({ type: 'context', elements: [{ type: 'mrkdwn', text: isPerm ? '_or reply in thread · react :one: / :two: / :three:_' : '_reply in this thread to answer_' }] });
        const fallback = `${name} needs input: ${(summary || message || '').toString().slice(0, 200)}`;
        // Per-agent thread: post under the agent's anchor (create it if this is the
        // agent's first pending request), so #nexus top-level stays one line per agent.
        let rootTs = rootForAgent(name);
        if (!rootTs) {
          const anchor = await web.chat.postMessage({ channel: NEXUS_CHANNEL, text: `:thread: *${name}* — waiting on you` });
          rootTs = anchor.ts;
        }
        const posted = await web.chat.postMessage({ channel: NEXUS_CHANNEL, thread_ts: rootTs, text: fallback, blocks });
        threadMap.set(posted.ts, { name, channel: NEXUS_CHANNEL, pane: pane || '', kind: kind || '', wait_since: wait_since || '', root: rootTs, ts: posted.ts, createdAt: Date.now() });
        saveThreadMap();
        res.writeHead(200);
        res.end(JSON.stringify({ ok: true, ts: posted.ts }));
      } catch (e) {
        res.writeHead(500);
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ ok: false, error: 'not found' }));
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
(async () => {
  try {
    const who = await web.auth.test();
    selfUserId = who.user_id;
    selfBotId = who.bot_id;
    console.log(`[slack-bridge] authenticated as ${who.user} (${selfUserId}) in ${who.team}`);
  } catch (e) {
    console.error(`[slack-bridge] auth.test failed — check SLACK_BOT_TOKEN: ${e.message}`);
    process.exit(1);
  }

  if (!NEXUS_CHANNEL) {
    console.warn('[slack-bridge] SLACK_NEXUS_CHANNEL not set — only DMs will be handled and /notify will fail.');
  }

  httpServer.listen(PORT, '127.0.0.1', () => {
    console.log(`[slack-bridge] notify endpoint: http://127.0.0.1:${PORT}/notify`);
  });

  socket.on('connected', () => { socketConnected = true; console.log('[slack-bridge] socket mode connected'); });
  socket.on('disconnected', () => { socketConnected = false; console.log('[slack-bridge] socket mode disconnected'); });

  // Background sweep to retire cards answered locally / for closed windows.
  if (NEXUS_CHANNEL) {
    setInterval(pruneLoop, PRUNE_INTERVAL_MS);
    console.log(`[slack-bridge] stale-card prune sweep every ${PRUNE_INTERVAL_MS}ms`);
  }

  // Orchestrator: seed per-repo locks from the ledger and sweep expired confirm
  // cards so a never-clicked offer never holds a repo's lock forever.
  if (SPAWN_ENABLED) {
    await seedLocksOnStartup();
    setInterval(() => { expirePendingSpawns().catch(() => {}); }, 60000);
    console.log(`[slack-bridge] orchestrator spawn branch ENABLED (session=${SPAWN_SESSION}, allowlist=${SPAWN_ALLOWLIST_FILE})`);
  } else {
    console.log('[slack-bridge] orchestrator spawn branch disabled (set SLACK_SPAWN_ENABLED=1 to enable)');
  }

  await socket.start();
})();
