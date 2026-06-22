/**
 * Orchestrator — the spawn branch of the Slack bridge.
 *
 * When an inbound #nexus message matches no running agent, the bridge can offer
 * to spin up the right agent for it: resolve the repo (Spark), confirm with a
 * human (Block Kit), and on approval spawn a seeded tmux agent — bounded by an
 * allowlist, a per-repo in-flight lock, and a global rate-limit. It also backs
 * resilience: restoring dormant (reaped) agents from the durable ledger.
 *
 * This module holds the pure / side-effect-isolated pieces (config loading,
 * guardrail math, child-process calls to the Spark resolver, the agent ledger,
 * and `tmux new-window`, plus Block Kit builders) so they are unit-testable.
 * index.js owns the stateful glue (the lock Set, rate-limit ring, pending-spawn
 * map) and all Slack posting. Everything here is inert unless SLACK_SPAWN_ENABLED
 * is set — index.js gates the call sites.
 */
import { execFile } from 'child_process';
import { readFileSync, existsSync } from 'fs';

// --------------------------------------------------------------------------
// Config: spawnable-repo allowlist. JSON object mapping the repo name (as Spark
// returns it) to the absolute local checkout path to spawn the agent in. This
// is BOTH the safety gate (only listed repos are spawnable) and the name->path
// resolver (Spark indexes many repos that are not cloned locally). Re-read on
// each call so edits take effect without a bridge restart.
// --------------------------------------------------------------------------
export function loadAllowlist(file) {
  try {
    if (!existsSync(file)) return {};
    const obj = JSON.parse(readFileSync(file, 'utf8'));
    return obj && typeof obj === 'object' ? obj : {};
  } catch {
    return {};
  }
}

// An allowlist value may be a bare path string, or an object { path, desc }
// (desc gives the repo classifier context). Normalize to { path, desc }.
function normalizeEntry(v) {
  if (v && typeof v === 'object') return { path: v.path || '', desc: v.desc || v.description || '' };
  return { path: v || '', desc: '' };
}

// Keys that are never real repos (config comments).
function isMeta(key) { return key.startsWith('__'); }

// Match a repo name against the allowlist, returning the canonical
// { name, path, desc } (the config key is canonical — used as the lock, ledger,
// window, and PROJECT_SLUG key so everything agrees), or null if not spawnable.
export function matchAllowlist(allowlist, repo) {
  if (!repo) return null;
  if (Object.prototype.hasOwnProperty.call(allowlist, repo) && !isMeta(repo)) {
    return { name: repo, ...normalizeEntry(allowlist[repo]) };
  }
  const lower = repo.toLowerCase();
  for (const [k, v] of Object.entries(allowlist)) {
    if (!isMeta(k) && k.toLowerCase() === lower) return { name: k, ...normalizeEntry(v) };
  }
  return null;
}

// All spawnable repos as [{ name, path, desc }] (config comments excluded) —
// the candidate set the repo classifier picks from.
export function allowlistEntries(allowlist) {
  return Object.entries(allowlist)
    .filter(([k]) => !isMeta(k))
    .map(([name, v]) => ({ name, ...normalizeEntry(v) }));
}

// Spark-derived descriptions cache: { repo -> description }, produced by
// scripts/spark-summary.py from the live Spark index (auto-maintained nightly).
// Best-effort: a missing/garbage file yields {} so the classifier still runs on
// whatever hand-written descriptions exist. Re-read on each call like the
// allowlist, so a nightly refresh takes effect without a bridge restart.
export function loadSummaries(file) {
  try {
    if (!file || !existsSync(file)) return {};
    const obj = JSON.parse(readFileSync(file, 'utf8'));
    return obj && typeof obj === 'object' ? obj : {};
  } catch {
    return {};
  }
}

// Fill each entry's `desc` from the Spark cache ONLY where no hand-written desc
// exists — a hand-written description in the allowlist always wins (it is the
// curator's override). Returns a new array; never mutates its inputs.
export function mergeSummaries(entries, summaries) {
  const cache = summaries || {};
  return entries.map((e) => (e.desc ? e : { ...e, desc: cache[e.name] || '' }));
}

