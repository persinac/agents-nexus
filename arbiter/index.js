/**
 * Pixel Dashboard Bridge Server
 *
 * Polls tmux state and JSONL transcripts, sends events to the
 * pixel-agents webview UI over WebSocket.
 *
 * Events emitted match the pixel-agents postMessage protocol:
 *   agentCreated, agentClosed, agentStatus, agentToolStart, agentToolDone, agentToolsClear
 */

import http from 'http';
import { fileURLToPath } from 'url';
import { dirname, join, basename, resolve } from 'path';
import { WebSocketServer } from 'ws';
import { execSync, execFileSync } from 'child_process';
import { readFileSync, existsSync, readdirSync, statSync, watch } from 'fs';
import { homedir } from 'os';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Load .env from project root so DATABASE_URL is available for memory stats
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

const MEMORY_RECALL_SCRIPT = resolve(__dirname, '../tmux/mac/tmux-scripts/memory-recall.py');
const MEMORY_STATS_SCRIPT = resolve(__dirname, '../tmux/mac/tmux-scripts/memory-stats.py');
const MEMORY_SEARCH_SCRIPT = resolve(__dirname, '../tmux/mac/tmux-scripts/memory-search.py');

const PORT = parseInt(process.env.PORT || '8420', 10);
const POLL_MS = 100;  // fast baseline poll
const TMUX_SESSION = process.env.TMUX_SESSION || 'agents';

// Platform detection
const isWindows = process.platform === 'win32';
const TMUX_BIN = isWindows ? 'C:\\msys64\\usr\\bin\\tmux.exe' : 'tmux';

// Claude transcript directory
const HOME = homedir();
const CLAUDE_PROJECTS_DIR = join(HOME, '.claude', 'projects');

// Command center paths
const REGISTRY_DIR = join(HOME, '.tmux', 'registry');
const CACHE_DIR = join(HOME, '.tmux', 'cache');
const CHECKPOINT_DIR = process.env.CHECKPOINT_DIR || join(HOME, 'vault', 'Checkpoints');

// Prefer the agent-memory venv python (has psycopg) over system python3
const AGENTS_NEXUS_DIR = process.env.AGENTS_NEXUS_DIR || resolve(__dirname, '..');
const AGENT_MEM_VENV = join(AGENTS_NEXUS_DIR, 'mnemon', '.venv');
const MEMORY_PYTHON = (() => {
  for (const p of [
    join(AGENT_MEM_VENV, 'bin', 'python3'),
    join(AGENT_MEM_VENV, 'Scripts', 'python3.exe'),
    join(AGENT_MEM_VENV, 'Scripts', 'python.exe'),
  ]) {
    if (existsSync(p)) return p;
  }
  return 'python3';
})();

// ── State ────────────────────────────────────────────────────────

/** @type {Map<number, {name: string, waiting: string, waitType: string, path: string, lastTool?: {toolId: string, status: string, toolName?: string, input?: object}}>} */
const agents = new Map();

/** @type {Map<string, {offset: number, activeTools: Set<string>}>} */
const transcriptState = new Map();

/** @type {Set<import('ws').WebSocket>} */
const clients = new Set();

// Message types that external bridge clients may send to be relayed to all other
// connected clients. Bridges connect as normal WebSocket clients and push these to
// feed agents from non-tmux runtimes (remote runners, cloud jobs, etc.).
const RELAY_TYPES = new Set([
  'agentCreated', 'agentClosed', 'agentStatus',
  'agentToolStart', 'agentToolDone', 'agentToolsClear',
  'agentToolPermission', 'agentToolPermissionClear', 'agentMessage',
]);

/** @type {Map<number, {name: string, status: string, agentSource?: string, lastTool?: {toolId: string, status: string}}>} */
const relayAgents = new Map();

// Tracks which relay agent IDs were published by each WS connection so we can
// evict stale entries when the relay client disconnects.
/** @type {WeakMap<import('ws').WebSocket, Set<number|string>>} */
const clientOwnedRelayIds = new WeakMap();

let nextToolId = 1;

// ── tmux polling ─────────────────────────────────────────────────

function tmuxListWindows() {
  try {
    const out = execSync(
      `"${TMUX_BIN}" list-windows -t "${TMUX_SESSION}" -F "#{window_index}|#{window_name}|#{@waiting}|#{pane_current_path}|#{pane_current_command}|#{@wait_type}"`,
      { encoding: 'utf8', timeout: 2000 }
    ).trim();
    return out.split('\n').filter(Boolean).map(line => {
      const [index, name, waiting, path, command, waitType] = line.split('|');
      return { index: parseInt(index, 10), name, waiting, path, command, waitType: waitType || '' };
    });
  } catch {
    return [];
  }
}

function isClaude(command) {
  if (!command) return false;
  // Match 'claude' or versioned binaries like '2.1.88' (claude symlinks to its version)
  return command.toLowerCase().includes('claude') || /^\d+\.\d+\.\d+$/.test(command);
}

function waitingToStatus(waiting) {
  if (waiting === '1') return 'permission';  // needs input → permission bubble
  if (waiting === '2') return 'waiting';     // idle/done → waiting bubble
  return 'active';                            // 0 or unset → active
}

