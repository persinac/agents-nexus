# GEEKOM A7 MAX Setup — agents-nexus Dedicated Box

Dedicated always-on box for running the full agents-nexus stack so the gaming
PC stays free. Postgres stays on Digital Ocean — only compute and indexing move
to the box. Client machines (mac, windows) SSH in and connect to MCP endpoints
over Tailscale.

## Progress

| Phase | Status |
|-------|--------|
| 1. OS & Base Setup | :white_check_mark: Done — Ubuntu 25.04, hostname `nexus`, SSH, static IP `192.168.4.94` |
| 2. Install Dependencies | :white_check_mark: Done — Docker, fnm/Node, uv, Task, Claude Code, Tailscale (`100.75.154.84`), gh, Caddy |
| 3. Clone & Configure | :white_check_mark: Done — repos cloned via `clone-from-manifest.py` into categorized dirs (personal/cackalacky/flashback-fleet/community) |
| 4. Docker Stack | :white_check_mark: Done — Ollama, Spark, mnemon-flush, dashboard all healthy. Spark reindex complete |
| 5. Langfuse Observability | :white_check_mark: Done — all 6 containers healthy, account + project created, API keys wired into .env |
| 6. Caddy Reverse Proxy | :white_check_mark: Done — path-based routing at `http://100.75.154.84` |
| 7. Autostart (systemd) | :white_check_mark: Done — Docker stack, arbiter, flush timer, spark nightly reindex all enabled |
| 8. tmux Layer | :white_check_mark: Done — Linux install script, hooks, bashrc functions, systemd user units |
| 9. API Key Rotation | :white_check_mark: Ready — infrastructure in place (`usekey`/`whichkey`/`keys`), activate when needed |
| 10. Client Machine Setup | :white_check_mark: Done — SSH config, MCP servers (spark SSE + agent-memory over SSH) in `~/.claude.json` |
| 11. Stability (idle reboots) | :warning: Stopgap only — C-state fix **falsified** (rebooted 6× with it active 2026-06-22); now on `cpu-boost-off.service` (boost disabled). Root cause = power delivery; **DC-brick swap + BIOS update pending**. See [nexus-reboot-plan.md](./nexus-reboot-plan.md) |

**Pre-work completed:** Repo discovery pipeline built (`scripts/`), manifest
generated with rule-based + AI tags (`repos-manifest.yaml`), reference repos
list ready (`scripts/clone-reference-repos.sh`).

## Architecture

```
mini PC — GEEKOM A7 MAX (Ubuntu Server 25.04)
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

client machines (mac / windows laptop / gaming PC)
├── SSH → mini PC tmux
├── Claude Code (MCP config → mini PC Tailscale IP)
└── browser → http://100.75.154.84:8421  (dashboard)
```

---

## Phase 1 — OS & Base Setup :white_check_mark:

1. **Install Ubuntu Server 25.04** — headless, no desktop. Set hostname
   (`nexus`), create user, enable OpenSSH server during install.
2. **Assign a static IP** — DHCP reservation on router: `192.168.4.94`.
3. **SSH in from another machine** — `ssh user@192.168.4.94`. Everything from
   here is remote.

---

## Phase 2 — Install Dependencies :white_check_mark:

### 2.1 Base packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
  git curl wget unzip \
  tmux fzf \
  build-essential \
  ca-certificates gnupg lsb-release \
  postgresql-client
```

### 2.2 Docker

```bash
# Official Docker install (not snap)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

### 2.3 Task runner

```bash
sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b ~/.local/bin
```

### 2.4 Node.js (for arbiter + settings merge script)

```bash
curl -fsSL https://fnm.vercel.app/install | bash
source ~/.bashrc
fnm install --lts
fnm use lts-latest
```

### 2.5 uv (Python — for mnemon + spark)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2.6 Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude  # first run — interactive login
```

### 2.7 Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Tailscale gives the box a stable address reachable from anywhere.
Tailscale IP: `100.75.154.84`. Use this instead of the LAN IP for MCP URLs
and SSH so everything works on and off your home network.

### 2.8 Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

---

## Phase 3 — Clone & Configure :arrow_left: UP NEXT

```bash
mkdir -p ~/repos
git clone https://github.com/persinac/agents-nexus.git ~/repos/agents-nexus
cd ~/repos/agents-nexus
```

**Create `.env`** — copy from Windows box, change paths:

| Variable | Windows | Linux |
|----------|---------|-------|
| `DATABASE_URL` | same (points to DO) | **no change** |
| `GITLAB_TOKEN` | same | same |
| `REPOS_PATH` | `C:/projects` | `/home/<user>/repos` |
| `HOST_TMUX_DIR` | `C:/msys64/home/apfba/.tmux` | `/home/<user>/.tmux` |
| `OLLAMA_BASE_URL` | same | `http://localhost:11434` |
| Everything else | same | same |