// --------------------------------------------------------------------------
// Spark repo resolution — shell out to scripts/spark-resolve.py (which talks to
// the live Spark MCP service). Returns { repo, score } or null. NEVER throws.
// minScore is applied here so the bridge sees null when nothing clears the bar.
// --------------------------------------------------------------------------
export function resolveRepo(text, { python, script, minScore = 0, timeoutMs = 25000, env = {} } = {}) {
  return new Promise((resolve) => {
    execFile(python, [script, text], { timeout: timeoutMs, env: { ...process.env, ...env } },
      (err, stdout) => {
        if (err && !stdout) { resolve(null); return; }
        try {
          const out = JSON.parse(String(stdout).trim().split('\n').pop() || '{}');
          if (!out.repo) { resolve(null); return; }
          if (typeof out.score === 'number' && out.score < minScore) { resolve(null); return; }
          resolve({ repo: out.repo, score: out.score });
        } catch {
          resolve(null);
        }
      });
  });
}

// --------------------------------------------------------------------------
// Agent ledger — shell out to scripts/agent-ledger.py. Returns parsed JSON for
// the subcommand, or null on any failure (best-effort, never throws).
// --------------------------------------------------------------------------
export function ledger(args, { python = 'python3', script, timeoutMs = 8000, env = {} } = {}) {
  return new Promise((resolve) => {
    execFile(python, [script, ...args], { timeout: timeoutMs, env: { ...process.env, ...env } },
      (err, stdout) => {
        if (err && !stdout) { resolve(null); return; }
        const lines = String(stdout).trim().split('\n').filter(Boolean);
        try { resolve(JSON.parse(lines.pop() || 'null')); }
        catch { resolve(null); }
      });
  });
}

// --------------------------------------------------------------------------
// Guardrail math (pure). Rate-limit over a rolling window: returns the pruned
// timestamp list and whether another spawn is allowed right now.
// --------------------------------------------------------------------------
export function rateState(timestamps, max, windowMs, now) {
  const cutoff = now - windowMs;
  const recent = timestamps.filter((t) => t >= cutoff);
  return { recent, allowed: recent.length < max };
}

// --------------------------------------------------------------------------
// Spawn a tmux agent window. Mirrors launch-claude.sh's invocation:
//   tmux new-window -d -n <name> -c <cwd> "env PROJECT_SLUG=.. [SEED_PROMPT=..]
//      [RESTORE_CHECKPOINT=..] <open-claude.sh>"
// The child env is built via `env` so values with spaces are safe (we pass the
// whole command as one string to tmux, which runs it through the shell — so we
// hand-build a single-quoted command and escape embedded quotes).
// Returns { ok, error }.
// --------------------------------------------------------------------------
function shQuote(s) {
  // POSIX single-quote escaping: ' -> '\''
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

export function buildSpawnCommand({ slug, seed, restoreCheckpoint, openClaude }) {
  const parts = ['env', `PROJECT_SLUG=${shQuote(slug)}`];
  if (seed) parts.push(`SEED_PROMPT=${shQuote(seed)}`);
  if (restoreCheckpoint) parts.push(`RESTORE_CHECKPOINT=${shQuote(restoreCheckpoint)}`);
  parts.push(shQuote(openClaude));
  return parts.join(' ');
}

export function spawnWindow({ session, name, cwd, slug, seed, restoreCheckpoint, openClaude, timeoutMs = 8000 }) {
  const command = buildSpawnCommand({ slug: slug || name, seed, restoreCheckpoint, openClaude });
  // -P -F prints the new pane id + window index so we can report/ledger identity.
  const args = ['new-window', '-dP', '-F', '#{pane_id}\t#{window_index}', '-t', session, '-n', name, '-c', cwd, command];
  return new Promise((resolve) => {
    execFile('tmux', args, { timeout: timeoutMs }, (err, stdout, stderr) => {
      if (err) { resolve({ ok: false, error: (stderr || err.message || 'tmux new-window failed').toString().trim() }); return; }
      const [pane, slot] = String(stdout).trim().split('\t');
      resolve({ ok: true, pane: pane || '', slot: slot || '' });
    });
  });
}

// Is there already a live agent for this repo? Checks the registry by name and
// by cwd (an agent whose working dir is the repo's path or under it).
export function repoHasLiveAgent(registry, repo, repoPath) {
  const lower = String(repo).toLowerCase();
  return registry.some((a) => {
    if (a.name && a.name.toLowerCase() === lower) return true;
    if (repoPath && a.cwd && (a.cwd === repoPath || a.cwd.startsWith(`${repoPath}/`))) return true;
    return false;
  });
}

// --------------------------------------------------------------------------
// Block Kit builders.
// --------------------------------------------------------------------------
export function confirmCard({ repo, cwd, score, requester }) {
  const pct = typeof score === 'number' ? ` · match ${Math.round(score * 1000) / 10}%` : '';
  return [
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `:sparkles: No agent is running for this. Spin one up in \`${repo}\`?${pct}\n_${cwd}_`,
      },
    },
    {
      type: 'actions',
      block_id: 'spawn_actions',
      elements: [
        { type: 'button', action_id: 'spawn:yes', style: 'primary', value: repo, text: { type: 'plain_text', text: '🚀 Spin it up', emoji: true } },
        { type: 'button', action_id: 'spawn:no', style: 'danger', value: repo, text: { type: 'plain_text', text: 'No thanks', emoji: true } },
      ],
    },
    { type: 'context', elements: [{ type: 'mrkdwn', text: requester ? `_requested by <@${requester}>_` : '_confirm to launch_' }] },
  ];
}

