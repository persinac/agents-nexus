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
  readFileSync, writeFileSync, renameSync, existsSync, readdirSync, watch,
} from 'fs';
import { homedir, hostname } from 'os';
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

// `status` / `who` command: minutes a "working" agent can go without running a
// tool before it's flagged "stuck" in the roll-up. Read-only command, always on.
const STATUS_STUCK_MIN = parseInt(process.env.SLACK_STATUS_STUCK_MIN || '10', 10);

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

// --- Inter-agent bus (opt-in; default OFF) ---
// Routes agent-to-agent messages that can't be delivered locally through Slack,
// so agents on different hosts can talk. agent-send.sh stays local-first
// (send-keys); only a non-local target (or --via-slack) hits POST /send, which
// posts an addressed, sender-tagged line to SLACK_AGENTS_CHANNEL. Every host's
// bridge sees it over Socket Mode and the one whose registry owns the target
// delivers it (handleBusMessage). Inert unless both vars are set.
const BUS_ENABLED = process.env.SLACK_BUS_ENABLED === '1';
const AGENTS_CHANNEL = process.env.SLACK_AGENTS_CHANNEL || '';

// --- Presence registry (Phase 2; opt-in on top of the bus; default OFF) ---
// Each bridge announces its live local agent set on the bus channel and consumes
// peers into presenceMap (host -> { agents:Set, ts, seen }). From it we derive a
// single deterministic owner per name (lexically-smallest claiming host) so
// exactly one host delivers a remote-addressed message, detect name collisions,
// and answer reachability (GET /agents). Reuses the Socket Mode fan-out — no
// shared store. Inert unless SLACK_PRESENCE_ENABLED=1 (keeps Phase 1 unchanged).
const SELF_HOST = (process.env.SLACK_PRESENCE_HOST || hostname() || 'unknown').trim();
const PRESENCE_ENABLED = BUS_ENABLED && !!AGENTS_CHANNEL && process.env.SLACK_PRESENCE_ENABLED === '1';
const PRESENCE_HEARTBEAT_MS = parseInt(process.env.SLACK_PRESENCE_HEARTBEAT_MS || '300000', 10); // 5 min
const PRESENCE_TTL_MS = parseInt(process.env.SLACK_PRESENCE_TTL_MS || String(16 * 60 * 1000), 10); // ~3 missed beats
const presenceMap = new Map(); // host -> { agents:Set<string>, ts, seen }

// --- Idle-gated bus delivery (default ON when the bus is on) ---
// A bus message is a real keystroke injection (send-keys). Injected into an agent
// that is mid-task it gets lost or interrupts the run. So delivery is gated on the
// recipient's `@waiting` window-option (the hook-maintained state the arbiter +
// reaper read): deliver only when it is idle at the prompt (`@waiting=2`); when it
// is working (`0`/unset) or at a permission prompt (`1`), hold the message in a
// per-pane queue and flush it when the agent next goes idle. The channel is the
// durable record (replay/audit); the queue makes delivery non-lossy + non-interrupting.
const BUS_DEFER = BUS_ENABLED && process.env.SLACK_BUS_DEFER !== '0';
const BUS_FLUSH_MS = parseInt(process.env.SLACK_BUS_FLUSH_MS || '4000', 10);
const BUS_QUEUE_MAX = parseInt(process.env.SLACK_BUS_QUEUE_MAX || '50', 10); // per pane; oldest dropped beyond (still in channel)
const busQueue = new Map(); // pane -> [{ target, body, at }]

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
// The bridge's delivery is always the FINAL hop (channel -> pane), so it must
// stay local send-keys. Force SLACK_A2A_SAMEHOST=local in the child env so a
// delivery can never re-route a pane back through the bus and loop, regardless of
// how the bridge's ambient env happens to be configured.
const DELIVER_ENV = { ...process.env, SLACK_A2A_SAMEHOST: 'local', SLACK_A2A_NUDGE: '0' };

