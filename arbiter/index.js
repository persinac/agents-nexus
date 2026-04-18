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
import { execSync } from 'child_process';
import { readFileSync, existsSync, readdirSync, statSync, watch } from 'fs';
import { homedir } from 'os';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const MEMORY_RECALL_SCRIPT = resolve(__dirname, '../tmux/mac/tmux-scripts/memory-recall.py');
const MEMORY_STATS_SCRIPT = resolve(__dirname, '../tmux/mac/tmux-scripts/memory-stats.py');

const PORT = parseInt(process.env.PORT || '8420', 10);
const POLL_MS = 100;  // fast baseline poll
const TMUX_SESSION = process.env.TMUX_SESSION || 'agents';

// Platform detection
const isWindows = process.platform === 'win32';
const TMUX_BIN = isWindows ? 'C:\\msys64\\usr\\bin\\tmux.exe' : 'tmux';

// Claude transcript directory
const HOME = homedir();
const CLAUDE_PROJECTS_DIR = join(HOME, '.claude', 'projects');

// Prefer the agent-memory venv python (has psycopg) over system python3
const AGENT_MEM_VENV = process.env.AGENTS_NEXUS_DIR
  ? join(process.env.AGENTS_NEXUS_DIR, 'mnemon/.venv')
  : join(HOME, 'repos/agents-nexus/mnemon/.venv');
const MEMORY_PYTHON = (() => {
  for (const p of [
    join(AGENT_MEM_VENV, 'bin', 'python3'),
    join(AGENT_MEM_VENV, 'Scripts', 'python3.exe'),
  ]) {
    if (existsSync(p)) return p;
  }
  return 'python3';
})();

// ── State ────────────────────────────────────────────────────────

/** @type {Map<number, {name: string, waiting: string, path: string, lastTool?: {toolId: string, status: string}}>} */
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
      `"${TMUX_BIN}" list-windows -t "${TMUX_SESSION}" -F "#{window_index}|#{window_name}|#{@waiting}|#{pane_current_path}|#{pane_current_command}"`,
      { encoding: 'utf8', timeout: 2000 }
    ).trim();
    return out.split('\n').filter(Boolean).map(line => {
      const [index, name, waiting, path, command] = line.split('|');
      return { index: parseInt(index, 10), name, waiting, path, command };
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
          if (agent) agent.lastTool = { toolId, status };

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
      agents.set(id, { name: win.name, waiting: win.waiting, path: win.path });
      console.log(`[Poll] New agent ${id} (${win.name}) waiting="${win.waiting}" → ${status}`);
      broadcast({ type: 'agentCreated', id, folderName: win.name });
      broadcastStatus(id, status);
    } else {
      // Status changed?
      if (existing.waiting !== win.waiting) {
        console.log(`[Poll] Agent ${id} (${win.name}) waiting="${existing.waiting}" → "${win.waiting}" (${status})`);
        existing.waiting = win.waiting;
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
