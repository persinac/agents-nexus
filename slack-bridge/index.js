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
import { randomUUID } from 'crypto';
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

// Completion ping: when an agent you messaged from Slack finishes its turn and
// settles back at the prompt, post "finished — idle" to that channel so you know
// it acted on your request — handy in auto-mode, from mobile. Gated to agents you
// actually messaged (pinging every idle transition would be noise).
const DONE_PING = process.env.SLACK_DONE_PING !== '0';                              // default ON
const DONE_STABLE_MS = parseInt(process.env.SLACK_DONE_STABLE_MS || '20000', 10);   // idle must hold this long (debounces auto-mode flicker)
const DONE_POLL_MS = parseInt(process.env.SLACK_DONE_POLL_MS || '5000', 10);        // how often messaged agents are checked
const DONE_TTL_MS = parseInt(process.env.SLACK_DONE_TTL_MS || '1800000', 10);       // stop tracking after 30 min
const messagedPanes = new Map(); // pane -> { name, channel, at, sawWorking, idleSince, lastTool0 }

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
// Max chars of an A2A message body the bus forwards. Slack's text field allows ~40k,
// so this is a sanity bound, not a hard ceiling. Over it, capWithMarker appends a
// visible truncation marker (never silent). Raising it is safe; chunking (IDEAS #30h)
// removes the limit entirely. Was a silent `.slice(0, 1500)` — the bug integration-tests caught.
const BUS_MAX_CHARS = parseInt(process.env.SLACK_BUS_MAX_CHARS || '8000', 10);

// --- A2A bus transport selection (default 'slack'; 'nats' = NATS + JetStream) ---
// The bus medium is pluggable: `slack` (default) keeps the #nexus-agents channel path
// byte-for-byte; `nats` routes A2A publish/subscribe over a NATS JetStream broker so the
// fleet scales past Slack's Socket-Mode / per-app-bot ceiling. Routing, the delivery layer
// (send-keys / SDK inbox), the idle-gate, and the HUMAN notify/reply leg are UNCHANGED —
// only the A2A publish (`/send`) and inbound A2A delivery move. The `nats` client is imported
// dynamically at startup ONLY in nats mode, so a slack-only bridge needs no NATS dependency.
// The bus is still gated by SLACK_BUS_ENABLED=1 regardless of transport (the master switch).
const BUS_TRANSPORT = (process.env.NEXUS_BUS_TRANSPORT || 'slack').toLowerCase();
const NATS_URL = process.env.NATS_URL || 'nats://127.0.0.1:4222';
const NATS_CREDS = process.env.NATS_CREDS || '';                 // creds file (NKEY/JWT) — the scale path
const NATS_TOKEN = process.env.NATS_TOKEN || '';
const NATS_USER = process.env.NATS_USER || '';
const NATS_PASS = process.env.NATS_PASS || '';
const NATS_A2A_STREAM = process.env.NATS_A2A_STREAM || 'NEXUS_A2A';
const NATS_A2A_SUBJECT_PREFIX = process.env.NATS_A2A_SUBJECT_PREFIX || 'nexus.a2a';
const NATS_PRESENCE_KV = process.env.NATS_PRESENCE_KV || 'nexus_presence';
let natsTransport = null;   // the NatsTransport instance (set at startup in nats mode)
let natsReady = false;      // true once connected + subscribed

// --- Presence registry (Phase 2; opt-in on top of the bus; default OFF) ---
// Each bridge announces its live local agent set on the bus channel and consumes
// peers into presenceMap (host -> { agents:Set, ts, seen }). From it we derive a
// single deterministic owner per name (lexically-smallest claiming host) so
// exactly one host delivers a remote-addressed message, detect name collisions,
// and answer reachability (GET /agents). Reuses the Socket Mode fan-out — no
// shared store. Opt-in (SLACK_PRESENCE_ENABLED=1) — only useful with 2+ live bridges, so a
// solo-host install leaves it off to avoid a pointless heartbeat. Enable it per-machine (the
// repo `.env`) when a second host joins, for cross-host FQDN election + reachability.
const SELF_HOST = (process.env.SLACK_PRESENCE_HOST || hostname() || 'unknown').trim();
const PRESENCE_ENABLED = BUS_ENABLED && !!AGENTS_CHANNEL && process.env.SLACK_PRESENCE_ENABLED === '1';
// FQDN presence (v2): publish per-instance { name, workspace, pane } records instead of a
// bare-name Set, so two same-named agents on one host are distinct and cross-host addressable
// by host/workspace/name or host/pane. Consume-side is ALWAYS v1+v2 tolerant; this flag only
// controls what WE PUBLISH, so a v2 bridge degrades to v1 among pre-FQDN peers. Opt-in, default off.
const PRESENCE_FQDN = PRESENCE_ENABLED && process.env.SLACK_PRESENCE_FQDN === '1';
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