export function nudgeCard(dormant) {
  const repos = dormant.slice(0, 5);
  const blocks = [
    {
      type: 'section',
      text: {
        type: 'mrkdwn',
        text: `:zzz: *${dormant.length}* agent${dormant.length === 1 ? '' : 's'} ${dormant.length === 1 ? 'was' : 'were'} reaped while you were away. Restore from checkpoint?`,
      },
    },
    {
      type: 'actions',
      block_id: 'restore_actions',
      elements: repos.map((r) => ({
        type: 'button', action_id: 'restore:do', value: r.repo || r.name,
        text: { type: 'plain_text', text: `↩ ${r.repo || r.name}`, emoji: true },
      })),
    },
    { type: 'context', elements: [{ type: 'mrkdwn', text: '_pick one to bring back · nothing is restored automatically_' }] },
  ];
  return blocks;
}

// Rebuild a card after a decision: keep section blocks, drop actions, add a note.
export function resolvedCard(message, note) {
  const kept = (((message || {}).blocks) || []).filter((b) => b.type === 'section');
  kept.push({ type: 'context', elements: [{ type: 'mrkdwn', text: note }] });
  return kept;
}

// --------------------------------------------------------------------------
// Presence registry (Phase 2).
//
// Phase 1 delivery is host-local: a bridge delivers a message addressed to a
// name only if that name is in ITS OWN registry. With unique-by-convention
// names that works, but two hosts that both registered the same name would BOTH
// deliver (double delivery), and there is no way to ask "who is reachable across
// the fleet?". Phase 2 has every bridge ANNOUNCE its live local agent set on the
// same bus channel (reusing the Socket Mode fan-out — no shared store, no host
// discovery) and CONSUME peers into an in-memory map: host -> { agents:Set, ts }.
// From that map we derive a single deterministic owner per name (so exactly one
// host delivers), detect name collisions, and answer reachability. These pieces
// are pure; index.js owns the map object, the timers, and all Slack I/O.
// --------------------------------------------------------------------------

// Marks a bus message as a presence announcement, not an addressed A2A message.
// It starts with a non-alnum char so the addressed parser (^[A-Za-z0-9]…) never
// mistakes it for a `name: text` delivery.
export const PRESENCE_SENTINEL = '::nexus-presence::';

// Format a presence announcement: sentinel + a compact JSON state snapshot.
// Full-state (not deltas) so a missed message self-heals on the next beat.
export function formatPresence({ host, agents, ts }) {
  const payload = { v: 1, host: String(host), agents: Array.from(agents || []), ts: ts || 0 };
  return `${PRESENCE_SENTINEL} ${JSON.stringify(payload)}`;
}

// Parse a presence announcement back to { host, agents:string[], ts }, or null
// if the text is not a well-formed presence message.
export function parsePresence(text) {
  if (typeof text !== 'string') return null;
  const s = text.trim();
  if (!s.startsWith(PRESENCE_SENTINEL)) return null;
  try {
    const obj = JSON.parse(s.slice(PRESENCE_SENTINEL.length).trim());
    if (!obj || typeof obj.host !== 'string' || !Array.isArray(obj.agents)) return null;
    return { host: obj.host, agents: obj.agents.map(String), ts: Number(obj.ts) || 0 };
  } catch { return null; }
}

// Apply a snapshot to the map (host -> { agents:Set, ts, seen }). Ignores an
// out-of-order snapshot (older sender ts than the one we hold for that host).
// An empty agent set still records the host (a host that drained to zero agents
// is known-empty, not left phantom-owning a stale name). Mutates + returns map.
export function applyPresence(map, snap, { now = 0 } = {}) {
  if (!snap || !snap.host) return map;
  const prev = map.get(snap.host);
  if (prev && snap.ts && prev.ts && snap.ts < prev.ts) return map;
  map.set(snap.host, { agents: new Set(snap.agents), ts: snap.ts || now, seen: now || snap.ts || 0 });
  return map;
}