function deliverToPane(pane, text) {
  try {
    execFileSync(AGENT_SEND, [pane, text], { encoding: 'utf8', timeout: 5000, env: DELIVER_ENV });
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
    execFileSync(AGENT_SEND, [String(slot), text], { encoding: 'utf8', timeout: 5000, env: DELIVER_ENV });
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

// Presence: this host's live local agent names. Reads the registry, then keeps
// only names whose pane is actually alive (registry files persist briefly after
// a window closes) so we never announce a dead agent. If tmux is unreachable we
// fall back to announcing all registry names rather than going dark.
function localLiveAgents() {
  const reg = loadRegistry();
  if (!reg.length) return [];
  let live = null;
  try {
    const out = execFileSync('tmux', ['list-panes', '-a', '-F', '#{pane_id}'], { encoding: 'utf8', timeout: 3000 });
    live = new Set(out.split('\n').map((s) => s.trim()).filter(Boolean));
  } catch { live = null; }
  const names = new Set();
  for (const a of reg) {
    if (!a.name) continue;
    if (live && a.pane && !live.has(a.pane)) continue; // stale registry file — pane gone
    names.add(a.name);
  }
  return Array.from(names);
}

// Throttle the re-announce-on-new-host nudge so a fleet booting together can't
// trigger a publish storm.
let lastPresencePublish = 0;

// Announce this host's live agent set on the bus channel (full-state snapshot)
// and update our own presence entry synchronously so deliveries don't wait for
// the round-trip. Best-effort; never throws.
async function publishPresence() {
  if (!PRESENCE_ENABLED) return;
  const agents = localLiveAgents();
  const ts = Math.floor(Date.now() / 1000);
  orch.applyPresence(presenceMap, { host: SELF_HOST, agents, ts }, { now: Date.now() });
  try {
    await web.chat.postMessage({ channel: AGENTS_CHANNEL, text: orch.formatPresence({ host: SELF_HOST, agents, ts }) });
  } catch (e) {
    console.error(`[presence] publish failed: ${e.message}`);
  }
}

// Consume a peer's presence snapshot into the map; warn on any name collision.
// Re-announce ourselves when a host we hadn't seen appears, so a freshly-booted
// bridge learns the fleet (and is learned) without waiting a full heartbeat.
function consumePresence(snap) {
  if (!PRESENCE_ENABLED || !snap) return;
  const isNewHost = snap.host !== SELF_HOST && !presenceMap.has(snap.host);
  orch.applyPresence(presenceMap, snap, { now: Date.now() });
  const cols = orch.presenceCollisions(presenceMap);
  if (cols.length) {
    console.warn(`[presence] name collision: ${cols.map((c) => `${c.name}@{${c.hosts.join(',')}}`).join(' ')}`);
  }
  if (isNewHost) {
    const since = Date.now() - lastPresencePublish;
    if (since > 8000) { lastPresencePublish = Date.now(); publishPresence().catch(() => {}); }
  }
}

// Read a hook-maintained window-option for a pane (@waiting / @wait_since /
// @last_tool). Empty string on any error / non-agent window.
function paneOpt(pane, name) {
  if (!pane) return '';
  try {
    return execFileSync('tmux', ['show-options', '-wqv', '-t', pane, name], { encoding: 'utf8', timeout: 3000 }).trim();
  } catch { return ''; }
}

// `@waiting` for a pane's window (hook-maintained; same option the arbiter +
// reaper read): '2' = idle/done at the prompt, '1' = at a permission prompt,
// '0'/'' = actively working. Empty on any error / non-agent window.
function paneWaiting(pane) {
  return paneOpt(pane, '@waiting');
}

// Live slot (window index) for a pane. The registry SLOT= field is stale (it's the
// index at registration; windows get renumbered), so resolve it live like
// agent-registry.sh's `peers` does. '' on error.
function paneSlot(pane) {
  if (!pane) return '';
  try {
    return execFileSync('tmux', ['display-message', '-t', pane, '-p', '#{window_index}'], { encoding: 'utf8', timeout: 3000 }).trim();
  } catch { return ''; }
}

// Git branch of a checkout, best-effort (single-agent status detail only).
function gitBranch(cwd) {
  if (!cwd) return '';
  try {
    return execFileSync('git', ['-C', cwd, 'branch', '--show-current'], { encoding: 'utf8', timeout: 3000 }).trim();
  } catch { return ''; }
}

// Live-agent status rows for the `status` command: each registry entry whose pane
// is still alive, joined with its live slot + hook-maintained window state. Local
// fleet only (presence carries no per-agent state). index.js reads tmux;
// orchestrator.js formats. Shape: [{name, slot, pane, cwd, waiting, waitSince, lastTool}].
function gatherFleetStatus() {
  const liveNames = new Set(localLiveAgents().map((n) => n.toLowerCase()));
  return loadRegistry()
    .filter((a) => a.name && liveNames.has(a.name.toLowerCase()))
    .map((a) => ({
      name: a.name,
      slot: paneSlot(a.pane) || a.slot,   // registry SLOT is stale — resolve live
      pane: a.pane, cwd: a.cwd,
      waiting: paneOpt(a.pane, '@waiting'),
      waitSince: paneOpt(a.pane, '@wait_since'),
      lastTool: paneOpt(a.pane, '@last_tool'),
    }));
}

// Compute fleet (or single-agent) status and post it back to the channel/DM the
// request came from. Bridge-computed from the registry + @waiting, so it always
// lands in Slack and works even when an agent is busy/wedged. Not gated on SPAWN.
async function doStatus(channel, threadTs, target) {
  const opts = { now: Date.now(), stuckMin: STATUS_STUCK_MIN };
  const t = String(target || '').trim();
  const fleet = gatherFleetStatus();          // single source: live slot + state
  let md;
  if (t && t.toLowerCase() !== 'all') {
    const low = t.toLowerCase();
    const agent = fleet.find((a) => a.name.toLowerCase() === low || a.slot === t);
    if (!agent) {
      await replyInThread(channel, threadTs, `:warning: no active agent \`${t}\`. Active: ${liveAgentList()}`);
      return;
    }
    md = orch.formatAgentStatus({ ...agent, branch: gitBranch(agent.cwd) }, opts);
  } else {
    md = orch.formatFleetStatus(fleet, opts);
  }
  try {
    await web.chat.postMessage({
      channel, thread_ts: threadTs,
      text: md.replace(/[*_`]/g, ''),                       // plain fallback for notifications
      blocks: [{ type: 'section', text: { type: 'mrkdwn', text: md } }],
    });
  } catch (e) {
    console.error(`[slack-bridge] status post failed: ${e.message}`);
  }
}

// Hold a bus message for a busy recipient. Bounded per pane so a perpetually-busy
// agent can't grow it without limit (oldest dropped — still in #nexus-agents).
function enqueueBus(pane, target, body) {
  const q = busQueue.get(pane) || [];
  q.push({ target, body, at: Date.now() });
  while (q.length > BUS_QUEUE_MAX) {
    q.shift();
    console.warn(`[bus] queue for ${target} (${pane}) over ${BUS_QUEUE_MAX} — dropped oldest (still in channel)`);
  }
  busQueue.set(pane, q);
}

// Flush poll: deliver ONE queued message to each now-idle recipient; drop queues
// for dead panes. One per tick — after a send-keys the agent goes busy, so the
// next message waits for its next idle window (each gets its own turn rather than
// being concatenated into one input).
function flushBusQueue() {
  if (!busQueue.size) return;
  let live = null;
  try {
    const out = execFileSync('tmux', ['list-panes', '-a', '-F', '#{pane_id}'], { encoding: 'utf8', timeout: 3000 });
    live = new Set(out.split('\n').map((s) => s.trim()).filter(Boolean));
  } catch { live = null; }
  for (const [pane, q] of busQueue) {
    if (live && !live.has(pane)) {
      busQueue.delete(pane);
      console.warn(`[bus] dropped ${q.length} queued msg(s) for dead pane ${pane} (still in channel)`);
      continue;
    }
    if (!q.length) { busQueue.delete(pane); continue; }
    if (paneWaiting(pane) !== '2') continue;     // still busy / at a prompt — keep holding
    const msg = q[0];
    const res = deliverToPane(pane, msg.body);
    if (res.ok) {
      q.shift();
      flashPane(pane, 'bus msg');
      console.log(`[bus] flushed to ${msg.target} (${pane}) after idle: ${msg.body.slice(0, 60)}`);
    } else {
      console.error(`[bus] flush to ${msg.target} (${pane}) failed: ${res.error}`);  // keep queued, retry next tick
    }
    if (!q.length) busQueue.delete(pane);
  }
}

// Inter-agent bus delivery: a message on the dedicated agents channel, of the
// form `to: text` (posted by some host's /send). Deliver to `to` ONLY if it is a
// live agent in THIS host's registry; otherwise ignore (another host owns it).
// This never posts anything back, so there is no feedback loop. The delivered
// text already carries the `↩ from <sender>:` prefix, baked in at post time.
async function handleBusMessage(event) {
  if (!event.text) return;
  const m = cleanSlackText(event.text).match(/^([A-Za-z0-9][\w.-]*)\s*:\s*([\s\S]+)$/);
  if (!m) return;                          // not an addressed bus message — ignore
  const target = m[1];
  const body = m[2].trim();
  const agent = resolveByName(target);     // local registry only
  if (!agent) return;                      // not ours — the owning host delivers
  // Single-owner rule (Phase 2): if presence positively designates another host
  // as the owner of this name, defer to it even though our local registry also
  // matches (our entry may be stale, or it's a genuine collision). Presence
  // unavailable / no positive owner → fall back to Phase 1 (local match wins).
  if (PRESENCE_ENABLED) {
    const owner = orch.ownerOf(presenceMap, target);
    if (owner && owner !== SELF_HOST) {
      console.warn(`[bus] ${target} owned by ${owner}, not ${SELF_HOST} — deferring delivery`);
      return;
    }
  }
  // Idle-gate: inject only when the recipient is idle at the prompt (@waiting=2);
  // if it is mid-task or at a permission prompt, hold the message and let
  // flushBusQueue deliver it when the agent next goes idle — so a running task is
  // never interrupted and the message is never lost into a busy pane.
  if (BUS_DEFER && agent.pane) {
    const w = paneWaiting(agent.pane);
    if (w !== '2') {
      enqueueBus(agent.pane, target, body);
      console.log(`[bus] ${target} busy (@waiting=${w || 'unset'}) — queued for idle delivery`);
      return;
    }
  }
  const res = agent.pane ? deliverToPane(agent.pane, body) : deliverToSlot(agent.slot, body);
  if (res.ok) {
    flashPane(agent.pane, 'bus msg');
    console.log(`[bus] delivered to ${target} (${agent.pane || agent.slot}): ${body.slice(0, 60)}`);
  } else {
    console.error(`[bus] delivery to ${target} failed: ${res.error}`);
  }
}

async function handleMessage(event) {
  if (!event) return;

  // --- Inter-agent bus: traffic on the dedicated agents channel is bot-posted
  // by some host's /send, so it bypasses the human-message bot/self filters
  // below. It only ever delivers to a LOCAL agent and never re-posts — no loop. ---
  if (BUS_ENABLED && AGENTS_CHANNEL && event.channel === AGENTS_CHANNEL) {
    // Presence announcements share this channel — route them out of the
    // addressed-delivery path. They only update the in-memory map (consumePresence
    // never delivers and never re-posts), so there is still no loop.
    const snap = orch.parsePresence(event.text || '');
    if (snap) { consumePresence(snap); return; }
    await handleBusMessage(event);
    return;
  }

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

  const cleaned = cleanSlackText(event.text);

  // 1b. Status command (read-only; independent of SPAWN_ENABLED). Bridge-computed
  //     from the registry + @waiting and posted back here (channel or DM), so it
  //     works even when an agent is busy/wedged. Checked before addressing/routing
  //     so "status …" / "who" is never delivered to an agent or LLM-routed.
  const mStatus = cleaned.match(/^(?:status|who)\b\s*(.*)$/i);
  if (mStatus) { await doStatus(channel, event.ts, mStatus[1]); return; }

  // 2. Addressed top-level: "name: text" or "slot: text" (after any @bot mention)
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
//   GET  /health | /agents | /status
// ---------------------------------------------------------------------------
const httpServer = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');

  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, connected: socketConnected, threads: threadMap.size, bus: BUS_ENABLED && !!AGENTS_CHANNEL, presence: PRESENCE_ENABLED, host: SELF_HOST }));
    return;
  }

  // Reachability (Phase 2): the fleet-wide live agent set derived from the
  // presence map — each agent, its owning host, the resolved single owner, and a
  // collision flag. Expire dead hosts first so a crashed bridge ages out.
  if (req.method === 'GET' && url.pathname === '/agents') {
    orch.expirePresence(presenceMap, { now: Date.now(), ttlMs: PRESENCE_TTL_MS });
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ok: true,
      presence: PRESENCE_ENABLED,
      self: SELF_HOST,
      hosts: presenceMap.size,
      agents: orch.reachability(presenceMap),
      collisions: orch.presenceCollisions(presenceMap),
    }));
    return;
  }

  // Local fleet status — the data behind the `status` Slack command, exposed for
  // curl/CLI checks without Slack. Local agents only (presence carries no state).
  if (req.method === 'GET' && url.pathname === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, host: SELF_HOST, agents: gatherFleetStatus() }));
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

  // Inter-agent bus: agent-send.sh POSTs here when a target isn't local (or
  // --via-slack). We only PUBLISH to the agents channel — delivery happens via
  // the channel round-trip (handleBusMessage on whichever host owns the target),
  // so cross-host works and we never deliver twice.
  if (req.method === 'POST' && url.pathname === '/send') {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', async () => {
      res.setHeader('Content-Type', 'application/json');
      try {
        if (!BUS_ENABLED || !AGENTS_CHANNEL) {
          res.writeHead(409);
          res.end(JSON.stringify({ ok: false, error: 'bus disabled (set SLACK_BUS_ENABLED=1 + SLACK_AGENTS_CHANNEL)' }));
          return;
        }
        const { to, from, msg } = JSON.parse(body || '{}');
        if (!to || !msg) { res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'to and msg required' })); return; }
        const sender = String(from || 'unknown').slice(0, 80);
        // `to: ↩ from <sender>: <msg>` — the leading `to:` is what the receiving
        // bridge's addressed-message parser keys on; the rest is delivered verbatim.
        const text = `${to}: ↩ from ${sender}: ${String(msg).slice(0, 1500)}`;
        const posted = await web.chat.postMessage({ channel: AGENTS_CHANNEL, text });
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

  if (BUS_ENABLED && AGENTS_CHANNEL) {
    console.log(`[slack-bridge] inter-agent bus ENABLED (channel=${AGENTS_CHANNEL})`);
    if (BUS_DEFER) {
      setInterval(flushBusQueue, BUS_FLUSH_MS);
      console.log(`[slack-bridge] bus idle-gated delivery ON (deliver on @waiting=2, flush every ${BUS_FLUSH_MS}ms, queue cap ${BUS_QUEUE_MAX}/pane)`);
    } else {
      console.log('[slack-bridge] bus idle-gated delivery OFF (SLACK_BUS_DEFER=0) — immediate send-keys');
    }
  } else if (BUS_ENABLED) {
    console.warn('[slack-bridge] SLACK_BUS_ENABLED=1 but SLACK_AGENTS_CHANNEL not set — bus inert');
  }

  // Presence registry: announce our live agents now, then on a heartbeat, expire
  // dead peers, and re-announce on any registry change (spawn/reap) so the fleet
  // map tracks reality within seconds rather than a full heartbeat.
  if (PRESENCE_ENABLED) {
    await publishPresence();
    setInterval(() => { publishPresence().catch(() => {}); }, PRESENCE_HEARTBEAT_MS);
    setInterval(() => { orch.expirePresence(presenceMap, { now: Date.now(), ttlMs: PRESENCE_TTL_MS }); }, PRESENCE_HEARTBEAT_MS);
    try {
      let debounce = null;
      watch(REGISTRY_DIR, () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => { lastPresencePublish = Date.now(); publishPresence().catch(() => {}); }, 1500);
      });
    } catch (e) {
      console.warn(`[presence] registry watch unavailable (${e.message}) — relying on the ${PRESENCE_HEARTBEAT_MS}ms heartbeat`);
    }
    console.log(`[slack-bridge] presence registry ENABLED (host=${SELF_HOST}, heartbeat=${PRESENCE_HEARTBEAT_MS}ms, ttl=${PRESENCE_TTL_MS}ms)`);
  } else if (BUS_ENABLED && AGENTS_CHANNEL) {
    console.log('[slack-bridge] presence registry disabled (set SLACK_PRESENCE_ENABLED=1 to enable)');
  }

  await socket.start();
})();