// --- Typed envelope + request/reply (Phase B) ---
// A2A messages carry a typed envelope (kind msg/request/reply/event; see orchestrator.js).
// `pendingRequests` tracks outstanding POST /request awaits (id -> { resolve, timer }); a
// `reply` whose `corr` matches is intercepted to resolve the waiter (interceptReply) instead
// of being delivered to an agent. A deadline resolves every waiter so none hangs.
const REQUEST_TTL_MS = parseInt(process.env.SLACK_BUS_REQUEST_TTL_MS || '120000', 10); // 2 min default
const pendingRequests = new Map(); // request id -> { resolve, timer, at }
const shortId = () => randomUUID().replace(/-/g, '').slice(0, 12);

// --- Human-typing guard (opt-in; default OFF) ---
// `@waiting=2` means the Claude *process* is idle at the prompt — but that is
// also exactly when a HUMAN may be composing a draft in that pane. A send-keys
// inject then interleaves with their keystrokes. So on top of the @waiting gate,
// optionally defer while a human is actively typing INTO the recipient pane:
// detected via tmux `client_activity` (last keystroke time of an attached client
// whose focused pane is this one). Held in the same busQueue and flushed on the
// next tick once typing goes quiet (or they submit, which flips @waiting busy).
// Grace = how recent a keystroke counts as "still typing". 0 disables the guard.
const HUMAN_TYPING_GRACE_MS = parseInt(process.env.SLACK_BUS_HUMAN_GRACE_MS || '2000', 10);  // 0 disables the guard
const HUMAN_GUARD = BUS_DEFER && HUMAN_TYPING_GRACE_MS > 0;

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
      // SLOT is a numeric window index in tmux mode but the pane HANDLE (wN:pN) in herdr
      // mode — capture it as-is. A prior `SLOT=(\d+)` here silently dropped EVERY herdr
      // agent (non-numeric slot → the `name && slot` gate failed), so the bridge saw zero
      // agents and every bus delivery / smart route / status came up empty in herdr mode.
      const slot = (c.match(/^SLOT=(.*)$/m) || [])[1];
      const pane = (c.match(/^PANE_ID=(.*)$/m) || [])[1];
      const cwd = (c.match(/^CWD=(.*)$/m) || [])[1];
      const workspace = (c.match(/^WORKSPACE=(.*)$/m) || [])[1];
      // Keep any entry with a name and at least one address (slot or pane). resolveBySlot
      // still matches numeric slots (tmux); resolveByName/resolveByPane use name/pane.
      if (name && (slot || pane)) out.push({ name: name.trim(), slot: (slot || '').trim(), pane: (pane || '').trim(), cwd: (cwd || '').trim(), workspace: (workspace || '').trim() });
    } catch { /* ignore unreadable registry file */ }
  }
  return out;
}

function resolveByName(name, workspace) {
  const lower = name.toLowerCase();
  const want = workspace || '';
  const matches = loadRegistry().filter((a) =>
    a.name.toLowerCase() === lower && orch.workspaceMatches(a.workspace, want));
  if (matches.length <= 1) return matches[0] || null;
  // >1 match. Preserve legacy behavior for a pure flat fleet (no buckets → first wins, as
  // before); once workspaces are in play a bare name that spans buckets is genuinely
  // ambiguous → null, and the caller must qualify it as workspace/name.
  if (!want && matches.every((a) => !a.workspace)) return matches[0];
  return null;
}

// Match by LIVE window index, not the registry SLOT= field — that field is the slot
// at registration and goes stale when windows are renumbered (it reads 1 for agents
// actually at 2/3/4/…). Resolve each entry's current slot from its pane (like
// agent-registry.sh `peers`), falling back to the stored value if tmux is unreachable.
function resolveBySlot(slot) {
  const s = String(slot);
  return loadRegistry().find((a) => (paneSlot(a.pane) || a.slot) === s) || null;
}

// Resolve a herdr pane handle (wN:pN) to its local registry entry. A handle addresses
// exactly ONE local instance — it's the only bus address that disambiguates two agents
// sharing a name (e.g. two `general`s in the `interactive` bucket). Host-local by
// construction: a handle not in THIS host's registry yields null (logged, never
// mis-delivered). Mirrors resolveBySlot; PANE_ID is parsed by loadRegistry().
function resolveByPane(handle) {
  const h = String(handle);
  return loadRegistry().find((a) => a.pane === h) || null;
}

