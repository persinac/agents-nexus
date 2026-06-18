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
  await socket.start();
})();