**Clone personal repos** for Spark to index — cackalackycon, flashback-fleet,
agents-nexus, and any other personal projects. No work repos on this box.

Use the repo manifest pipeline (see `scripts/README.md`) to clone everything:
```bash
# Personal repos from clone-urls.txt
while read -r url; do
  name=$(basename "$url" .git)
  [ -d "$HOME/repos/$name" ] || git clone "$url" "$HOME/repos/$name"
done < ~/repos/agents-nexus/scripts/clone-urls.txt

# Reference repos (community)
bash ~/repos/agents-nexus/scripts/clone-reference-repos.sh ~/repos/reference
```

---

## Phase 4 — Docker Stack

```bash
task install          # symlinks tmux conf + scripts, installs dashboard/arbiter/mnemon deps
task docker:up        # starts Ollama, Spark, mnemon-flush, dashboard
task docker:init      # pulls nomic-embed-text into Ollama (~270 MB, one-time)
task mnemon:migrate   # ensure DB schema is current (hits DO Postgres)
task spark:reclaim    # full index of all repos — takes a while
```

### Verify

```bash
task docker:status              # all containers healthy
curl localhost:8343/webhook/status  # spark alive
curl localhost:8421             # dashboard serves HTML
curl localhost:11434/api/tags   # ollama has nomic-embed-text
```

---

## Phase 5 — Langfuse Observability

The compose stack includes a full Langfuse v3 profile (Postgres, Redis,
ClickHouse, MinIO, web + worker). It runs behind its own Docker Compose
profile so `task docker:up` doesn't start it — you opt in explicitly.

### 5.1 Configure secrets

Edit `.env` and change the Langfuse defaults to real values:

```bash
# .env — Langfuse section
LANGFUSE_PORT=3000
LANGFUSE_DB_PASSWORD=<generate-a-password>
LANGFUSE_REDIS_AUTH=<generate-a-password>
LANGFUSE_CLICKHOUSE_PASSWORD=<generate-a-password>
LANGFUSE_NEXTAUTH_SECRET=<openssl rand -hex 32>
LANGFUSE_SALT=<openssl rand -hex 32>
LANGFUSE_ENCRYPTION_KEY=<openssl rand -hex 32>
```

### 5.2 Start the stack

```bash
task langfuse:up          # starts all 6 Langfuse containers
task langfuse:status      # verify all healthy
```

### 5.3 First-run setup

1. Open `http://localhost:3000` (or `http://100.75.154.84:3000` from a client)
2. Create an account — this is the local admin, no external auth needed
3. Create a new project (e.g. `agents-nexus`)
4. Go to **Settings → API Keys** and create a key pair

> **Note:** Langfuse redirects to `localhost` after sign-up/login because
> `NEXTAUTH_URL` defaults to `http://localhost:3000`. For remote access,
> set `NEXTAUTH_URL=http://100.75.154.84:3000` in `.env` and restart
> Langfuse.

### 5.4 Wire up mnemon tracing

Add the API keys from step 3 to `.env`:

```bash
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

mnemon's tracing module (`mnemon/agent_memory/tracing.py`) exposes a
`set_trace_callback()` hook. With `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` set in the environment, you can register a Langfuse
callback that pipes every memory operation (L2/L3 reads, retrieval scoring,
archival) into Langfuse as traces. Install the SDK:

```bash
cd ~/repos/agents-nexus/mnemon
uv pip install langfuse
```

### 5.5 Verify

```bash
curl localhost:3000/api/public/health    # {"status":"OK"}
task langfuse:logs                       # tail web + worker logs
```

### 5.6 Useful task commands

| Command | What it does |
|---------|-------------|
| `task langfuse:up` | Start the full Langfuse stack |
| `task langfuse:down` | Stop Langfuse (data volumes preserved) |
| `task langfuse:update` | Pull latest images and restart |
| `task langfuse:logs` | Tail web + worker logs |
| `task langfuse:status` | Show container health |

> Data lives in named Docker volumes (`langfuse-postgres-data`,
> `langfuse-clickhouse-data`, etc.) so `task langfuse:down` preserves
> everything. Only `docker compose --profile langfuse down -v` wipes data.

---

## Phase 6 — Caddy Reverse Proxy

> Langfuse is included in the Caddy config below at `/langfuse`.

Create `/etc/caddy/Caddyfile`:

```
# Replace 100.75.154.84 with output of: tailscale ip -4
# Or use a hostname if you set one in Tailscale DNS.