// Fully-qualify a sender `from` tag to this host's FQDN — SELF_HOST/[workspace/]name — so
// A2A replies route deterministically cross-host (the address grammar's default form; there's
// no cost to the longer address). A `from` that's already host-qualified (a known/self host
// prefix) is left as-is; a bare name gets its registry workspace when resolvable.
function qualifyFrom(from) {
  const raw = String(from || 'unknown').trim();
  const parsed = orch.parseAddress(raw, { knownHosts: new Set(presenceMap.keys()), selfHost: SELF_HOST });
  if (parsed.host) return raw;                     // already host-qualified → leave it
  let wsName = raw;
  if (!parsed.workspace) {                         // no workspace segment → add the sender's bucket if known
    const a = resolveByName(parsed.name);
    if (a && a.workspace) wsName = `${a.workspace}/${parsed.name}`;
  }
  return `${SELF_HOST}/${wsName}`;
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
  if (NEXUS_SUBSTRATE === 'herdr') return;   // tmux status-line flash has no herdr analog
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
  if (NEXUS_SUBSTRATE === 'herdr') return subRead(['pane-opt', pane, '@wait_since']);
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
// ── Substrate read seam ────────────────────────────────────────────────────
// In herdr mode these route through substrate.sh (→ substrated daemon cache); in
// tmux mode the readers keep their exact tmux calls so the live tmux bus is
// byte-identical. Enabled by NEXUS_SUBSTRATE=herdr.
const NEXUS_SUBSTRATE = process.env.NEXUS_SUBSTRATE || 'herdr';  // default herdr; set NEXUS_SUBSTRATE=tmux for the legacy fallback
const SUBSTRATE_BIN = `${process.env.HOME}/.tmux/substrate.sh`;
function subRead(args) {
  try { return execFileSync(SUBSTRATE_BIN, args, { encoding: 'utf8', timeout: 3000 }).trim(); }
  catch { return ''; }
}
// Live pane/agent handles for liveness filtering (herdr: daemon pane list; tmux: list-panes).
function liveHandles() {
  if (NEXUS_SUBSTRATE === 'herdr') return subRead(['list-panes']).split('\n').map((s) => s.trim()).filter(Boolean);
  const out = execFileSync('tmux', ['list-panes', '-a', '-F', '#{pane_id}'], { encoding: 'utf8', timeout: 3000 });
  return out.split('\n').map((s) => s.trim()).filter(Boolean);
}

// This host's live agents as instance RECORDS [{name, workspace, pane}] — one per live
// registry entry (deduped by pane), so two same-named agents on this host are two records,
// not one collapsed name. Presence (v2) publishes these; callers that only want names map
// `.name`. A stale registry file whose pane is gone is dropped.
function localLiveAgents() {
  const reg = loadRegistry();
  if (!reg.length) return [];
  let live = null;
  try { live = new Set(liveHandles()); } catch { live = null; }
  const out = [];
  const seen = new Set();
  for (const a of reg) {
    if (!a.name) continue;
    if (live && a.pane && !live.has(a.pane)) continue; // stale registry file — pane gone
    const key = a.pane || `${a.workspace || ''}\u0000${a.name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ name: a.name, workspace: a.workspace || '', pane: a.pane || '' });
  }
  return out;
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
    await web.chat.postMessage({ channel: AGENTS_CHANNEL, text: orch.formatPresence({ host: SELF_HOST, agents, ts }, { fqdn: PRESENCE_FQDN }) });
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
    console.warn(`[presence] identity collision: ${cols.map((c) => `${c.workspace ? c.workspace + '/' : ''}${c.name} (${c.count}× @ ${c.hosts.join(',')})`).join(' ')}`);
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
  if (NEXUS_SUBSTRATE === 'herdr') return subRead(['pane-opt', pane, name]);
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

// True if a HUMAN is actively typing into `pane` right now — the case @waiting=2
// can't distinguish (process idle vs. human mid-draft). Two conditions:
//   1. `pane` is the focused pane of its session (window_active=1 AND pane_active=1).
//      Both are required: pane_active=1 alone just means "active within its own
//      window" — many background windows report it — so keystrokes only land here
//      when the window is active too.
//   2. Some client attached to that session had a keystroke (`client_activity`,
//      epoch seconds) within HUMAN_TYPING_GRACE_MS. Keys go to the focused pane,
//      so a recent client keystroke + (1) means they're typing HERE.
// Fail-open (false) on any tmux error or no attached client: the @waiting gate is
// still the primary guard, so a transient error just reverts to today's behavior
// rather than deferring a message forever.
//
// herdr has no client_activity timestamp, so its branch approximates "typing here" as: the pane is
// FOCUSED (keystrokes land there) AND its on-screen content is still changing between checks. That
// needs a tiny bit of state — the last capture + when it last changed, per pane:
const _typingSnap = new Map();   // pane -> { text, since }
function humanTyping(pane, graceMs = HUMAN_TYPING_GRACE_MS) {
  if (!pane || !(graceMs > 0)) return false;
  if (NEXUS_SUBSTRATE === 'herdr') {
    // Focused + content still changing ⇒ they're typing ⇒ defer. Content stable for graceMs ⇒
    // they've stopped ⇒ deliver. Fail-open (deliver) on any read miss so nothing holds forever.
    if (subRead(['pane-focused', pane]) !== '1') { _typingSnap.delete(pane); return false; }
    const vis = subRead(['pane-visible', pane, '12']);
    if (!vis) return false;
    const now = Date.now();
    const prev = _typingSnap.get(pane);
    if (!prev || prev.text !== vis) { _typingSnap.set(pane, { text: vis, since: now }); return true; }
    return (now - prev.since) < graceMs;   // within grace of the last change → still defer; past it → deliver
  }
  try {
    const [sess, winActive, paneActive] = execFileSync(
      'tmux', ['display-message', '-t', pane, '-p', '#{session_name} #{window_active} #{pane_active}'],
      { encoding: 'utf8', timeout: 3000 },
    ).trim().split(' ');
    if (winActive !== '1' || paneActive !== '1') return false;   // not the focused pane — keys aren't landing here
    const now = Date.now();
    const clients = execFileSync(
      'tmux', ['list-clients', '-F', '#{client_session} #{client_activity}'],
      { encoding: 'utf8', timeout: 3000 },
    ).trim().split('\n');
    return clients.some((line) => {
      const [cs, act] = line.split(' ');
      return cs === sess && Number(act) > 0 && (now - Number(act) * 1000) < graceMs;
    });
  } catch { return false; }
}

// Live slot (window index) for a pane. The registry SLOT= field is stale (it's the
// index at registration; windows get renumbered), so resolve it live like
// agent-registry.sh's `peers` does. '' on error.
function paneSlot(pane) {
  if (!pane) return '';
  if (NEXUS_SUBSTRATE === 'herdr') return subRead(['pane-field', pane, '#{window_index}']);
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

// --- Completion ping: track Slack-messaged agents, announce when they finish ---

// Record that we just delivered a Slack message to an agent, so doneSweep can tell
// you when it settles. Newest delivery wins (resets the timer + work baseline).
function markMessaged(pane, name, channel) {
  if (!DONE_PING || !pane) return;
  messagedPanes.set(pane, {
    name, channel, at: Date.now(), sawWorking: false, idleSince: null,
    lastTool0: paneOpt(pane, '@last_tool'),   // baseline: any work bumps @last_tool past this
  });
}

// Poll each messaged agent, advance its done-state machine (orch.advanceDone), and
// post the "finished — idle" ping once it settles. Drops dead/expired entries.
// Reentrancy-guarded so a slow post can't overlap the next tick.
let doneSweeping = false;
async function doneSweep() {
  if (doneSweeping || !messagedPanes.size) return;
  doneSweeping = true;
  try {
    let live = null;
    try {
      live = new Set(liveHandles());
    } catch { live = null; }
    const now = Date.now();
    for (const [pane, entry] of messagedPanes) {
      if (live && !live.has(pane)) { messagedPanes.delete(pane); continue; }   // window gone
      const waiting = paneWaiting(pane);
      const worked = waiting === '0' || Number(paneOpt(pane, '@last_tool')) > Number(entry.lastTool0 || 0);
      const { action, entry: next } = orch.advanceDone(entry, { waiting, worked }, now, { stableMs: DONE_STABLE_MS, ttlMs: DONE_TTL_MS });
      if (action === 'fire') {
        messagedPanes.delete(pane);
        try {
          await web.chat.postMessage({ channel: entry.channel, text: `:white_check_mark: *${entry.name}* finished — now idle at the prompt.` });
        } catch (e) { console.error(`[slack-bridge] done ping failed: ${e.message}`); }
      } else if (action === 'drop') {
        messagedPanes.delete(pane);
      } else {
        messagedPanes.set(pane, next);
      }
    }
  } finally {
    doneSweeping = false;
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
    live = new Set(liveHandles());
  } catch { live = null; }
  for (const [pane, q] of busQueue) {
    if (live && !live.has(pane)) {
      busQueue.delete(pane);
      console.warn(`[bus] dropped ${q.length} queued msg(s) for dead pane ${pane} (still in channel)`);
      continue;
    }
    if (!q.length) { busQueue.delete(pane); continue; }
    if (paneWaiting(pane) !== '2') continue;     // still busy / at a prompt — keep holding
    if (HUMAN_GUARD && humanTyping(pane)) continue;  // human mid-draft in this pane — hold, don't clobber keystrokes
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
// Resolve a bus address token to ONE local agent, or null (logging why). A herdr pane handle
// (wN:pN) or a bare slot number is instance-exact and host-local → resolve directly, bypassing
// presence owner-election (the colon-SPACE delimiter lets `wQ:pF` survive the parser). Anything
// else is a [host/][workspace/]name resolved right-to-left with owner-election. Extracted from
// the old handleBusMessage so the Slack legacy line, the Slack typed envelope, and the NATS
// consumer all resolve identically.
function resolveBusTarget(token) {
  if (/^w[A-Za-z0-9]+:p[A-Za-z0-9]+$/.test(token)) {
    const a = resolveByPane(token);
    if (!a) console.warn(`[bus] no local agent for pane ${token} — dropped (from bus)`);
    return a;
  }
  if (/^\d+$/.test(token)) {
    const a = resolveBySlot(token);
    if (!a) console.warn(`[bus] no local agent at slot ${token} — dropped (from bus)`);
    return a;
  }
  const { host: qualHost, workspace: qualWs, name: nm } =
    orch.parseAddress(token, { knownHosts: new Set(presenceMap.keys()), selfHost: SELF_HOST });
  if (qualHost && qualHost.toLowerCase() !== SELF_HOST.toLowerCase()) return null; // not our host
  if (/^w[A-Za-z0-9]+:p[A-Za-z0-9]+$/.test(nm)) {
    const a = resolveByPane(nm);
    if (!a) console.warn(`[bus] no local agent for pane ${nm} (from '${token}') — dropped (from bus)`);
    return a;
  }
  const agent = resolveByName(nm, qualWs);
  if (!agent) {
    // resolveByName returns null on a cross-instance name collision BY DESIGN — surface the
    // qualified addresses (was a silent, expensive-to-diagnose drop); else it's simply not ours.
    const dups = loadRegistry().filter((a) => a.name.toLowerCase() === nm.toLowerCase()
      && orch.workspaceMatches(a.workspace, qualWs || ''));
    if (dups.length > 1) {
      const cands = dups.map((d) => d.workspace ? `${SELF_HOST}/${d.workspace}/${d.name}` : `${SELF_HOST}/${d.pane || d.slot}`);
      console.warn(`[bus] '${nm}' ambiguous across ${dups.length} local instances — qualify: ${cands.join(', ')}`);
    }
    return null;
  }
  // Single-owner election: defer to another host that positively owns this name. Skipped for a
  // qualified target — an explicit `host/` prefix naming THIS host IS the owner designation.
  if (PRESENCE_ENABLED && !qualHost && !qualWs) {
    const owner = orch.ownerOf(presenceMap, nm);
    if (owner && owner !== SELF_HOST) { console.warn(`[bus] ${nm} owned by ${owner}, not ${SELF_HOST} — deferring delivery`); return null; }
  }
  return agent;
}

// Idle-gate + deliver a ready text to a resolved agent. Injects only at @waiting=2 (and not
// while a human is mid-draft in the pane); otherwise holds in the per-pane queue for flush on
// idle — so a running task is never interrupted and the message is never lost.
function deliverOrQueue(agent, text, label) {
  const target = label || agent.name || '?';
  if (BUS_DEFER && agent.pane) {
    const w = paneWaiting(agent.pane);
    if (w !== '2') { enqueueBus(agent.pane, target, text); console.log(`[bus] ${target} busy (@waiting=${w || 'unset'}) — queued for idle delivery`); return; }
    if (HUMAN_GUARD && humanTyping(agent.pane)) { enqueueBus(agent.pane, target, text); console.log(`[bus] ${target} idle but human is typing — queued (grace ${HUMAN_TYPING_GRACE_MS}ms)`); return; }
  }
  const res = agent.pane ? deliverToPane(agent.pane, text) : deliverToSlot(agent.slot, text);
  if (res.ok) { flashPane(agent.pane, 'bus msg'); console.log(`[bus] delivered to ${target} (${agent.pane || agent.slot}): ${text.slice(0, 60)}`); }
  else console.error(`[bus] delivery to ${target} failed: ${res.error}`);
}

// Intercept a `reply` that answers an outstanding POST /request (matched by `corr`): resolve
// the awaiting HTTP caller and do NOT deliver to an agent. Returns true if intercepted.
function interceptReply(env) {
  if (env.kind !== 'reply' || !env.corr) return false;
  const pending = pendingRequests.get(env.corr);
  if (!pending) return false;
  pendingRequests.delete(env.corr);
  clearTimeout(pending.timer);
  pending.resolve({ status: 'ok', from: env.from, body: env.body, id: env.corr });
  console.log(`[bus] request ${env.corr} answered by ${env.from}`);
  return true;
}

// Route a typed envelope to its local target: intercept a /request reply first, else resolve
// `to` and deliver the kind-rendered text. Shared by the Slack typed-line path + NATS consumer.
function routeEnvelope(env) {
  if (interceptReply(env)) return;
  const agent = resolveBusTarget(env.to);
  if (!agent) return;                           // not ours / unresolved (resolveBusTarget logged)
  deliverOrQueue(agent, orch.renderDelivery(env), agent.name || env.to);
}

// Inter-agent bus delivery on the Slack transport: a channel line `to: body` posted by some
// host's /send. If `body` is a typed-envelope sentinel, route the envelope; otherwise it's a
// legacy `msg` whose body already carries the `↩ from <sender>:` prefix baked in at post time —
// deliver it verbatim (host-local ownership rule, no re-post → no loop).
async function handleBusMessage(event) {
  if (!event.text) return;
  const parsed = orch.parseAddressedLine(cleanSlackText(event.text));
  if (!parsed) return;
  const env = orch.parseEnvelope(parsed.body);   // non-null only for a `::nexus-env::` sentinel body
  if (env) { if (!env.to) env.to = parsed.token; routeEnvelope(env); return; }
  const agent = resolveBusTarget(parsed.token);
  if (agent) deliverOrQueue(agent, parsed.body, agent.name || parsed.token);
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
    // Relays are human-facing (an agent sharing output for a person to read in
    // Slack). Route them out of delivery too — never send-keys, never re-post.
    const relay = orch.parseRelay(event.text || '');
    if (relay) { console.log(`[relay] from ${relay.from}: ${relay.text.slice(0, 80)}`); return; }
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
        markMessaged(entry.pane, entry.name, channel);   // ping when it finishes the resumed work
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

  // 2. Addressed top-level: "name: text", "slot: text", or "wN:pN: text" (a pane handle),
  //    after any @bot mention. Same colon-space parser as the bus path, so a human can
  //    address one exact instance by handle when a name collides (two `general`s).
  const addr = orch.parseAddressedLine(cleaned);
  if (addr) {
    const rawTarget = addr.token;
    const text = addr.body;
    // Pane handle → exact instance; numeric → slot; else [host/][workspace/]name (nx-resolve).
    let agent = null;
    if (/^w[A-Za-z0-9]+:p[A-Za-z0-9]+$/.test(rawTarget)) {
      agent = resolveByPane(rawTarget);
    } else if (/^\d+$/.test(rawTarget)) {
      agent = resolveBySlot(rawTarget);
    } else {
      const parsed = orch.parseAddress(rawTarget, { knownHosts: new Set(presenceMap.keys()), selfHost: SELF_HOST });
      agent = resolveByName(parsed.name, parsed.workspace);
    }
    if (agent) {
      const res = agent.pane ? deliverToPane(agent.pane, text) : deliverToSlot(agent.slot, text);
      if (res.ok) {
        flashPane(agent.pane, answerLabel(text));          // mirror the message onto the agent's terminal
        await react(channel, event.ts, 'white_check_mark');
        markMessaged(agent.pane, agent.name, channel);     // ping when it finishes
        // Receipt-with-state: tell you whether it took the message now or queued it.
        const s = orch.statusLabel(paneWaiting(agent.pane));
        const note = s.key === 'active' ? "queued — it's mid-task; it'll pick this up at its next turn"
                   : s.key === 'waiting' ? "note: it's at a permission prompt, so this waits until that's answered"
                   : 'on it now';
        await replyInThread(channel, event.ts, `:inbox_tray: \`${agent.name}\` ${s.emoji} ${s.text} · ${note}`);
      } else {
        await react(channel, event.ts, 'x');
        await replyInThread(channel, event.ts, `:warning: ${res.error}`);
      }
      return;
    }
    // Unresolved: a slash-qualified target might just be free-text ("foo/bar: note") — fall
    // through to the classifier rather than hard-error. A bare unknown name still warns.
    if (!rawTarget.includes('/')) {
      await replyInThread(channel, event.ts,
        `:warning: no active agent \`${rawTarget}\`. Active: ${liveAgentList()}`);
      return;
    }
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
            markMessaged(match.pane, match.name, channel);   // ping when it finishes
            const s = orch.statusLabel(paneWaiting(match.pane));
            await replyInThread(channel, event.ts,
              `:robot_face: routed to \`${match.name}\` (auto · ${Math.round(Number(verdict.confidence) * 100)}%) ${s.emoji} ${s.text}. Use \`name: …\` to override.`);
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

