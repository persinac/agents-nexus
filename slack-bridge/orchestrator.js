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
import { execFile, execFileSync } from 'child_process';
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
  const substrate = process.env.HOME + "/.tmux/substrate.sh";
  return new Promise((resolve) => {
    execFile(substrate, ["spawn", name, cwd, command, "--print"],
      { timeout: timeoutMs, env: { ...process.env, TMUX_AGENT_SESSION: session } },
      (err, stdout, stderr) => {
        if (err) { resolve({ ok: false, error: (stderr || err.message || "substrate spawn failed").toString().trim() }); return; }
        try {
          const out = String(stdout).trim();
          if (out.startsWith("{")) {
            const parsed = JSON.parse(out);
            const pane = parsed.result?.agent_started?.pane_id || "";
            const slot = pane ? (pane.split(":")[0] || "") : "";
            resolve({ ok: true, pane, slot });
          } else {
            const [pane, slot] = out.split("\t");
            resolve({ ok: true, pane: pane || "", slot: slot || "" });
          }
        } catch (e) { resolve({ ok: false, error: "parse error" }); }
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

// Marks a bus message as a human-facing relay (an agent sharing its output into
// the channel for a person to read), NOT an addressed delivery. Same non-alnum
// leading char trick as presence, so handleBusMessage's `[host/]name:` parser
// never mistakes a relay whose body happens to start with `word:` (e.g. a pasted
// `TODO: fix …`) for a delivery to an agent named `word`. The bridge routes it
// out of the delivery path and never re-posts, so there is no loop.
export const RELAY_SENTINEL = '::nexus-relay::';

// Format a relay: sentinel + `from@host` attribution + the shared text, so a
// reader in Slack sees who/where it came from. Attribution is on one line and
// the body follows verbatim (multi-line ok — the sentinel prefix, not a newline,
// is what keeps it out of the addressed-delivery path).
export function formatRelay({ from, host, text }) {
  const who = `${String(from || 'unknown')}@${String(host || 'unknown')}`;
  return `${RELAY_SENTINEL} ↩ relay from ${who}:\n${String(text ?? '')}`;
}

// Parse a relay back to { from, text } if the text is a well-formed relay,
// else null. `from` is best-effort (the `who:` attribution line); `text` is
// everything after it. Used only to route relays out of the delivery path and
// (optionally) log them — the human reads the raw Slack message regardless.
export function parseRelay(text) {
  if (typeof text !== 'string') return null;
  const s = text.trim();
  if (!s.startsWith(RELAY_SENTINEL)) return null;
  const rest = s.slice(RELAY_SENTINEL.length).replace(/^\s*/, '');
  const m = rest.match(/^↩ relay from ([^\n:]+):\n?([\s\S]*)$/);
  if (m) return { from: m[1].trim(), text: m[2] };
  return { from: 'unknown', text: rest };
}

// Normalize a presence agent entry to an instance RECORD. Accepts a bare name
// string (v1 wire / legacy) → { name, workspace:'', pane:'' }, or an object
// (v2 wire) → its { name, workspace, pane }. This is the single shape every
// downstream function (owner election, collisions, reachability) operates on, so
// a mixed-version fleet folds into one representation.
export function toInstance(a) {
  if (a && typeof a === 'object') {
    return { name: String(a.name ?? ''), workspace: String(a.workspace ?? ''), pane: String(a.pane ?? '') };
  }
  return { name: String(a ?? ''), workspace: '', pane: '' };
}

const _lc = (s) => String(s == null ? '' : s).toLowerCase();
// Full instance key (workspace + name + pane) — used to de-dupe a snapshot; two
// same workspace/name instances stay distinct by pane (the intra-bucket duplicate).
function instanceKey(i) { return `${_lc(i.workspace)}\u0000${_lc(i.name)}\u0000${_lc(i.pane)}`; }
// Identity key (workspace + name, NO pane) — the addressable identity; two instances
// sharing it are a collision (resolvable only by pane). NUL-joined so a '/' inside a
// workspace label can't collide with the separator.
function identityKey(i) { return `${_lc(i.workspace)}\u0000${_lc(i.name)}`; }

// Format a presence announcement: sentinel + a compact JSON state snapshot.
// Full-state (not deltas) so a missed message self-heals on the next beat.
// `fqdn` on → v2: agents are { name, workspace, pane } records, so a same-named
// instance is distinct and cross-host addressable. Off → v1: bare name strings
// (unchanged wire) for back-compat with pre-FQDN bridges on the same channel.
export function formatPresence({ host, agents, ts }, { fqdn = false } = {}) {
  const insts = Array.from(agents || []).map(toInstance);
  const names = insts.map((i) => i.name);
  // CRITICAL back-compat: v2 keeps `agents` as bare NAMES (a v1 bridge does
  // `obj.agents.map(String)` — objects there would become "[object Object]") and carries
  // the rich per-instance identity in a SEPARATE `instances` field that v1 ignores. So the
  // wire is back-compatible in BOTH directions. v1 output is byte-for-byte unchanged.
  const payload = fqdn
    ? { v: 2, host: String(host), agents: names, instances: insts.map((i) => ({ name: i.name, workspace: i.workspace, pane: i.pane })), ts: ts || 0 }
    : { v: 1, host: String(host), agents: names, ts: ts || 0 };
  return `${PRESENCE_SENTINEL} ${JSON.stringify(payload)}`;
}

// Parse a presence announcement back to { v, host, agents:instance[], ts }, or
// null if malformed. Prefers the rich v2 `instances` field (per-instance records);
// falls back to the bare-name `agents` field (v1, and the v2 back-compat mirror).
// Also tolerates a legacy v2 line whose `agents` were records. Everything normalizes
// to instance records via toInstance, so a mixed-version fleet interoperates.
export function parsePresence(text) {
  if (typeof text !== 'string') return null;
  const s = text.trim();
  if (!s.startsWith(PRESENCE_SENTINEL)) return null;
  try {
    const obj = JSON.parse(s.slice(PRESENCE_SENTINEL.length).trim());
    if (!obj || typeof obj.host !== 'string') return null;
    const raw = Array.isArray(obj.instances) ? obj.instances
      : Array.isArray(obj.agents) ? obj.agents : null;
    if (!raw) return null;
    return { v: Number(obj.v) || 1, host: obj.host, agents: raw.map(toInstance), ts: Number(obj.ts) || 0 };
  } catch { return null; }
}

// Apply a snapshot to the map (host -> { agents:instance[], ts, seen }). Ignores
// an out-of-order snapshot (older sender ts than the one we hold for that host).
// An empty agent set still records the host (a host that drained to zero agents
// is known-empty, not left phantom-owning a stale name). Stores instance RECORDS
// (not a name Set) so two same-named agents on one host stay distinct; de-dupes
// by full instance key so a doubled snapshot entry can't inflate the fleet.
// Mutates + returns map.
export function applyPresence(map, snap, { now = 0 } = {}) {
  if (!snap || !snap.host) return map;
  const prev = map.get(snap.host);
  if (prev && snap.ts && prev.ts && snap.ts < prev.ts) return map;
  const seen = new Set();
  const agents = [];
  for (const raw of (snap.agents || [])) {
    const i = toInstance(raw);
    if (!i.name) continue;
    const k = instanceKey(i);
    if (seen.has(k)) continue;
    seen.add(k);
    agents.push(i);
  }
  map.set(snap.host, { agents, ts: snap.ts || now, seen: now || snap.ts || 0 });
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
    for (const a of rec.agents) { if (String(a.name).toLowerCase() === lower) { hosts.push(host); break; } }
  }
  return hosts.sort();
}

// The single deterministic owner of `name` (lexically-smallest claiming host),
// or null if no host in the map claims it. Every bridge agrees → one delivers.
export function ownerOf(map, name) {
  const hosts = ownersOf(map, name);
  return hosts.length ? hosts[0] : null;
}

// Instance-identity collisions: a (workspace, name) identity claimed by more than
// one live instance — whether across hosts (owner election must pick one) or twice
// on ONE host (the intra-bucket duplicate, resolvable only by pane). Two SAME-named
// agents in DIFFERENT workspaces are NOT a collision — they are distinct instances.
// With no workspace set (v1 wire, FQDN off) this reduces to same-name grouping, so
// cross-host name clashes are surfaced exactly as before. Returns
// { name, workspace, hosts:[...], count } per colliding identity.
export function presenceCollisions(map) {
  const byId = new Map(); // identityKey -> { name, workspace, hosts:Set, count }
  for (const [host, rec] of map) {
    for (const a of rec.agents) {
      const k = identityKey(a);
      if (!byId.has(k)) byId.set(k, { name: a.name, workspace: a.workspace || '', hosts: new Set(), count: 0 });
      const g = byId.get(k);
      g.hosts.add(host);
      g.count += 1;
    }
  }
  const out = [];
  for (const g of byId.values()) {
    if (g.count > 1) out.push({ name: g.name, workspace: g.workspace, hosts: Array.from(g.hosts).sort(), count: g.count });
  }
  return out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : (a.workspace < b.workspace ? -1 : a.workspace > b.workspace ? 1 : 0)));
}

// Reachability projection: one row PER INSTANCE (not per name-per-host), each with
// its { name, workspace, pane, host }, the resolved single `owner` (host-level, by
// name), and a `collided` flag (its workspace/name identity is shared by >1 live
// instance). Two same-named instances on one host therefore appear as two distinct,
// individually-addressable rows. Sorted by name, then host, then workspace.
export function reachability(map) {
  const collided = new Set(presenceCollisions(map).map((c) => identityKey(c)));
  const rows = [];
  for (const [host, rec] of map) {
    for (const a of rec.agents) {
      rows.push({ name: a.name, workspace: a.workspace || '', pane: a.pane || '', host, owner: ownerOf(map, a.name), collided: collided.has(identityKey(a)) });
    }
  }
  return rows.sort((x, y) => (x.name < y.name ? -1 : x.name > y.name ? 1 : (x.host < y.host ? -1 : x.host > y.host ? 1 : (x.workspace < y.workspace ? -1 : x.workspace > y.workspace ? 1 : 0))));
}

// --------------------------------------------------------------------------
// Agent address grammar (shared contract with agent-resolve.sh's nx_parse_addr).
// Parse an address TOKEN (the part before ':') right-to-left:
//   - the LAST segment is always the agent name;
//   - the FIRST segment is a host ONLY if it's a known host (SELF_HOST or a
//     presence-announced host) — otherwise the whole prefix is the workspace
//     label, which may itself contain '/' (e.g. `mission/spark-reclaim`).
// Backward-compatible with the legacy `host/name` cross-PC scheme: a real host
// is always known, so `mac/general` still parses as host=mac; single-PC users
// never type a host, so `search/example-service` parses as workspace=search.
export function parseAddress(target, opts = {}) {
  const { knownHosts, selfHost } = opts;
  const t = String(target == null ? '' : target).trim();
  if (!t.includes('/')) return { host: '', workspace: '', name: t };
  const segs = t.split('/');
  const name = segs[segs.length - 1];
  const prefix = segs.slice(0, -1);
  const lc = (s) => String(s).toLowerCase();
  const known = (h) => {
    if (!h) return false;
    if (selfHost && lc(h) === lc(selfHost)) return true;
    if (knownHosts) for (const kh of knownHosts) if (lc(kh) === lc(h)) return true;
    return false;
  };
  if (known(prefix[0])) return { host: prefix[0], workspace: prefix.slice(1).join('/'), name };
  return { host: '', workspace: prefix.join('/'), name };
}

// Split an addressed channel line "<token>: <body>" into { token, body } (or null if the
// line is not addressed). The delimiter is colon-SPACE (": "): every /send line emits it
// (`${to}: ↩ from …`), while a herdr pane handle's internal colon is colon-NON-space
// (`wQ:pF`). So we admit ':' in the token, match it NON-greedily, and require whitespace
// AFTER the delimiter colon — which lets a pane handle be a first-class bus address without
// any new grammar. `token` may be a name, a [host/][workspace/]name (parseAddress splits
// that), a pane handle `wN:pN`, or a bare slot number. The leading `[A-Za-z0-9]` still
// rejects the ':'-prefixed presence/relay sentinels, so those never look addressed.
//
// Known, accepted regression: a line with NO space after the colon (`general:hi`) is no
// longer treated as addressed. /send never emits that (it always writes ": "); it can only
// come from a human hand-typing, where a trailing space is the natural form.
export function parseAddressedLine(text) {
  const m = String(text == null ? '' : text).match(/^([A-Za-z0-9][\w:./-]*?)\s*:\s+([\s\S]+)$/);
  if (!m) return null;
  return { token: m[1], body: m[2].trim() };
}

// Does a registry WORKSPACE= value satisfy an address's workspace token?
// Two-tier: exact full-label match, else slug (leaf after last '/') match.
// Empty `want` = "no workspace filter" → always matches.
export function workspaceMatches(entryWs, want) {
  if (!want) return true;
  const ew = String(entryWs == null ? '' : entryWs);
  const w = String(want);
  if (ew === w) return true;
  const slug = (s) => (s.includes('/') ? s.slice(s.lastIndexOf('/') + 1) : s);
  return slug(ew) === slug(w);
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
// ("/home/u/repos/example-repo/infra" -> "example-repo/infra").
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

// Cap a string to `max` chars, appending a VISIBLE marker when it overflows — so an
// over-long bus / notify message is never *silently* truncated (the dangerous failure
// mode: the reader loses the tail without knowing). Returns the input unchanged when
// it fits. Slack's text field allows ~40k, so `max` is a sanity bound, not a hard limit.
export function capWithMarker(str, max) {
  const s = String(str ?? '');
  if (s.length <= max) return s;
  return `${s.slice(0, max)} …[truncated ${s.length - max} chars]`;
}
