# Linux Server Setup Plan
# Ubuntu Server 24.04 — agents-nexus mini PC host

This plan covers turning a fresh Ubuntu Server 24.04 box into the canonical
agents-nexus host. Claude Code agents and tmux sessions run here. Client
machines (mac, windows) SSH in and connect to MCP endpoints over Tailscale.

---

## Architecture

```
mini PC (Ubuntu Server 24.04)
├── tailscale                          # LAN + remote access
├── caddy                              # reverse proxy (all services under one host)
├── docker compose
│   ├── nexus-ollama      :11434       # embeddings
│   ├── nexus-spark       :8343        # semantic search MCP (SSE)
│   ├── nexus-mnemon-flush             # event drain daemon
│   ├── nexus-dashboard   :8421        # pixel office UI (nginx)
│   └── [langfuse profile] :3000       # optional observability
├── arbiter               :8420        # native — needs local tmux socket
├── mnemon MCP            (stdio)      # native — Claude Code on same box
└── tmux                               # agent sessions live here

client machines (mac / windows laptop)
├── SSH → mini PC tmux
├── Claude Code (MCP config → mini PC Tailscale IP)
└── browser → http://<tailscale-ip>:8421  (dashboard)
```

---

## Phase 1 — System Bootstrap

### 1.1 Base packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
  git curl wget unzip \
  tmux fzf \
  build-essential \
  ca-certificates gnupg lsb-release \
  tailscale
```

### 1.2 Docker

```bash
# Official Docker install (not snap)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

### 1.3 Task runner

```bash
sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b ~/.local/bin
```

### 1.4 Node.js (for arbiter + settings merge script)

```bash
curl -fsSL https://fnm.vercel.app/install | bash
source ~/.bashrc
fnm install --lts
fnm use lts-latest
```

### 1.5 uv (Python — for mnemon + spark)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1.6 Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

---

## Phase 2 — Tailscale

```bash
sudo tailscale up
# Note your Tailscale IP — use this in client MCP configs
tailscale ip -4
```

Enable exit node or subnet routing if you want LAN-only access without
the Tailscale cloud relay (optional — direct LAN works fine for home use).

---

## Phase 3 — Clone and Configure

```bash
mkdir -p ~/repos
git clone https://github.com/persinac/agents-nexus.git ~/repos/agents-nexus
cd ~/repos/agents-nexus

cp .env.example .env
$EDITOR .env
# Key fields:
#   DATABASE_URL    — cloud Postgres connection string
#   GITLAB_TOKEN    — GitLab PAT
#   REPOS_PATH      — /home/<user>/repos  (absolute, no ~)
#   HOST_TMUX_DIR   — /home/<user>/.tmux
#   OLLAMA_BASE_URL — http://localhost:11434
```

---

## Phase 4 — Docker Stack

```bash
task docker:up
task docker:init       # pull nomic-embed-text into ollama (~270 MB, once)
task mnemon:migrate    # run schema migrations against cloud postgres
task spark:reclaim     # full index build — takes a while
```

Optionally start Langfuse:
```bash
task langfuse:up
```

---

## Phase 5 — Caddy Reverse Proxy

Create `/etc/caddy/Caddyfile`:

```
# Replace <tailscale-ip> with output of: tailscale ip -4
# Or use a hostname if you set one in Tailscale DNS.

http://<tailscale-ip> {
    # Pixel dashboard
    handle /dashboard* {
        reverse_proxy localhost:8421
    }

    # Spark MCP (SSE)
    handle /spark* {
        reverse_proxy localhost:8343
    }

    # Arbiter WebSocket bridge
    handle /arbiter* {
        reverse_proxy localhost:8420
    }

    # Langfuse (if running)
    handle /langfuse* {
        reverse_proxy localhost:3000
    }

    # Default → dashboard
    handle {
        reverse_proxy localhost:8421
    }
}
```

