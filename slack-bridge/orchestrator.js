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
