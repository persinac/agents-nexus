# claude-agents-tmux

RTS-inspired multi-agent orchestration — manage multiple Claude Code agents across repos
without full context switches, using tmux as the orchestration layer.

Multi-platform: macOS, Windows (MSYS2), Linux.

## Knowledge Stack (Docker)

Postgres + pgvector, Ollama, Spark, and the pixel dashboard all run as Docker services that start automatically on login. The only piece that stays native is the arbiter (it needs your local tmux socket).

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — enable **Start at login** in settings
- [`task`](https://taskfile.dev) — `brew install go-task`
- [`spark/`](spark/) — included in this repo

### First-time setup

```bash
cd ~/repos/agents-nexus

# 1. Create your local env file
cp .env.example .env

# 2. Fill in secrets
#    - POSTGRES_PASSWORD   — pick anything, used internally only
#    - GITLAB_TOKEN        — copy from guilty-spark/.env
#    - SPARK_SOURCE        — absolute path to the guilty-spark repo
#    - REPOS_PATH          — absolute path to your repos directory
#    - HOST_TMUX_DIR       — usually ~/.tmux
$EDITOR .env

# 3. Start all services
task docker:up

# 4. Pull the embedding model into Ollama (once; ~270 MB)
task docker:init

# 5. Build the Spark index (first run indexes all repos — takes a while)
task spark:reclaim

# 6. Wire autostart so the stack comes up after every reboot
task launchd:install
```

### Point Claude Code at the local services

Update `mnemon/.env` so the MCP memory server connects to the Docker postgres:

```
DATABASE_URL=postgresql://agents:<your-password>@localhost:5432/agents?sslmode=disable
OLLAMA_BASE_URL=http://localhost:11434
```

Spark's MCP server is already running at `http://localhost:8343` (SSE transport). Add it to `~/.claude.json` if it isn't there:

```json
{
  "mcpServers": {
    "spark": {
      "type": "sse",
      "url": "http://localhost:8343/sse"
    }
  }
}
```

### Start the pixel dashboard

The dashboard UI is served by Docker at `http://localhost:8421`. Start the arbiter (WebSocket bridge) natively:

```bash
task arbiter:start
```

Then open `http://localhost:8421` in a browser.

### Day-to-day commands

```bash
task docker:up              # start everything
task docker:down            # stop (data volumes preserved)
task docker:logs            # tail all logs
task docker:logs -- spark   # tail a single service
task docker:status          # health + uptime

task spark:activate -- my-repo   # re-index one repo after changes
task spark:status                # index stats

task launchd:install        # enable autostart
task launchd:uninstall      # disable autostart
```

### Ports

| Service | Port |
|---------|------|
| Postgres | 5432 |
| Ollama | 11434 |
| Spark | 8343 |
| Dashboard UI | 8421 |
| Arbiter (native) | 8420 |

---

## Install

One command — detects your OS, installs system deps, links configs, and sets up the pixel dashboard:

```bash
cd /path/to/agent-orchestration
./install.sh            # full install (deps + configs + dashboard)
./install.sh --no-ui    # skip pixel dashboard setup
```

The installer handles macOS (Homebrew), Windows (MSYS2/pacman), and Linux (apt/dnf/pacman).

> **Windows:** Requires [MSYS2](https://www.msys2.org/) (default: `C:\msys64`). Run inside an MSYS2 terminal.
> MSYS2's `$HOME` is `/home/<user>` (`C:\msys64\home\<user>`), not `/c/Users/<user>`.

Platform-specific install scripts (`mac/install.sh`, `windows/install.sh`) still work standalone if you prefer.

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) must be installed and on `PATH` for all platforms.

## Usage

### Start a session

```bash
work            # attach/create "agents" session
work query      # attach/create "query" session
```

### Spawn agents

| Hotkey | Action |
|---|---|
| `ctrl+a → N` | Fuzzy repo picker → opens claude in new background window |
| `ctrl+a → n` | Prompt for path → opens claude there |

### Monitor agents

| Command / Hotkey | Action |
|---|---|
| `v 2` | Quick peek at agent 2 (status summary + last output) |
| `ctrl+a → A` | APM dashboard popup |
| `agents` | List all registered agents with slot, name, and directory |
| Status bar | Grey = idle, Green = running, Yellow = stuck (>10min), Red = needs input |

### Send commands without switching

```bash
q 2 use JWT                       # queue message to agent 2
q 2 "can you check the tests?"   # quote if message has ? ! * etc.
q 2 1                             # approve (no Enter — instant select)
```

### Agent-to-agent messaging

Agents automatically know about each other. On startup, each agent:

1. **Registers** itself in `~/.tmux/registry/` (keyed by pane ID, so slot numbers stay correct across `renumber-windows`)
2. **Receives a peer list** in its opening prompt — slot number, project name, and directory for every other active agent

This means agents can use `/msg <slot> <message>` without you telling them which slot to target. The `agents` shell command shows the same registry for humans.

### Navigation

| Hotkey | Action |
|---|---|
| `ctrl+a → 1..9` | Jump to window N |
| `ctrl+a → w` | Window list with live preview |
| `ctrl+a → s` | Session tree |
| `ctrl+a → \|` | Split pane horizontal |
| `ctrl+a → -` | Split pane vertical |
| `ctrl+a → d` | Detach (leave running in background) |
| `ctrl+a → r` | Reload tmux config |
| `ctrl+a → ,` | Rename current window |

## APM Tracking

The status bar shows a rolling 60-second count: `42a/7h` = 42 agent actions, 7 human actions.

`ctrl+a → A` opens the full dashboard with today's totals, avg response time, and active agent count.

### What gets tracked

| Event | Logged as |
|---|---|
| Agent tool use | `agent` |
| Agent waiting for input | `wait` |
| `q` command sent | `human-q` |
| `v` peek | `human-v` |
| Window switch | `switch` |
| Fuzzy picker / new window / splits | `tmux-*` |

Log lives at `~/.tmux/apm.log`, auto-pruned to 24h.

## Claude Code Hooks

The `claude-settings.json` configures two hooks:

- **Stop** — sets `@waiting` flag (turns status bar red), fires bell, logs `wait`
- **PreToolUse** — clears `@waiting` flag, logs `agent` tool use

## Files

```
├── install.sh               # unified installer (detects OS, installs everything)
├── CLAUDE.md.template       # scaffold template for per-repo CLAUDE.md
├── IDEAS.md                 # roadmap & feature ideas
├── searchable-history-design.md  # design doc for #11 (searchable history)
├── mac/
│   ├── install.sh           # symlinks into ~/
│   ├── zshrc                # shell functions (zsh)
│   ├── tmux.conf
│   ├── claude-settings.json
│   └── tmux-scripts/        # macOS-specific (osascript, BSD date, open-claude.sh)
├── windows/
│   ├── install.sh           # copies into MSYS2 $HOME
│   ├── bashrc               # shell functions (bash)
│   ├── tmux.conf
│   ├── claude-settings.json
│   └── tmux-scripts/        # Windows-specific (PowerShell toast, GNU date)
├── pixel-dashboard/         # animated pixel art agent dashboard
│   ├── server/              # WebSocket bridge (tmux → browser)
│   └── ui/                  # React + Vite frontend
└── linux/
    └── README.md            # placeholder — not yet implemented
```

## Platform differences

| | macOS | Windows (MSYS2) | Linux |
|---|---|---|---|
| Shell | zsh | bash | bash |
| Home | `~/` | `/home/<user>` (MSYS2) | `~/` |
| Repo dir | `~/repos` | `/c/projects` | configurable |
| `date` | BSD (`-v0H`) | GNU (`-d "today..."`) | GNU |
| `read` key | `-rk1` (zsh) | `-rsn1` (bash) | `-rsn1` |
| Notifications | `osascript` | PowerShell toast | `notify-send` |
| Idle check | `zsh` process | `bash` process | `bash` process |