// Publish a typed envelope via the ACTIVE transport. NATS → publish to the target's subject.
// Slack → post to the agents channel: a `msg` stays the human-readable addressed line (old
// bridges + humans read it), a typed kind uses `to: <::nexus-env:: json>` (addressed so the
// owning host routes it, sentinel so it never parses as a plain delivery). Returns transport info.
async function publishEnv(env) {
  if (BUS_TRANSPORT === 'nats') {
    if (!natsReady) throw new Error('nats transport not connected');
    const fqdn = orch.parseAddress(env.to, { knownHosts: new Set(presenceMap.keys()), selfHost: SELF_HOST });
    return natsTransport.publish(fqdn, env);
  }
  if (!AGENTS_CHANNEL) throw new Error('SLACK_AGENTS_CHANNEL not set');
  const text = env.kind === 'msg'
    ? `${env.to}: ↩ from ${env.from}: ${env.body}`
    : `${env.to}: ${orch.formatEnvelope(env)}`;
  const posted = await web.chat.postMessage({ channel: AGENTS_CHANNEL, text });
  return { ts: posted.ts };
}

// ---------------------------------------------------------------------------
// Outbound: localhost HTTP for the Notification hook.
//   POST /notify  { name, message, slot?, pane? }        -> post to #nexus, track thread
//   POST /send    { to, from, msg, kind?, corr?, reply_to? } -> publish an A2A envelope
//   POST /request { to, body, deadline_ms?, from? }      -> publish a request, await the reply
//   GET  /health | /agents | /status
// ---------------------------------------------------------------------------
const httpServer = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');

  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    const busUp = BUS_ENABLED && (BUS_TRANSPORT === 'nats' ? natsReady : !!AGENTS_CHANNEL);
    res.end(JSON.stringify({ ok: true, connected: socketConnected, threads: threadMap.size, bus: busUp, transport: BUS_TRANSPORT, nats: BUS_TRANSPORT === 'nats' ? natsReady : undefined, presence: PRESENCE_ENABLED, host: SELF_HOST }));
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
          ? `${category ? `*[${String(category).slice(0, 60)}]*\n` : ''}:robot_face: ${orch.capWithMarker(summary, 2500)}`
          : orch.capWithMarker(message || 'needs your input', 2500);
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
        if (!BUS_ENABLED) {
          res.writeHead(409);
          res.end(JSON.stringify({ ok: false, error: 'bus disabled (set SLACK_BUS_ENABLED=1)' }));
          return;
        }
        const { to, from, msg, kind, corr, reply_to } = JSON.parse(body || '{}');
        if (!to || !msg) { res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'to and msg required' })); return; }
        // Self-identify by FQDN (SELF_HOST/[workspace/]name) by default so the recipient can
        // reply across hosts unambiguously; the bridge owns SELF_HOST, so it stamps the host.
        const sender = qualifyFrom(from).slice(0, 120);
        const capped = orch.capWithMarker(msg, BUS_MAX_CHARS);
        // Build the typed envelope; the bridge stamps the id + ts (one uuid source). A bare
        // send (no kind) is `msg`, unchanged on the wire. A `request` defaults reply_to to the
        // sender so the answer routes back to them.
        const env = orch.buildEnvelope({
          id: shortId(), ts: Date.now(), from: sender, to, kind: kind || 'msg', corr,
          reply_to: reply_to || ((kind === 'request') ? sender : undefined), body: capped,
        });
        const info = await publishEnv(env);
        res.writeHead(200);
        res.end(JSON.stringify({ ok: true, id: env.id, ...info }));
      } catch (e) {
        if (/not connected/.test(e.message)) { res.writeHead(503); res.end(JSON.stringify({ ok: false, error: e.message })); return; }
        if (/SLACK_AGENTS_CHANNEL/.test(e.message)) { res.writeHead(409); res.end(JSON.stringify({ ok: false, error: e.message })); return; }
        res.writeHead(500);
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
    return;
  }

  // Request/reply: publish a `request` to an agent and AWAIT its reply. A skill/loop/Conductor
  // node can ask an agent and get a structured answer (async — the agent replies on its turn).
  // reply_to is a bridge-local address (SELF_HOST/_req/<id>) so the reply routes back to THIS
  // host's consumer; interceptReply matches `corr` and resolves this waiter. Always resolves —
  // on a reply, or with {status:'timeout'} at the deadline — so the HTTP call never hangs.
  if (req.method === 'POST' && url.pathname === '/request') {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', async () => {
      res.setHeader('Content-Type', 'application/json');
      try {
        if (!BUS_ENABLED) { res.writeHead(409); res.end(JSON.stringify({ ok: false, error: 'bus disabled (set SLACK_BUS_ENABLED=1)' })); return; }
        const parsed = JSON.parse(body || '{}');
        const { to, deadline_ms } = parsed;
        const reqBody = parsed.body != null ? parsed.body : parsed.msg;
        if (!to || !reqBody) { res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'to and body required' })); return; }
        const id = shortId();
        const replyAddr = `${SELF_HOST}/_req/${id}`;   // bridge-local; interceptReply matches corr=id
        const env = orch.buildEnvelope({
          id, ts: Date.now(), from: parsed.from ? qualifyFrom(parsed.from).slice(0, 120) : replyAddr,
          to, kind: 'request', reply_to: replyAddr, body: orch.capWithMarker(String(reqBody), BUS_MAX_CHARS),
        });
        await publishEnv(env);
        const deadline = Math.max(1000, parseInt(deadline_ms, 10) || REQUEST_TTL_MS);
        const result = await new Promise((resolve) => {
          const timer = setTimeout(() => { pendingRequests.delete(id); resolve({ status: 'timeout', id }); }, deadline);
          pendingRequests.set(id, { resolve, timer, at: Date.now() });
        });
        res.writeHead(200);
        res.end(JSON.stringify({ ok: true, ...result }));
      } catch (e) {
        if (/not connected/.test(e.message)) { res.writeHead(503); res.end(JSON.stringify({ ok: false, error: e.message })); return; }
        if (/SLACK_AGENTS_CHANNEL/.test(e.message)) { res.writeHead(409); res.end(JSON.stringify({ ok: false, error: e.message })); return; }
        res.writeHead(500);
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
    return;
  }

  // Relay: an agent shares its output into #nexus-agents for a HUMAN to read
  // (the copy-paste killer). Unlike /send this is not addressed and never
  // delivered — the sentinel keeps handleBusMessage from parsing the body as a
  // `name:` delivery, and the bridge routes it out of the delivery path. Reuses
  // the same channel + BUS_MAX_CHARS cap as /send.
  if (req.method === 'POST' && url.pathname === '/relay') {
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
        const { from, text } = JSON.parse(body || '{}');
        if (!text) { res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'text required' })); return; }
        const sender = String(from || 'unknown').slice(0, 80);
        const out = orch.formatRelay({ from: sender, host: SELF_HOST, text: orch.capWithMarker(String(text), BUS_MAX_CHARS) });
        const posted = await web.chat.postMessage({ channel: AGENTS_CHANNEL, text: out });
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

  // Completion ping: announce when a Slack-messaged agent settles back at idle.
  if (DONE_PING) {
    setInterval(() => { doneSweep().catch((e) => console.error(`[slack-bridge] done sweep error: ${e.message}`)); }, DONE_POLL_MS);
    console.log(`[slack-bridge] completion ping ON (poll ${DONE_POLL_MS}ms, stable ${DONE_STABLE_MS}ms; SLACK_DONE_PING=0 to disable)`);
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

  if (BUS_ENABLED && BUS_TRANSPORT === 'nats') {
    // NATS A2A transport: connect, subscribe our host subtree, and deliver inbound
    // messages through the SAME path as the Slack bus (handleBusMessage → resolution +
    // idle-gate + send-keys/inbox). The human notify/reply leg stays on Slack. Best-effort:
    // a NATS failure logs and leaves A2A-over-NATS down without affecting the Slack legs.
    try {
      const { createNatsTransport } = await import('./transports/nats-transport.js');
      natsTransport = createNatsTransport({
        selfHost: SELF_HOST,
        url: NATS_URL,
        streamName: NATS_A2A_STREAM,
        subjectPrefix: NATS_A2A_SUBJECT_PREFIX,
        kvBucket: NATS_PRESENCE_KV,
        presenceTtlMs: PRESENCE_TTL_MS,
        credsFile: NATS_CREDS || undefined,
        token: NATS_TOKEN || undefined,
        user: NATS_USER || undefined,
        pass: NATS_PASS || undefined,
      });
      await natsTransport.connect();
      // Inbound: the payload IS a typed envelope (Phase B) — parse + route through the shared
      // path (reply-intercept → resolve → idle-gated deliver of the kind-rendered text). A
      // legacy {to,from,msg} record still parses as a `msg`. ack-on-receive: the in-memory
      // busQueue idle-gates a busy recipient as today. (Ack-on-idle — holding the JetStream
      // message un-acked until delivery so a hold survives a restart — is a tracked follow-up.)
      await natsTransport.subscribe(async (envelope, msg) => {
        try { const env = orch.parseEnvelope(envelope); if (env) routeEnvelope(env); }
        catch (e) { console.error(`[nats] delivery failed for ${envelope && envelope.to}: ${e.message}`); }
        finally { try { msg.ack(); } catch { /* connection draining */ } }
      });
      natsReady = true;
      console.log(`[slack-bridge] A2A transport = NATS ENABLED (${NATS_URL}, stream=${NATS_A2A_STREAM}, prefix=${NATS_A2A_SUBJECT_PREFIX})`);
      if (BUS_DEFER) {
        setInterval(flushBusQueue, BUS_FLUSH_MS);
        console.log(`[slack-bridge] bus idle-gated delivery ON (deliver on @waiting=2, flush every ${BUS_FLUSH_MS}ms, queue cap ${BUS_QUEUE_MAX}/pane)`);
      }
      // Presence: upsert our live local agents into the JetStream KV on a heartbeat so any
      // bridge can build reachability + resolve bare names. TTL ages out reaped agents.
      const pushNatsPresence = async () => {
        try { await natsTransport.presenceUpsert(loadRegistry()); }
        catch (e) { console.error(`[nats] presence upsert failed: ${e.message}`); }
      };
      await pushNatsPresence();
      setInterval(() => { pushNatsPresence().catch(() => {}); }, PRESENCE_HEARTBEAT_MS);
      console.log(`[slack-bridge] NATS presence heartbeat every ${PRESENCE_HEARTBEAT_MS}ms (bucket=${NATS_PRESENCE_KV})`);
    } catch (e) {
      console.error(`[slack-bridge] NATS transport failed to start: ${e.message} — A2A over NATS is DOWN (Slack human legs unaffected)`);
    }
  } else if (BUS_ENABLED && AGENTS_CHANNEL) {
    console.log(`[slack-bridge] inter-agent bus ENABLED (channel=${AGENTS_CHANNEL})`);
    if (BUS_DEFER) {
      setInterval(flushBusQueue, BUS_FLUSH_MS);
      console.log(`[slack-bridge] bus idle-gated delivery ON (deliver on @waiting=2, flush every ${BUS_FLUSH_MS}ms, queue cap ${BUS_QUEUE_MAX}/pane)`);
      if (HUMAN_GUARD) {
        console.log(`[slack-bridge] human-typing guard ON (defer while a human typed into the recipient pane <${HUMAN_TYPING_GRACE_MS}ms ago)`);
      }
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