```bash
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

> TODO: evaluate whether each service gets its own subdomain via Tailscale
> MagicDNS (spark.nexus, arbiter.nexus, etc.) vs path-based routing above.
> Subdomains are cleaner for MCP SSE connections.

---

## Phase 6 — tmux Layer (Linux)

### 6.1 Install script

`tmux/linux/install.sh` — mirrors `mac/install.sh` with these changes:

- Sources `bashrc` instead of `zshrc`
- Installs **systemd user units** instead of launchd plists
- No `Library/LaunchAgents` path

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NEXUS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Symlink tmux config
ln -sf "$SCRIPT_DIR/tmux.conf" "$HOME/.tmux.conf"

# Symlink all scripts
mkdir -p "$HOME/.tmux"
for f in "$SCRIPT_DIR"/tmux-scripts/*.sh "$SCRIPT_DIR"/tmux-scripts/*.py; do
  [ -f "$f" ] || continue
  chmod +x "$f"
  ln -sf "$f" "$HOME/.tmux/$(basename "$f")"
done

# Write env.sh
ENV_FILE="$HOME/.tmux/env.sh"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
REPO_DIR="\${REPO_DIR:-$HOME/repos}"
NOTES_DIR="\${NOTES_DIR:-$HOME/notes}"
AGENTS_NEXUS_DIR="\${AGENTS_NEXUS_DIR:-$NEXUS_DIR}"
EXTRA_REPO_DIRS="\${EXTRA_REPO_DIRS:-}"
EOF
  echo "Created ~/.tmux/env.sh"
fi

# Install systemd user units
mkdir -p "$HOME/.config/systemd/user"
for unit in "$SCRIPT_DIR"/systemd/*.service; do
  [ -f "$unit" ] || continue
  name=$(basename "$unit")
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$NEXUS_DIR|g" \
    "$unit" > "$HOME/.config/systemd/user/$name"
  systemctl --user enable "$name"
  systemctl --user start "$name"
  echo "  Enabled systemd unit: $name"
done

# Merge claude settings
mkdir -p "$HOME/.claude"
SETTINGS="$HOME/.claude/settings.json"
TEMPLATE="$SCRIPT_DIR/claude-settings.json"
if [ ! -f "$SETTINGS" ]; then
  cp "$TEMPLATE" "$SETTINGS"
  echo "Created ~/.claude/settings.json"
else
  # same node merge as mac install
  node - "$SETTINGS" "$TEMPLATE" <<'EOF'
const [,, existingPath, templatePath] = process.argv;
const existing = JSON.parse(require('fs').readFileSync(existingPath, 'utf8'));
const template = JSON.parse(require('fs').readFileSync(templatePath, 'utf8'));
for (const [event, entries] of Object.entries(template.hooks ?? {})) {
  existing.hooks ??= {};
  existing.hooks[event] ??= [];
  for (const entry of entries) {
    const cmd = entry.hooks?.[0]?.command;
    const alreadyPresent = existing.hooks[event].some(e => e.hooks?.[0]?.command === cmd);
    if (!alreadyPresent) existing.hooks[event].push(entry);
  }
}
const existingPerms = existing.permissions?.allow ?? [];
const templatePerms = template.permissions?.allow ?? [];
existing.permissions ??= {};
existing.permissions.allow = [...new Set([...existingPerms, ...templatePerms])];
require('fs').writeFileSync(existingPath, JSON.stringify(existing, null, 2) + '\n');
EOF
  echo "Merged claude-settings.json into ~/.claude/settings.json"
fi

# Source bashrc
MARKER="# agent-orchestration"
if ! grep -qF "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
  echo "" >> "$HOME/.bashrc"
  echo "$MARKER" >> "$HOME/.bashrc"
  echo "source \"$SCRIPT_DIR/bashrc\"" >> "$HOME/.bashrc"
  echo "Added source line to ~/.bashrc"
fi

echo "Done. Run: task up && tmux source ~/.tmux.conf"
```

### 6.2 tmux.conf

Identical to `mac/tmux.conf` except:

- Remove `bell-action any` / `visual-bell off` (keep default tmux bell behavior)
- Bell propagates to SSH client terminal which handles its own notification

```diff
- set -g bell-action any
- set -g visual-bell off
+ # Bell propagates to SSH client — terminal handles system notification
+ set -g bell-action any
+ set -g visual-bell off
```

Actually identical — SSH clients (iTerm2, Windows Terminal) respond to tmux
bells the same way they would locally. No change needed.

### 6.3 Script delta from mac/

