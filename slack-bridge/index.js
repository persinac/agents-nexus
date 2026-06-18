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
      if (name && slot) out.push({ name: name.trim(), slot: slot.trim(), pane: (pane || '').trim() });
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

async function replyInThread(channel, thread_ts, text) {
  try { await web.chat.postMessage({ channel, thread_ts, text }); } catch (e) {
    console.error(`[slack-bridge] reply failed: ${e.message}`);
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

async function handleReaction(event) {
  if (!event || event.type !== 'reaction_added') return;
  if (selfUserId && event.user === selfUserId) return;        // ignore the bot's own reactions
  const digit = REACTION_DIGIT[event.reaction];
  if (!digit) return;
  const item = event.item;
  if (!item || item.type !== 'message') return;
  const entry = threadMap.get(item.ts);                       // reacted on a tracked prompt root?
  if (!entry) return;
  const res = entry.pane ? deliverToPane(entry.pane, digit) : deliverToName(entry.name, digit);
  if (res.ok) {
    try { await react(item.channel, item.ts, 'eyes'); } catch { /* ack reaction — best effort */ }
  } else {
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

  // 1. Reply inside a tracked thread -> that thread's agent
  if (isReply && threadMap.has(event.thread_ts)) {
    const entry = threadMap.get(event.thread_ts);
    let text = cleanSlackText(event.text);
    // Permission-prompt threads: translate yes/no into the menu digit so the
    // agent's select actually advances. Unmapped replies pass through verbatim.
    if (PERMISSION_KINDS.has(entry.kind)) {
      const digit = permissionReplyToDigit(text);
      if (digit) text = digit;
    }
    // Deliver to the exact pane captured at notify-time — drift-proof, unlike the
    // agent name (which re-resolves through stale/duplicate registry slots).
    const res = entry.pane ? deliverToPane(entry.pane, text) : deliverToName(entry.name, text);
    if (res.ok) {
      await react(channel, event.ts, 'white_check_mark');
    } else {
      await react(channel, event.ts, 'x');
      await replyInThread(channel, event.thread_ts, `:warning: ${res.error}`);
    }
    return;
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
      await react(channel, event.ts, 'white_check_mark');
    } else {
      await react(channel, event.ts, 'x');
      await replyInThread(channel, event.ts, `:warning: ${res.error}`);
    }
    return;
  }

  // 3. Unaddressed / untracked -> usage hint
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
        const { name, message, pane, kind, category, summary } = JSON.parse(body || '{}');
        if (!name) { res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'name required' })); return; }
        if (!NEXUS_CHANNEL) { res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'SLACK_NEXUS_CHANNEL not set' })); return; }
        // Rich middle-man payload ({category, summary}) renders as a [category] tag +
        // an attributed summary; a bare {message} (elicitation / no-classifier fallback)
        // keeps the simple one-line form. Each line is `> `-prefixed for a clean blockquote.
        let text;
        if (summary) {
          const cat = category ? `> [${String(category).slice(0, 60)}]\n` : '';
          const sumLines = String(summary).slice(0, 1200).split('\n').map((l) => `> 🤖 ${l}`).join('\n');
          text = `:hourglass_flowing_sand: *${name}* needs input:\n${cat}${sumLines}\n_Reply in thread_ · or react :one: / :two: / :three:`;
        } else {
          const s = (message || 'needs your input').toString().slice(0, 500);
          text = `:hourglass_flowing_sand: *${name}* needs input:\n> ${s}\n_Reply in this thread to answer._`;
        }
        const posted = await web.chat.postMessage({ channel: NEXUS_CHANNEL, text });
        threadMap.set(posted.ts, { name, channel: NEXUS_CHANNEL, pane: pane || '', kind: kind || '', ts: posted.ts, createdAt: Date.now() });
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
  await socket.start();
})();