// ── Broadcast ────────────────────────────────────────────────────

function broadcast(msg) {
  const data = JSON.stringify(msg);
  for (const ws of clients) {
    if (ws.readyState === 1) ws.send(data);
  }
}

function broadcastStatus(id, status) {
  if (status === 'permission') {
    // Pixel-agents UI expects agentToolPermission for the amber "..." bubble
    broadcast({ type: 'agentToolPermission', id });
    broadcast({ type: 'agentStatus', id, status: 'active' });
  } else if (status === 'active') {
    // Clear permission bubble when agent resumes
    broadcast({ type: 'agentToolPermissionClear', id });
    broadcast({ type: 'agentStatus', id, status: 'active' });
  } else {
    // 'waiting' = idle/done → character goes idle, stops typing
    broadcast({ type: 'agentToolsClear', id });
    broadcast({ type: 'agentStatus', id, status });
  }
}

// ── JSONL transcript reading ─────────────────────────────────────

function findTranscriptFile(agentPath) {
  // Claude transcripts live at ~/.claude/projects/<hash>/<session>.jsonl
  // The hash is derived from the project path
  if (!existsSync(CLAUDE_PROJECTS_DIR)) return null;

  // Normalize the path to create the hash Claude uses
  const normalized = agentPath
    .replace(/\\/g, '-')
    .replace(/\//g, '-')
    .replace(/:/g, '-');

  // Look for a matching project directory
  try {
    const dirs = readdirSync(CLAUDE_PROJECTS_DIR);
    for (const dir of dirs) {
      // Check if this dir matches our agent's path pattern
      if (normalized.includes(dir) || dir.includes(normalized.slice(-30))) {
        const projectDir = join(CLAUDE_PROJECTS_DIR, dir);
        const stat = statSync(projectDir);
        if (!stat.isDirectory()) continue;

        // Find the most recent .jsonl file
        const files = readdirSync(projectDir)
          .filter(f => f.endsWith('.jsonl'))
          .map(f => ({
            name: f,
            mtime: statSync(join(projectDir, f)).mtimeMs
          }))
          .sort((a, b) => b.mtime - a.mtime);

        if (files.length > 0) {
          return join(projectDir, files[0].name);
        }
      }
    }
  } catch {
    // ignore
  }
  return null;
}

function readNewLines(filePath) {
  if (!existsSync(filePath)) return [];

  let state = transcriptState.get(filePath);
  if (!state) {
    state = { offset: 0, activeTools: new Set() };
    transcriptState.set(filePath, state);
  }

  try {
    const content = readFileSync(filePath, 'utf8');
    if (content.length <= state.offset) return [];

    const newContent = content.slice(state.offset);
    state.offset = content.length;

    const lines = newContent.split('\n').filter(Boolean);
    const records = [];
    for (const line of lines) {
      try {
        records.push(JSON.parse(line));
      } catch {
        // partial line, skip
      }
    }
    return records;
  } catch {
    return [];
  }
}

function processTranscriptRecords(agentId, records) {
  for (const record of records) {
    if (record.type === 'assistant' && record.message?.content) {
      const content = Array.isArray(record.message.content)
        ? record.message.content
        : [];

      for (const block of content) {
        if (block.type === 'tool_use') {
          const toolId = block.id || `tool_${nextToolId++}`;
          const toolName = block.name || 'unknown';
          const input = block.input || {};

          // Build status string
          let status = toolName;
          if (toolName.startsWith('mcp__agent-memory__')) {
            const op = toolName.slice('mcp__agent-memory__'.length);
            const memLabels = {
              create_note:    'Memory: saving note',
              query_notes:    'Memory: searching notes',
              search_similar: 'Memory: semantic search',
              query_entity:   'Memory: looking up entity',
              query_session:  'Memory: reading session',
              recent_events:  'Memory: recent events',
              log_event:      'Memory: logging event',
            };
            status = memLabels[op] || `Memory: ${op}`;
          } else if (toolName === 'Read' && input.file_path) {
            status = `Reading ${basename(input.file_path)}`;
          } else if (toolName === 'Write' && input.file_path) {
            status = `Writing ${basename(input.file_path)}`;
          } else if (toolName === 'Edit' && input.file_path) {
            status = `Editing ${basename(input.file_path)}`;
          } else if (toolName === 'Bash' && input.command) {
            // Detect agent-to-agent messaging
            if (input.command.includes('agent-send.sh')) {
              const match = input.command.match(/agent-send\.sh\s+(\d+)\s+(.*)/);
              if (match) {
                const targetSlot = parseInt(match[1], 10);
                const message = match[2].slice(0, 50);
                status = `Messaging agent ${targetSlot}`;
                broadcast({
                  type: 'agentMessage',
                  fromId: agentId,
                  toId: targetSlot,
                  message
                });
              }
            } else {
              status = `Running: ${input.command.slice(0, 30)}`;
            }
          } else if (toolName === 'Grep' && input.pattern) {
            status = `Searching: ${input.pattern.slice(0, 30)}`;
          } else if (toolName === 'Glob' && input.pattern) {
            status = `Finding: ${input.pattern.slice(0, 30)}`;
          }

          broadcast({ type: 'agentToolStart', id: agentId, toolId, status });

          // Track last tool so new clients can catch up
          const agent = agents.get(agentId);
          if (agent) agent.lastTool = { toolId, status, toolName, input };

          const state = transcriptState.get(`agent_${agentId}`);
          if (state) state.activeTools.add(toolId);
        }
      }
    }

    if (record.type === 'user' && record.message?.content) {
      const content = Array.isArray(record.message.content)
        ? record.message.content
        : [];

      for (const block of content) {
        if (block.type === 'tool_result' && block.tool_use_id) {
          broadcast({ type: 'agentToolDone', id: agentId, toolId: block.tool_use_id });
        }
      }
    }
  }
}

// ── Main poll loop ───────────────────────────────────────────────

function poll() {
  const windows = tmuxListWindows();
  const currentIds = new Set();

  for (const win of windows) {
    if (!isClaude(win.command)) continue;

    const id = win.index;
    currentIds.add(id);

    const existing = agents.get(id);
    const status = waitingToStatus(win.waiting);

    if (!existing) {
      // New agent
      agents.set(id, { name: win.name, waiting: win.waiting, waitType: win.waitType, path: win.path });
      console.log(`[Poll] New agent ${id} (${win.name}) waiting="${win.waiting}" → ${status}`);
      broadcast({ type: 'agentCreated', id, folderName: win.name });
      broadcastStatus(id, status);
    } else {
      // Status changed?
      if (existing.waiting !== win.waiting) {
        console.log(`[Poll] Agent ${id} (${win.name}) waiting="${existing.waiting}" → "${win.waiting}" (${status}) type=${win.waitType}`);
        existing.waiting = win.waiting;
        existing.waitType = win.waitType;
        broadcastStatus(id, status);
      }

      // Name changed?
      if (existing.name !== win.name) {
        existing.name = win.name;
      }
    }

    // Read JSONL transcripts for tool-level events
    const transcriptFile = findTranscriptFile(win.path);
    if (transcriptFile) {
      const records = readNewLines(transcriptFile);
      if (records.length > 0) {
        processTranscriptRecords(id, records);
      }
    }
  }

  // Remove closed agents
  for (const [id] of agents) {
    if (!currentIds.has(id)) {
      agents.delete(id);
      broadcast({ type: 'agentClosed', id });
    }
  }
}

// ── Timers / scheduled jobs ──────────────────────────────────────
// On macOS we read launchd plists from ~/Library/LaunchAgents and surface
// every job whose label starts with com.agents-nexus. — i.e. the jobs
// installed via `task launchd:install:*`. On Linux we fall through to the
// existing systemctl --user list-timers path further down.

const LAUNCHD_LABEL_RE = /^com\.agents-nexus\.[a-zA-Z0-9._-]+$/;
const DESCRIPTIONS_PATH = resolve(__dirname, '..', 'launchd', 'descriptions.json');

function _loadDescriptions() {
  try {
    return JSON.parse(readFileSync(DESCRIPTIONS_PATH, 'utf8'));
  } catch {
    return {};
  }
}

function _humanizeDelta(ms) {
  if (!ms || ms < 0) return '';
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  if (m && !d) parts.push(`${m}m`);
  return 'in ' + (parts.join(' ') || '<1m');
}

function _computeNextLaunchdRun(plistData) {
  const sci = plistData.StartCalendarInterval;
  if (!sci) return null;
  const entries = Array.isArray(sci) ? sci : [sci];
  const now = new Date();
  let soonest = null;
  for (const entry of entries) {
    // Search up to 8 days ahead so weekday-pinned schedules always resolve.
    for (let offset = 0; offset < 8; offset++) {
      const c = new Date(now);
      c.setDate(now.getDate() + offset);
      if (entry.Hour !== undefined) c.setHours(entry.Hour);
      if (entry.Minute !== undefined) c.setMinutes(entry.Minute);
      c.setSeconds(0, 0);
      if (entry.Weekday !== undefined && c.getDay() !== entry.Weekday) continue;
      if (entry.Day !== undefined && c.getDate() !== entry.Day) continue;
      if (entry.Month !== undefined && c.getMonth() !== entry.Month - 1) continue;
      if (c <= now) continue;
      if (!soonest || c < soonest) soonest = c;
      break;
    }
  }
  return soonest;
}

function _launchctlStatus(label) {
  try {
    const out = execSync(`launchctl list ${label} 2>/dev/null`, { encoding: 'utf8', timeout: 2000 });
    const exit = out.match(/"LastExitStatus"\s*=\s*(\d+)/);
    const pid = out.match(/"PID"\s*=\s*(\d+)/);
    return {
      exit: exit ? parseInt(exit[1], 10) : null,
      pid: pid ? parseInt(pid[1], 10) : null,
    };
  } catch {
    return { exit: null, pid: null };
  }
}

function _loadPlistJson(path) {
  try {
    const json = execSync(`plutil -convert json -o - "${path}"`, { encoding: 'utf8', timeout: 2000 });
    return JSON.parse(json);
  } catch { return null; }
}

function listMacosTimers() {
  const launchAgentsDir = join(HOME, 'Library', 'LaunchAgents');
  if (!existsSync(launchAgentsDir)) return [];
  const descriptions = _loadDescriptions();
  const timers = [];
  for (const fname of readdirSync(launchAgentsDir)) {
    if (!fname.startsWith('com.agents-nexus.') || !fname.endsWith('.plist')) continue;
    const path = join(launchAgentsDir, fname);
    const data = _loadPlistJson(path);
    if (!data) continue;
    const label = data.Label || fname.replace(/\.plist$/, '');
    const programArgs = data.ProgramArguments || [];
    const description = descriptions[label]
      || (programArgs.length ? programArgs[programArgs.length - 1].split('/').pop() : null);
    const status = _launchctlStatus(label);
    let result = 'unknown';
    if (status.exit === 0) result = 'success';
    else if (status.exit !== null && status.exit !== 0) result = `exit-code ${status.exit}`;
    let nextRun = '';
    let leftUntil = '';
    const nextDate = _computeNextLaunchdRun(data);
    if (nextDate) {
      nextRun = nextDate.toLocaleString('en-US', {
        weekday: 'short', month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit',
      });
      leftUntil = _humanizeDelta(nextDate.getTime() - Date.now());
    } else if (data.StartInterval) {
      const sec = data.StartInterval;
      nextRun = sec >= 60 ? `every ${Math.round(sec / 60)}m` : `every ${sec}s`;
      leftUntil = nextRun;
    }
    // Best-effort lastRun: StandardOutPath mtime is touched on every fire
    let lastRun = null;
    if (data.StandardOutPath) {
      try {
        const st = statSync(data.StandardOutPath);
        lastRun = st.mtime.toISOString();
      } catch {}
    }
    timers.push({
      name: label, nextRun, leftUntil, lastRun, result,
      active: status.pid !== null || status.exit !== null,
      description,
    });
  }
  // Sort by upcoming-ness: anything with a leftUntil first, then by label
  timers.sort((a, b) => {
    if (a.leftUntil && !b.leftUntil) return -1;
    if (!a.leftUntil && b.leftUntil) return 1;
    return a.name.localeCompare(b.name);
  });
  return timers;
}

// ── HTTP + WebSocket server ──────────────────────────────────────

const httpServer = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost`);

  if (url.pathname === '/api/memory/stats') {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Access-Control-Allow-Origin', '*');
    try {
      const out = execSync(
        `"${MEMORY_PYTHON}" "${MEMORY_STATS_SCRIPT}"`,
        { encoding: 'utf8', timeout: 10000 }
      ).trim();
      res.end(out || '{}');
    } catch {
      res.end('{"error":"stats unavailable"}');
    }
    return;
  }

  if (url.pathname === '/api/memory') {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Access-Control-Allow-Origin', '*');
    const project = url.searchParams.get('project') || 'general';
    try {
      const out = execSync(
        `"${MEMORY_PYTHON}" "${MEMORY_RECALL_SCRIPT}" "${project}" --format json`,
        { encoding: 'utf8', timeout: 10000 }
      ).trim();
      res.end(out || '[]');
    } catch {
      res.end('[]');
    }
    return;
  }

  if (url.pathname === '/api/system/memory/search') {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Access-Control-Allow-Origin', '*');
    const query = (url.searchParams.get('query') || '').trim();
    const mode = url.searchParams.get('mode') === 'keyword' ? 'keyword' : 'semantic';
    const project = url.searchParams.get('project') || 'all';
    let limit = parseInt(url.searchParams.get('limit') || '10', 10);
    if (!Number.isFinite(limit) || limit < 1) limit = 10;
    if (limit > 50) limit = 50;
    if (!query) { res.end('[]'); return; }
    try {
      // execFileSync with an args array — no shell, so the free-text query
      // cannot be interpreted as a shell command.
      let out;
      if (mode === 'keyword') {
        out = execFileSync(
          MEMORY_PYTHON,
          [MEMORY_SEARCH_SCRIPT, '--query', query, '--project', project, '--limit', String(limit), '--format', 'json'],
          { encoding: 'utf8', timeout: 10000 },
        ).trim();
      } else {
        out = execFileSync(
          'docker',
          ['exec', 'nexus-mnemon-mcp', 'uv', 'run', 'python', '-m', 'agent_memory.cli',
           'search', '--project', project, '--query', query, '--limit', String(limit), '--json'],
          { encoding: 'utf8', timeout: 20000 },
        ).trim();
      }
      res.end(out || '[]');
    } catch {
      res.end('[]');
    }
    return;
  }

  // ── Command Center API ──────────────────────────────────────────

  const corsJson = () => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Access-Control-Allow-Origin', '*');
  };

  // CORS preflight for POST endpoints
  if (req.method === 'OPTIONS') {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    res.writeHead(204);
    res.end();
    return;
  }

  // POST /api/agents/:id/respond — approve or deny a permission prompt
  const respondMatch = url.pathname.match(/^\/api\/agents\/(\d+)\/respond$/);
  if (respondMatch && req.method === 'POST') {
    corsJson();
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        const { action } = JSON.parse(body);
        const agentId = parseInt(respondMatch[1], 10);
        const agent = agents.get(agentId);
        if (!agent) {
          res.end(JSON.stringify({ ok: false, error: 'agent not found' }));
          return;
        }
        const wType = agent.waitType || 'permission_prompt';
        console.log(`[Respond] Agent ${agentId} (${agent.name}): action=${action}, waiting=${agent.waiting}, type=${wType}`);
        if (action === 'approve') {
          if (wType === 'elicitation_dialog') {
            // AskUserQuestion / edit acceptance — confirm with Enter
            execSync(`"${TMUX_BIN}" send-keys -t "${TMUX_SESSION}:${agentId}" Enter`, { timeout: 3000 });
          } else {
            // Permission prompt — approve with 'y'
            execSync(`"${TMUX_BIN}" send-keys -t "${TMUX_SESSION}:${agentId}" -l y`, { timeout: 3000 });
          }
        } else if (action === 'deny') {
          execSync(`"${TMUX_BIN}" send-keys -t "${TMUX_SESSION}:${agentId}" Escape`, { timeout: 3000 });
        } else {
          res.end(JSON.stringify({ ok: false, error: 'action must be approve or deny' }));
          return;
        }
        res.end(JSON.stringify({ ok: true, action, agentId }));
      } catch (err) {
        res.end(JSON.stringify({ ok: false, error: err.message }));
      }
    });
    return;
  }

  if (url.pathname === '/api/system/health') {
    corsJson();
    try {
      let docker = false, containerCount = 0;
      try {
        const out = execSync('docker info --format "{{.ContainersRunning}}"', { encoding: 'utf8', timeout: 5000 }).trim();
        docker = true;
        containerCount = parseInt(out, 10) || 0;
      } catch {}

      let tmux = false;
      try { execSync(`${TMUX_BIN} has-session -t ${TMUX_SESSION} 2>/dev/null`, { timeout: 3000 }); tmux = true; } catch {}

      let database = false;
      try {
        const stats = execSync(`"${MEMORY_PYTHON}" "${MEMORY_STATS_SCRIPT}"`, { encoding: 'utf8', timeout: 10000 }).trim();
        const parsed = JSON.parse(stats || '{}');
        database = !parsed.error;
      } catch {}

      let timerCount = 0;
      if (process.platform === 'darwin') {
        try {
          const dir = join(HOME, 'Library', 'LaunchAgents');
          if (existsSync(dir)) {
            timerCount = readdirSync(dir).filter(f =>
              f.startsWith('com.agents-nexus.') && f.endsWith('.plist')
            ).length;
          }
        } catch {}
      } else {
        try {
          const out = execSync('systemctl --user list-timers --no-pager --no-legend 2>/dev/null', { encoding: 'utf8', timeout: 5000 });
          timerCount = out.trim().split('\n').filter(l => l.trim()).length;
        } catch {}
      }

      res.end(JSON.stringify({
        arbiter: true, docker, tmux, database,
        agentCount: agents.size, containerCount, timerCount,
      }));
    } catch {
      res.end('{"arbiter":true,"error":"health check failed"}');
    }
    return;
  }

  if (url.pathname === '/api/system/agents') {
    corsJson();
    const result = [];
    for (const [id, agent] of agents) {
      let uptime = null;
      try {
        const files = existsSync(REGISTRY_DIR) ? readdirSync(REGISTRY_DIR) : [];
        for (const f of files) {
          const content = readFileSync(join(REGISTRY_DIR, f), 'utf8');
          const slotMatch = content.match(/^SLOT=(\d+)/m);
          if (slotMatch && parseInt(slotMatch[1], 10) === id) {
            const atMatch = content.match(/^AT=(\d+)/m);
            if (atMatch) uptime = Math.floor(Date.now() / 1000) - parseInt(atMatch[1], 10);
            break;
          }
        }
      } catch {}
      const lt = agent.lastTool || null;
      let toolDetail = null;
      if (lt && agent.waiting === '1') {
        toolDetail = { status: lt.status, toolName: lt.toolName || null };
        if (lt.input) {
          const inp = lt.input;
          if (inp.file_path) toolDetail.file = inp.file_path;
          if (inp.command) toolDetail.command = inp.command;
          if (inp.pattern) toolDetail.pattern = inp.pattern;
          if (inp.description) toolDetail.description = inp.description;
        }
      }
      result.push({
        id, name: agent.name, status: waitingToStatus(agent.waiting),
        cwd: agent.path, uptime,
        lastTool: lt ? { toolId: lt.toolId, status: lt.status } : null,
        pendingTool: toolDetail,
        waitType: agent.waiting === '1' ? (agent.waitType || 'permission_prompt') : null,
      });
    }
    res.end(JSON.stringify(result));
    return;
  }

  if (url.pathname === '/api/system/services') {
    corsJson();
    try {
      const out = execSync('docker ps -a --format "{{json .}}"', { encoding: 'utf8', timeout: 10000 });
      const containers = out.trim().split('\n').filter(l => l.trim()).map(line => {
        const c = JSON.parse(line);
        return {
          name: c.Names, status: c.State || c.Status,
          health: (c.Status || '').match(/\((healthy|unhealthy)\)/)?.[1] || 'none',
          uptime: c.Status, ports: c.Ports || '', image: c.Image,
        };
      });
      res.end(JSON.stringify(containers));
    } catch {
      res.end('[]');
    }
    return;
  }

  if (url.pathname === '/api/system/installations') {
    corsJson();
    try {
      // Spark's installations.json lives in the spark-index Docker volume,
      // unreadable from the host — but reachable via docker exec (same pattern
      // as /api/system/services shelling out to docker).
      const out = execSync(
        'docker exec nexus-spark cat /app/data/the-index/installations.json',
        { encoding: 'utf8', timeout: 10000, maxBuffer: 4 * 1024 * 1024 },
      );
      const meta = JSON.parse(out);
      const now = Date.now();
      const rows = Object.entries(meta).map(([relPath, rec]) => {
        const indexedAt = rec.indexed_at || '';
        const t = indexedAt ? Date.parse(indexedAt) : NaN;
        const ageSeconds = Number.isNaN(t) ? null : Math.floor((now - t) / 1000);
        return {
          relPath,
          name: relPath.split('/').pop(),
          indexedAt,
          lastRemoteTs: rec.last_remote_ts || 0,
          ageSeconds,
        };
      });
      res.end(JSON.stringify(rows));
    } catch {
      res.end('[]');
    }
    return;
  }

  if (url.pathname === '/api/system/spark-index') {
    corsJson();
    const PY = "import json,lancedb\n" +
      "from spark.config import SparkConfig\n" +
      "c=SparkConfig.load()\n" +
      "o={'embedder':c.embedder}\n" +
      "o['model']='BAAI/bge-small-en-v1.5' if c.embedder=='fastembed' else c.embedding_model\n" +
      "try:\n" +
      "    t=lancedb.connect(str(c.index_path)).open_table('the_index')\n" +
      "    vf=[f for f in t.schema if f.name=='vector'][0]\n" +
      "    o['dim']=getattr(vf.type,'list_size',None)\n" +
      "    o['chunks']=t.count_rows()\n" +
      "except Exception as e:\n" +
      "    o['error']=str(e)\n" +
      "print(json.dumps(o))";
    try {
      const out = execFileSync(
        'docker',
        ['exec', 'nexus-spark', 'uv', 'run', 'python', '-c', PY],
        { encoding: 'utf8', timeout: 20000, maxBuffer: 1024 * 1024 },
      );
      // uv may emit noise on stderr; stdout's last non-empty line is our JSON.
      const line = out.trim().split('\n').filter((l) => l.trim().startsWith('{')).pop() || '{}';
      res.end(line);
    } catch {
      res.end('{}');
    }
    return;
  }

  if (url.pathname === '/api/system/cost') {
    corsJson();
    // Durable daily cost rollup (agents.langfuse_cost_daily), populated by
    // scripts/langfuse-cost-snapshot.py before Langfuse's 10-day trace TTL
    // prunes the source. Reached via docker exec, same pattern as installations.
    // SQL is piped on stdin (-f -) so its quotes need no shell escaping.
    const sql =
      "SELECT coalesce(json_agg(row_to_json(c) ORDER BY c.day DESC, c.total_cost DESC), '[]') FROM (" +
      "SELECT to_char(day, 'YYYY-MM-DD') AS day, model, observations, " +
      "total_cost::float8 AS total_cost, input_tokens, output_tokens, " +
      "cache_creation_tokens, cache_read_tokens, total_tokens " +
      "FROM agents.langfuse_cost_daily WHERE day >= current_date - 90) c";
    try {
      const out = execFileSync(
        'docker',
        ['exec', '-i', 'nexus-postgres', 'sh', '-c',
          'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -f -'],
        { encoding: 'utf8', input: sql, timeout: 10000, maxBuffer: 8 * 1024 * 1024 },
      );
      res.end(out.trim() || '[]');
    } catch {
      res.end('[]');
    }
    return;
  }

  if (url.pathname === '/api/system/timers/log') {
    corsJson();
    const label = url.searchParams.get('label') || '';
    const lines = Math.min(parseInt(url.searchParams.get('lines') || '200', 10) || 200, 1000);
    if (!LAUNCHD_LABEL_RE.test(label)) {
      res.statusCode = 400;
      res.end(JSON.stringify({ error: 'invalid label' }));
      return;
    }
    if (process.platform !== 'darwin') {
      res.statusCode = 501;
      res.end(JSON.stringify({ error: 'log fetch only implemented for macOS launchd' }));
      return;
    }
    const plistPath = join(HOME, 'Library', 'LaunchAgents', `${label}.plist`);
    if (!existsSync(plistPath)) {
      res.statusCode = 404;
      res.end(JSON.stringify({ error: 'plist not found' }));
      return;
    }
    const data = _loadPlistJson(plistPath);
    const logPath = data?.StandardOutPath || data?.StandardErrorPath;
    if (!logPath) {
      res.end(JSON.stringify({ label, path: null, content: '', mtime: null, note: 'no StandardOutPath/StandardErrorPath in plist' }));
      return;
    }
    if (!existsSync(logPath)) {
      res.end(JSON.stringify({ label, path: logPath, content: '', mtime: null, note: 'log file does not exist yet (job may not have fired)' }));
      return;
    }
    let content = '';
    let mtime = null;
    try {
      content = execSync(`tail -n ${lines} "${logPath}" 2>/dev/null`, { encoding: 'utf8', timeout: 3000, maxBuffer: 2 * 1024 * 1024 });
      mtime = statSync(logPath).mtime.toISOString();
    } catch (err) {
      res.statusCode = 500;
      res.end(JSON.stringify({ label, path: logPath, error: String(err.message || err) }));
      return;
    }
    res.end(JSON.stringify({ label, path: logPath, mtime, lines, content }));
    return;
  }

  if (url.pathname === '/api/system/timers') {
    corsJson();
    if (process.platform === 'darwin') {
      try { res.end(JSON.stringify(listMacosTimers())); }
      catch { res.end('[]'); }
      return;
    }
    try {
      const lines = execSync('systemctl --user list-timers --all --no-pager --no-legend 2>/dev/null', { encoding: 'utf8', timeout: 5000 }).trim();
      const timers = [];
      for (const line of lines.split('\n')) {
        if (!line.trim()) continue;
        const m = line.match(/(\S+\.timer)\s+(\S+\.service)\s*$/);
        if (!m) continue;
        const unitName = m[1].replace(/\.timer$/, '');
        let result = 'unknown', lastRun = null, nextRun = '', leftUntil = '';
        let timerDescription = '', serviceDescription = '';
        try {
          const show = execSync(`systemctl --user show ${unitName}.service --property=Result,ExecMainStartTimestamp,Description --no-pager 2>/dev/null`, { encoding: 'utf8', timeout: 3000 });
          const resultMatch = show.match(/^Result=(.+)/m);
          if (resultMatch) result = resultMatch[1];
          const tsMatch = show.match(/^ExecMainStartTimestamp=(.+)/m);
          if (tsMatch && tsMatch[1].trim()) lastRun = tsMatch[1].trim();
          const descMatch = show.match(/^Description=(.+)/m);
          if (descMatch) serviceDescription = descMatch[1].trim();
        } catch {}
        try {
          const timerShow = execSync(`systemctl --user show ${unitName}.timer --property=NextElapseUSecRealtime,Description --no-pager 2>/dev/null`, { encoding: 'utf8', timeout: 3000 });
          const nm = timerShow.match(/^NextElapseUSecRealtime=(.+)/m);
          if (nm && nm[1].trim()) nextRun = nm[1].trim();
          const descMatch = timerShow.match(/^Description=(.+)/m);
          if (descMatch) timerDescription = descMatch[1].trim();
        } catch {}
        const prefix = line.substring(0, line.indexOf(m[1])).trim();
        const cols = prefix.split(/\s{2,}/);
        if (cols.length >= 2) leftUntil = cols[1];
        timers.push({
          name: unitName, nextRun, leftUntil, lastRun, result, active: true,
          description: serviceDescription || timerDescription || null,
        });
      }
      res.end(JSON.stringify(timers));
    } catch {
      res.end('[]');
    }
    return;
  }

  if (url.pathname === '/api/system/cache') {
    corsJson();
    try {
      if (!existsSync(CACHE_DIR)) { res.end('[]'); return; }
      const files = readdirSync(CACHE_DIR);
      const entries = [];
      for (const f of files) {
        if (!f.endsWith('.md')) continue;
        const fp = join(CACHE_DIR, f);
        const st = statSync(fp);
        const ageSec = Math.floor((Date.now() - st.mtimeMs) / 1000);
        entries.push({
          project: f.replace(/\.md$/, ''), file: f,
          size: st.size, mtime: st.mtime.toISOString(),
          age: ageSec, stale: ageSec > 86400,
        });
      }
      entries.sort((a, b) => a.age - b.age);
      res.end(JSON.stringify(entries));
    } catch {
      res.end('[]');
    }
    return;
  }

  if (url.pathname === '/api/checkpoints') {
    corsJson();
    try {
      const days = parseInt(url.searchParams.get('days') || '7', 10);
      const cutoff = Date.now() - days * 86400000;
      if (!existsSync(CHECKPOINT_DIR)) { res.end('[]'); return; }
      const files = readdirSync(CHECKPOINT_DIR).filter(f => f.endsWith('-checkpoint.md'));
      const entries = [];
      for (const f of files) {
        const fp = join(CHECKPOINT_DIR, f);
        const st = statSync(fp);
        if (st.mtimeMs < cutoff) continue;
        const dateMatch = f.match(/^(\d{4}-\d{2}-\d{2})-(.+)-checkpoint\.md$/);
        let branch = null, changes = null;
        try {
          const head = readFileSync(fp, 'utf8').slice(0, 800);
          const bm = head.match(/\*\*Branch:\*\*\s*(.+)/);
          if (bm) branch = bm[1].trim();
          const cm = head.match(/\*\*Changes:\*\*\s*(.+)/);
          if (cm) changes = cm[1].trim();
        } catch {}
        entries.push({
          date: dateMatch?.[1] || null, project: dateMatch?.[2] || f,
          file: f, size: st.size, mtime: st.mtime.toISOString(),
          branch, changes,
        });
      }
      entries.sort((a, b) => new Date(b.mtime).getTime() - new Date(a.mtime).getTime());
      res.end(JSON.stringify(entries));
    } catch {
      res.end('[]');
    }
    return;
  }

  res.writeHead(404);
  res.end();
});

const wss = new WebSocketServer({ server: httpServer });

wss.on('connection', (ws) => {
  clients.add(ws);
  console.log(`Client connected (${clients.size} total)`);

  // Send current state to new client
  for (const [id, agent] of agents) {
    const status = waitingToStatus(agent.waiting);
    ws.send(JSON.stringify({ type: 'agentCreated', id, folderName: agent.name }));
    if (status === 'permission') {
      ws.send(JSON.stringify({ type: 'agentToolPermission', id }));
      ws.send(JSON.stringify({ type: 'agentStatus', id, status: 'active' }));
    } else {
      ws.send(JSON.stringify({ type: 'agentStatus', id, status }));
    }
    // Replay last known tool so overlay shows something meaningful
    if (agent.lastTool && status !== 'waiting') {
      ws.send(JSON.stringify({ type: 'agentToolStart', id, ...agent.lastTool }));
    }
  }

  // Catch up new client on relay agents from external bridge clients
  for (const [id, agent] of relayAgents) {
    ws.send(JSON.stringify({ type: 'agentCreated', id, folderName: agent.name, palette: agent.palette, agentSource: agent.agentSource }));
    if (agent.status === 'permission') {
      ws.send(JSON.stringify({ type: 'agentToolPermission', id, agentSource: agent.agentSource }));
      ws.send(JSON.stringify({ type: 'agentStatus', id, status: 'active', agentSource: agent.agentSource }));
    } else {
      ws.send(JSON.stringify({ type: 'agentStatus', id, status: agent.status, agentSource: agent.agentSource }));
    }
    if (agent.lastTool && agent.status !== 'waiting') {
      ws.send(JSON.stringify({ type: 'agentToolStart', id, ...agent.lastTool, agentSource: agent.agentSource }));
    }
  }

  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw.toString());

      if (RELAY_TYPES.has(msg.type)) {
        // Stamp agentSource on relay messages that don't declare one
        const enriched = msg.agentSource ? msg : { ...msg, agentSource: 'relay' };
        // Update relay state so future clients get an accurate catch-up
        switch (enriched.type) {
          case 'agentCreated': {
            relayAgents.set(enriched.id, { name: enriched.folderName ?? String(enriched.id), status: 'active', palette: enriched.palette, agentSource: enriched.agentSource });
            let owned = clientOwnedRelayIds.get(ws);
            if (!owned) { owned = new Set(); clientOwnedRelayIds.set(ws, owned); }
            owned.add(enriched.id);
            break;
          }
          case 'agentClosed':
            relayAgents.delete(enriched.id);
            clientOwnedRelayIds.get(ws)?.delete(enriched.id);
            break;
          case 'agentStatus': {
            const a = relayAgents.get(enriched.id);
            if (a) a.status = enriched.status;
            break;
          }
          case 'agentToolStart': {
            const a = relayAgents.get(enriched.id);
            if (a) a.lastTool = { toolId: enriched.toolId, status: enriched.status };
            break;
          }
          case 'agentToolsClear': {
            const a = relayAgents.get(enriched.id);
            if (a) a.lastTool = undefined;
            break;
          }
        }
        // Relay to every other connected client
        const data = JSON.stringify(enriched);
        for (const client of clients) {
          if (client !== ws && client.readyState === 1) client.send(data);
        }
        return;
      }

      console.log('[Client→Server]', msg.type);
    } catch {
      // ignore
    }
  });

  ws.on('close', () => {
    clients.delete(ws);
    console.log(`Client disconnected (${clients.size} total)`);
    // Evict relay agents owned by this connection so they don't ghost on reconnect
    const owned = clientOwnedRelayIds.get(ws);
    if (owned?.size) {
      for (const id of owned) {
        relayAgents.delete(id);
        broadcast({ type: 'agentClosed', id });
      }
      console.log(`[Relay] Evicted ${owned.size} stale relay agent(s) on disconnect`);
    }
  });
});

// ── Start ────────────────────────────────────────────────────────

httpServer.listen(PORT, () => {
  console.log(`Pixel Dashboard bridge server`);
  console.log(`  WebSocket: ws://localhost:${PORT}`);
  console.log(`  HTTP API:  http://localhost:${PORT}/api/memory`);
  console.log(`  Polling tmux session: "${TMUX_SESSION}" every ${POLL_MS}ms`);
  console.log(`  Transcripts: ${CLAUDE_PROJECTS_DIR}`);
});

// Baseline poll interval
setInterval(poll, POLL_MS);
poll();

// Watch apm.log for immediate updates when hooks fire
const APM_LOG = join(HOME, '.tmux', 'apm.log');
let watchDebounce = null;
try {
  watch(APM_LOG, () => {
    // Debounce rapid writes (hooks fire in bursts)
    if (watchDebounce) return;
    watchDebounce = setTimeout(() => {
      watchDebounce = null;
      poll();
    }, 50);
  });
  console.log(`  Watching: ${APM_LOG} (event-driven updates)`);
} catch {
  console.log(`  Note: fs.watch on apm.log unavailable, using polling only`);
}