| Script | Change |
|--------|--------|
| `hook-notification.sh` | Replace `osascript` with `printf '\a'` (tmux bell → SSH client) |
| `hook-stop.sh` | No change needed |
| `hook-pretooluse.sh` | No change needed |
| `hook-memory.sh` | No change needed |
| `log-action.sh` | No change needed |
| `apm-bar.sh` | No change needed |
| `window-status.sh` | No change needed |
| `launch-claude.sh` | Verify fzf path (`/usr/bin/fzf`) |
| `flush-events.*` | No change needed |
| `memory-*.py` | No change needed |
| `agent-registry.sh` | No change needed |
| `agent-send.sh` | No change needed |
| `worktree-cleanup.sh` | No change needed |
| `peek-summary.sh` | No change needed |
| `stats.sh` | No change needed |

**Only one real diff: `hook-notification.sh`**

```bash
# Replace:
osascript -e "display notification \"...\" ..." 2>/dev/null &

# With:
printf '\a'   # bell — SSH client (iTerm2 / Windows Terminal) handles notification
```

### 6.4 bashrc (shell functions)

Mirrors `mac/zshrc` with these changes:

- `read -rsn1` instead of `read -rk1` (bash vs zsh)
- `source` instead of `.` for explicit clarity
- `claude-init` uses `$BASH_SOURCE` instead of `${0:A:h}` for script path resolution

Everything else (`work`, `q`, `v`, `qa`, `agents`, `wt`) is identical.

---

## Phase 7 — Systemd Autostart

Replace launchd with systemd user units. Create `tmux/linux/systemd/`:

### `agents-nexus-stack.service`
Starts the Docker stack + native services on login.

```ini
[Unit]
Description=agents-nexus stack (arbiter + mnemon)
After=network.target docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=__AGENTS_NEXUS_DIR__
ExecStart=/bin/bash -c 'task up'
ExecStop=/bin/bash -c 'task kill && docker compose down'
Environment=HOME=__HOME__

[Install]
WantedBy=default.target
```

Enable: `systemctl --user enable --now agents-nexus-stack`

> Note: Docker itself is managed by the system-level dockerd service (started
> at boot). The user unit just runs `task up` after Docker is ready.

---

## Phase 8 — Client Machine Setup

Each client machine (mac / windows) needs only:

### SSH config (`~/.ssh/config`)
```
Host nexus
  HostName <tailscale-ip>
  User <username>
  IdentityFile ~/.ssh/id_ed25519
  ForwardAgent yes
```

### Claude Code MCP config (`~/.claude.json`)
```json
{
  "mcpServers": {
    "agent-memory": {
      "type": "stdio",
      "command": "ssh",
      "args": [
        "nexus",
        "/home/<user>/repos/agents-nexus/mnemon/.venv/bin/python3",
        "-m", "agent_memory.server.mcp_server"
      ]
    },
    "spark": {
      "type": "sse",
      "url": "http://<tailscale-ip>:8343/sse"
    }
  }
}
```

> `agent-memory` tunnels stdio over SSH so the MCP server runs on the mini PC
> but appears local to Claude Code. No network protocol change needed.
> `spark` is already SSE — just point at the Tailscale IP.

### Daily workflow from client
```bash
ssh nexus          # or: ssh -t nexus tmux new-session -A -s agents
work               # (inside SSH session) attach to agents tmux session
# all hotkeys work exactly as local
```

Open `http://<tailscale-ip>:8421` in local browser for the pixel dashboard.

---

## Open Questions / TODOs

- [ ] Evaluate Tailscale MagicDNS subdomains vs Caddy path routing
      (e.g. `spark.nexus.ts.net` instead of `<ip>:8343`)
- [ ] SSH over MCP for mnemon vs converting mnemon to SSE transport
      (SSE would remove the SSH dependency but needs more work in mnemon)
- [ ] Langfuse data migration path from existing ~/langfuse deployment
- [ ] `install.sh` root script — detect Linux and delegate to tmux/linux/install.sh
- [ ] Git clone strategy for repos on mini PC (bare clone + fetch, or full clones)
- [ ] Disk sizing — spark index for 436 repos is significant; check nvme size
- [ ] Auth on caddy — at minimum HTTP basic auth in front of dashboard + arbiter
      before exposing via Tailscale (Tailscale ACLs may be sufficient)