// Drop hosts whose last snapshot is older than ttlMs (a host that stopped
// heartbeating — crashed / offline). Mutates + returns the map.
export function expirePresence(map, { now, ttlMs }) {
  for (const [host, rec] of map) {
    if (now - (rec.seen || rec.ts || 0) > ttlMs) map.delete(host);
  }
  return map;
}

// Hosts that currently claim `name`, sorted deterministically so every bridge
// computes the same order (case-insensitive match; original host strings kept).
export function ownersOf(map, name) {
  const lower = String(name).toLowerCase();
  const hosts = [];
  for (const [host, rec] of map) {
    for (const a of rec.agents) { if (String(a).toLowerCase() === lower) { hosts.push(host); break; } }
  }
  return hosts.sort();
}

// The single deterministic owner of `name` (lexically-smallest claiming host),
// or null if no host in the map claims it. Every bridge agrees → one delivers.
export function ownerOf(map, name) {
  const hosts = ownersOf(map, name);
  return hosts.length ? hosts[0] : null;
}

// Names claimed by more than one host — the collisions to surface/disambiguate.
export function presenceCollisions(map) {
  const byName = new Map(); // lowerName -> { display, hosts:Set }
  for (const [host, rec] of map) {
    for (const a of rec.agents) {
      const k = String(a).toLowerCase();
      if (!byName.has(k)) byName.set(k, { display: a, hosts: new Set() });
      byName.get(k).hosts.add(host);
    }
  }
  const out = [];
  for (const { display, hosts } of byName.values()) {
    if (hosts.size > 1) out.push({ name: display, hosts: Array.from(hosts).sort() });
  }
  return out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

// Reachability projection: every (name, host) pair in the map, annotated with the
// resolved single `owner` and a `collided` flag. Sorted by name then host.
export function reachability(map) {
  const collided = new Set(presenceCollisions(map).map((c) => c.name.toLowerCase()));
  const rows = [];
  for (const [host, rec] of map) {
    for (const a of rec.agents) {
      rows.push({ name: a, host, owner: ownerOf(map, a), collided: collided.has(String(a).toLowerCase()) });
    }
  }
  return rows.sort((x, y) => (x.name < y.name ? -1 : x.name > y.name ? 1 : (x.host < y.host ? -1 : x.host > y.host ? 1 : 0)));
}

// --------------------------------------------------------------------------
// `status` command formatters (pure). index.js reads each live agent's
// registry entry + hook-maintained window options (`@waiting`/`@wait_since`/
// `@last_tool`) and hands the plain data here to render a roll-up that the
// bridge posts back to Slack. Data in -> mrkdwn out, so they're unit-testable.
// --------------------------------------------------------------------------

// Map the hook-maintained `@waiting` value to a user-facing status:
//   '0'/'' (unset) -> active  (working; a tool is running)   :large_green_circle:
//   '1'            -> waiting (at a permission prompt; needs you) :large_yellow_circle:
//   '2'            -> idle    (done, sitting at the prompt)   :white_circle:
export function statusLabel(waiting) {
  const w = String(waiting ?? '').trim();
  if (w === '1') return { key: 'waiting', emoji: ':large_yellow_circle:', text: 'waiting on you' };
  if (w === '2') return { key: 'idle', emoji: ':white_circle:', text: 'idle' };
  return { key: 'active', emoji: ':large_green_circle:', text: 'working' };
}

// Compact human duration: 45 -> "45s", 184 -> "3m", 7600 -> "2h6m".
export function fmtAgo(secs) {
  const s = Math.max(0, Math.floor(Number(secs) || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

// Short repo label from a cwd: last two path segments
// ("/home/u/repos/flashback-fleet/infra" -> "flashback-fleet/infra").
function repoShort(cwd) {
  if (!cwd) return '';
  const parts = String(cwd).split('/').filter(Boolean);
  return parts.slice(-2).join('/') || String(cwd);
}

// Seconds the agent has been in its current state, from the per-window
// timestamps (waiting -> @wait_since; active/idle -> @last_tool). null if unknown
// or inconsistent (missing / in the future).
function stateAgeSecs(agent, nowMs) {
  const nowS = Math.floor((nowMs ?? Date.now()) / 1000);
  const key = statusLabel(agent.waiting).key;
  const ref = Number(key === 'waiting' ? agent.waitSince : agent.lastTool);
  if (!Number.isFinite(ref) || ref <= 0 || ref > nowS) return null;
  return nowS - ref;
}

// True when an agent looks "working" but hasn't run a tool for > stuckMin minutes.
function isStuck(agent, nowMs, stuckMin) {
  if (!stuckMin || statusLabel(agent.waiting).key !== 'active') return null;
  const nowS = Math.floor((nowMs ?? Date.now()) / 1000);
  const lt = Number(agent.lastTool);
  if (!Number.isFinite(lt) || lt <= 0 || lt > nowS) return null;
  return nowS - lt > stuckMin * 60 ? nowS - lt : null;
}

// One agent -> a single mrkdwn line for the fleet roll-up.
function fleetLine(agent, { now, stuckMin } = {}) {
  const s = statusLabel(agent.waiting);
  const bits = [`${s.emoji} ${agent.name} (${agent.slot})`, s.text];
  const age = stateAgeSecs(agent, now);
  if (age != null) bits.push(fmtAgo(age));
  const stuck = isStuck(agent, now, stuckMin);
  if (stuck != null) bits.push(`:warning: stuck ${fmtAgo(stuck)}`);
  const repo = repoShort(agent.cwd);
  if (repo) bits.push(repo);
  return bits.join(' · ');
}

// The fleet roll-up. agents: [{name, slot, cwd, waiting, waitSince, lastTool}].
// Returns mrkdwn (a header with state counts, then one line per agent, slot-sorted).
export function formatFleetStatus(agents, opts = {}) {
  if (!agents || !agents.length) return '*nexus fleet* · _no active agents_';
  const counts = { active: 0, idle: 0, waiting: 0 };
  for (const a of agents) counts[statusLabel(a.waiting).key]++;
  const n = agents.length;
  const header = `*nexus fleet* · ${n} agent${n === 1 ? '' : 's'} · `
    + `:large_green_circle:${counts.active} :white_circle:${counts.idle} :large_yellow_circle:${counts.waiting}`;
  const lines = agents.slice()
    .sort((a, b) => (Number(a.slot) || 0) - (Number(b.slot) || 0))
    .map((a) => fleetLine(a, opts));
  return [header, ...lines].join('\n');
}

// Single-agent detail. `agent` may also carry `branch` (git branch of its cwd).
export function formatAgentStatus(agent, opts = {}) {
  if (!agent) return ':warning: no such agent';
  const s = statusLabel(agent.waiting);
  const age = stateAgeSecs(agent, opts.now);
  const lines = [`${s.emoji} *${agent.name}* (slot ${agent.slot}) · ${s.text}${age != null ? ` · ${fmtAgo(age)}` : ''}`];
  const repo = repoShort(agent.cwd);
  if (repo) lines.push(`repo: ${repo}${agent.branch ? ` @ ${agent.branch}` : ''}`);
  const nowS = Math.floor((opts.now ?? Date.now()) / 1000);
  const lt = Number(agent.lastTool);
  if (Number.isFinite(lt) && lt > 0 && lt <= nowS) {
    const stuck = isStuck(agent, opts.now, opts.stuckMin);
    lines.push(`last tool: ${fmtAgo(nowS - lt)} ago${stuck != null ? '  :warning: stuck' : ''}`);
  }
  return lines.join('\n');
}

// --------------------------------------------------------------------------
// Completion-ping state machine (pure). After the user messages an agent from
// Slack, index.js tracks it and calls this each poll to decide when to announce
// "finished — idle". Kept pure (no tmux/Date) so the tricky timing is testable.
//
// `entry` carries { at, sawWorking, idleSince } (plus app fields we pass through).
// `signal` is the live read: { waiting, worked } — `worked` = the agent has done
// something since being messaged (observed `@waiting=0`, or `@last_tool` advanced),
// computed by the caller so a quick task that never lands on a `0` poll still counts.
//
// Rules:
//   - drop once older than ttlMs (give up; never leak an entry).
//   - mark sawWorking permanently once `worked`.
//   - fire only after the agent has *worked* AND then stayed idle (`@waiting=2`)
//     for stableMs — the debounce stops auto-mode's between-turn 0→2→0 flicker
//     from pinging prematurely.
//   - permission ('1'), pre-work idle, working ('0'), or unknown → not finished;
//     leaving idle resets the stability timer.
export function advanceDone(entry, signal, now, { stableMs = 20000, ttlMs = 1800000 } = {}) {
  const e = { ...entry };
  if (now - e.at > ttlMs) return { action: 'drop', entry: e };
  const w = String(signal?.waiting ?? '').trim();
  if (signal?.worked) e.sawWorking = true;
  if (w === '2' && e.sawWorking) {
    if (e.idleSince == null) e.idleSince = now;
    if (now - e.idleSince >= stableMs) return { action: 'fire', entry: e };
    return { action: 'keep', entry: e };
  }
  if (w !== '2') e.idleSince = null;   // left idle (or never idle) → reset the timer
  return { action: 'keep', entry: e };
}