http://100.75.154.84 {
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

## Phase 7 — Autostart on Boot (systemd)

### Docker stack

`/etc/systemd/system/agents-nexus.service`:

```ini
[Unit]
Description=agents-nexus Docker stack
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=<your-user>
WorkingDirectory=/home/<user>/repos/agents-nexus
ExecStart=/usr/local/bin/docker compose up --no-recreate -d
ExecStop=/usr/local/bin/docker compose down

[Install]
WantedBy=multi-user.target
```

### Arbiter

`/etc/systemd/system/agents-nexus-arbiter.service`:

```ini
[Unit]
Description=agents-nexus arbiter (tmux to dashboard bridge)
After=agents-nexus.service

[Service]
User=<your-user>
WorkingDirectory=/home/<user>/repos/agents-nexus/arbiter
ExecStart=/usr/bin/node index.js
Restart=on-failure
Environment=PORT=8420

[Install]
WantedBy=multi-user.target
```

### Agents-nexus stack (native services via task)

User-level unit for arbiter + mnemon via `task up`:

`~/.config/systemd/user/agents-nexus-stack.service`:

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

### Memory flush timer

`/etc/systemd/system/agents-nexus-flush.timer`:

```ini
[Unit]
Description=Flush agent memory events every 2 minutes

[Timer]
OnBootSec=30
OnUnitActiveSec=120

[Install]
WantedBy=timers.target
```

### Spark nightly reindex

`/etc/systemd/system/spark-nightly.timer`:

```ini
[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### Enable all

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agents-nexus agents-nexus-arbiter agents-nexus-flush.timer spark-nightly.timer
```

---

## Phase 8 — tmux Layer (Linux)

### 8.1 Install script

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

### 8.2 tmux.conf

Identical to `mac/tmux.conf` — SSH clients (iTerm2, Windows Terminal) respond
to tmux bells the same way they would locally. No changes needed.

### 8.3 Script delta from mac/

| Script | Change |
|--------|--------|
| `hook-notification.sh` | Replace `osascript` with `printf '\a'` (tmux bell -> SSH client) |
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

### 8.4 bashrc (shell functions)

Mirrors `mac/zshrc` with these changes:

- `read -rsn1` instead of `read -rk1` (bash vs zsh)
- `source` instead of `.` for explicit clarity
- `claude-init` uses `$BASH_SOURCE` instead of `${0:A:h}` for script path resolution

Everything else (`work`, `q`, `v`, `qa`, `agents`, `wt`) is identical.

---

## Phase 9 — API Key Rotation

Multiple named keys live in `~/.tmux/keys/` (never committed):

```bash
mkdir -p ~/.tmux/keys
echo 'sk-ant-...' > ~/.tmux/keys/alex
echo 'sk-ant-...' > ~/.tmux/keys/buddy
chmod 600 ~/.tmux/keys/*
```

Switch the active key for the session:
```bash
usekey alex      # set key + update tmux session env -> all new windows inherit it
usekey buddy     # swap to buddy's key
whichkey         # show active key name + first 12 chars
keys             # list all available profiles (* = active)
```

The status bar shows `[key:buddy]` in red when a non-default key is active.
When the default key is in use, nothing is shown (no clutter).

> Existing Claude processes keep their key until restarted. `usekey` only
> affects new windows/panes spawned after the call.

---

## Phase 10 — Client Machine Setup

Each client machine (mac / windows / gaming PC) needs only:

### SSH config (`~/.ssh/config`)
```
Host nexus
  HostName 100.75.154.84
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
      "url": "http://100.75.154.84:8343/sse"
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

Open `http://100.75.154.84:8421` in local browser for the pixel dashboard.
Open `http://100.75.154.84:3000` for Langfuse (or `/langfuse` via Caddy).

---

## Phase 11 — Stability: Spontaneous Reboots (AMD deep-idle)

> ⚠️ **Superseded — see [nexus-reboot-plan.md](./nexus-reboot-plan.md).** A 3rd
> cluster on 2026-06-22 hard-reset **6× with the C-state fix below already active**,
> falsifying the deep-idle theory. Root cause is now **marginal power delivery under
> the 7940HS's boost current transients**; the live stopgap is `cpu-boost-off.service`
> (CPU boost disabled) and the real fix is a DC-brick swap. The C-state cap is kept
> only as harmless insurance. The tooling below (`crash-breadcrumb`/`boot-notify`)
> remains valid and current.

**Symptom:** the box hard-reboots on its own with no logs — `journalctl -b -1`
ends mid-activity, with no OOM / MCE / thermal / panic. From an SSH client this
looks like a "network error" (PuTTY etc.), because the TCP session dies *with*
the box — it's not the network, it's the box vanishing underneath you.

**Diagnosis:** `sar` (sysstat) shows the box was ~95% **idle** before every crash,
and it runs cool (`k10temp` Tctl ~45 °C). Idle + instant + logless on a Ryzen APU
is the classic **deep-idle C-state hang**. An unprotected wall-power dip (no UPS)
is a weaker second suspect with the same logless signature.

**Kernel fix (one-time, manual — `install.sh` does not touch boot params):**

```bash
# disable the deep C-states (live, until reboot)
echo 1 | sudo tee /sys/devices/system/cpu/cpu*/cpuidle/state{2,3}/disable
# persist across boots
sudo sed -i 's#^GRUB_CMDLINE_LINUX_DEFAULT=.*#GRUB_CMDLINE_LINUX_DEFAULT="processor.max_cstate=1"#' /etc/default/grub
sudo update-grub
```

**Observability (installed + enabled by `tmux/linux/install.sh`):**

| Unit | What |
|------|------|
| `crash-breadcrumb.service` | fsync's temp/power/load to `~/.tmux/crash-breadcrumb.log` every 20s — the last line is the box's state at the moment it died (the crash itself logs nothing). |
| `boot-notify.service` | On boot, posts to Slack `#nexus` (bridge-independent, via the Doppler bot token): previous uptime, downtime, clean vs unclean, and the last breadcrumb. Guarded by an uptime check so it only fires near a real boot. |

**Firmware / hardware (manual):**

- BIOS → **Power Supply Idle Control = Typical Current Idle** and **Global
  C-State Control = Disabled** (usually under *Advanced → AMD CBS*) — the
  firmware-grade version of the kernel param above.
- Check for a BIOS newer than the installed AMI build.
- A **UPS** is the definitive test/fix for the wall-power suspect.

> Update (2026-06-22): it pinged again *with this fix active* — so the C-state fix
> was **not** the cure. The breadcrumb line in the Slack notice still tells power vs.
> thermal at a glance; see [nexus-reboot-plan.md](./nexus-reboot-plan.md) for the
> current diagnosis and live mitigation (`cpu-boost-off.service`).

---

## What Moves to the Box

| What | How | Benefit |
|------|-----|---------|
| Docker stack (Ollama, Spark, dashboard, mnemon-flush) | Runs 24/7 via systemd | No start/stop when gaming |
| Tmux agent orchestration | SSH in, run `work` | Agents run even when PC is off |
| Scheduled agents | `claude /schedule` or cron triggers | Nightly PR reviews, health checks fire reliably |
| Spark webhook receiver | Cloudflare tunnel or Tailscale funnel | Auto-reindex on merge while AFK |
| Centralized MCP servers | Spark + mnemon as SSE servers | Any device connects to same memory + search |
| Transcript aggregation | All JSONL logs on one machine | Single source for agent history |
| Langfuse observability | `task langfuse:up` via compose profile | Trace all agent memory ops, accessible from any device |
| Git mirror / repo sync | Cron `git fetch --all` nightly | Spark indexes fresh personal code |

## What Stays on Client Machines

- Claude Code CLI (talks to remote MCP servers)
- IDE / editor
- Git repos you're actively editing
- Nothing running in Docker

---

## Open Questions / TODOs

- [ ] Evaluate Tailscale MagicDNS subdomains vs Caddy path routing
      (e.g. `spark.nexus.ts.net` instead of `<ip>:8343`)
- [ ] SSH over MCP for mnemon vs converting mnemon to SSE transport
      (SSE would remove the SSH dependency but needs more work in mnemon)
- [ ] `install.sh` root script — detect Linux and delegate to tmux/linux/install.sh
- [ ] Git clone strategy for repos on mini PC (bare clone + fetch, or full clones)
      Repos: cackalackycon, flashback-fleet, and personal projects only — no work repos
- [x] Disk sizing — 1TB NVMe, plenty for personal repos + Spark index + Langfuse volumes
- [ ] Auth on caddy — at minimum HTTP basic auth in front of dashboard + arbiter
      before exposing via Tailscale (Tailscale ACLs may be sufficient)
