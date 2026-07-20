#!/usr/bin/env bash
# Linux install — mirrors mac/install.sh with systemd instead of launchd.
# Run from anywhere: bash tmux/linux/install.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NEXUS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MAC_DIR="$SCRIPT_DIR/../mac"

# Ensure ~/.tmux is owned by the current user (may be root-owned from prior sudo)
if [ -d "$HOME/.tmux" ] && [ "$(stat -c %u "$HOME/.tmux")" != "$(id -u)" ]; then
  sudo chown -R "$(id -u):$(id -g)" "$HOME/.tmux"
  echo "Fixed ~/.tmux ownership"
fi

# Symlink tmux config (identical to mac)
ln -sf "$MAC_DIR/tmux.conf" "$HOME/.tmux.conf"

# Symlink scripts — use mac versions by default, linux overrides where needed
mkdir -p "$HOME/.tmux"
for script in "$MAC_DIR"/tmux-scripts/*.sh "$MAC_DIR"/tmux-scripts/*.py; do
  [ -f "$script" ] || continue
  name=$(basename "$script")
  # Check for linux-specific override
  if [ -f "$SCRIPT_DIR/tmux-scripts/$name" ]; then
    chmod +x "$SCRIPT_DIR/tmux-scripts/$name"
    ln -sf "$SCRIPT_DIR/tmux-scripts/$name" "$HOME/.tmux/$name"
  else
    chmod +x "$script"
    ln -sf "$script" "$HOME/.tmux/$name"
  fi
done

# Linux-only scripts that have NO mac counterpart — the loop above only iterates mac scripts,
# so any such script needs this explicit pass. Currently none: boot-notify / crash-breadcrumb
# were folded into the shared tmux-scripts/ dir behind an $OSTYPE guard (they no-op on mac), so
# the first loop links them. This pass is kept for future genuinely-linux-only scripts and is a
# clean no-op when tmux/linux/tmux-scripts/ is empty (the [ -f ] guard skips the unexpanded glob).
for script in "$SCRIPT_DIR"/tmux-scripts/*.sh "$SCRIPT_DIR"/tmux-scripts/*.py; do
  [ -f "$script" ] || continue
  name=$(basename "$script")
  [ -f "$MAC_DIR/tmux-scripts/$name" ] && continue   # already linked via the override path above
  chmod +x "$script"
  ln -sf "$script" "$HOME/.tmux/$name"
done

# Auto-approve classifier venv — the permission-prompt gate (notify-classify.py)
# runs under this venv. hook-notification.sh skips the gate entirely when the venv
# is absent, so without this the read-only auto-approve silently never fires.
# Idempotent: only (re)builds when `import litellm` fails. Non-fatal — a failure
# just leaves the gate inert, which is the same as the pre-existing behavior.
CLASSIFY_VENV="$HOME/.tmux/.classify-venv"
CLASSIFY_PY="$CLASSIFY_VENV/bin/python"
if ! "$CLASSIFY_PY" -c "import litellm" >/dev/null 2>&1; then
  echo "Provisioning auto-approve classifier venv (~/.tmux/.classify-venv)..."
  if python3 -m venv "$CLASSIFY_VENV" >/dev/null 2>&1; then
    "$CLASSIFY_PY" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    if "$CLASSIFY_PY" -m pip install -q litellm >/dev/null 2>&1; then
      echo "  Installed litellm — read-only auto-approve gate is now live"
    else
      echo "  WARNING: litellm install failed — auto-approve gate stays inert (prompts fall through to Slack)"
    fi
  else
    echo "  WARNING: venv creation failed — on Debian/Ubuntu run 'sudo apt install python3-venv', then re-run this script. Gate stays inert until then."
  fi
else
  echo "Auto-approve classifier venv OK (litellm importable)"
fi

# Write machine-specific env (sourced by tmux scripts at runtime).
# NOTES_DIR is intentionally not seeded — open-claude.sh now resolves
# CHECKPOINT_DIR first and only falls back to NOTES_DIR for older installs.
ENV_FILE="$HOME/.tmux/env.sh"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
REPO_DIR="\${REPO_DIR:-$HOME/repos}"
AGENTS_NEXUS_DIR="\${AGENTS_NEXUS_DIR:-$NEXUS_DIR}"
EXTRA_REPO_DIRS="\${EXTRA_REPO_DIRS:-}"
VAULT_DIR="\${VAULT_DIR:-$HOME/vault}"
CHECKPOINT_DIR="\${CHECKPOINT_DIR:-$HOME/vault/Checkpoints}"
ANTHROPIC_BASE_URL="\${ANTHROPIC_BASE_URL:-http://localhost:4000}"
CLAUDE_MODEL="\${CLAUDE_MODEL:-claude-opus-4-8}"
CLAUDE_EFFORT="\${CLAUDE_EFFORT:-xhigh}"
# Inter-agent Slack bus (A2A): when 1, agent-send.sh routes non-local targets
# through the bridge (:8788/send). EXPORTED so it reaches agent-send.sh (an
# agent subprocess). Opt-in: default off; flip to 1 once the bridge bus is
# enabled (Doppler nexus/prd: SLACK_BUS_ENABLED=1 + SLACK_AGENTS_CHANNEL).
export SLACK_BUS_ENABLED="\${SLACK_BUS_ENABLED:-0}"
# Same-host A2A routing: 'local' (fast send-keys, default) or 'channel' (route
# NAME peers through the bus so they're buffered + delivered when the recipient
# is idle, never lost into a busy pane). Needs the bus enabled to do anything.
export SLACK_A2A_SAMEHOST="\${SLACK_A2A_SAMEHOST:-local}"
# Route the built-in SendMessage tool's peer messages through the bus (PreToolUse
# hook-sendmessage-bus.sh). Opt-in, off by default.
export SENDMESSAGE_BUS_ENABLED="\${SENDMESSAGE_BUS_ENABLED:-0}"
EOF
  echo "Created ~/.tmux/env.sh"
else
  grep -q "AGENTS_NEXUS_DIR" "$ENV_FILE" || {
    echo "AGENTS_NEXUS_DIR=\"\${AGENTS_NEXUS_DIR:-$NEXUS_DIR}\"" >> "$ENV_FILE"
    echo "Added AGENTS_NEXUS_DIR to ~/.tmux/env.sh"
  }
  grep -q "EXTRA_REPO_DIRS" "$ENV_FILE" || {
    echo "EXTRA_REPO_DIRS=\"\${EXTRA_REPO_DIRS:-}\"" >> "$ENV_FILE"
    echo "Added EXTRA_REPO_DIRS to ~/.tmux/env.sh"
  }
  grep -q "VAULT_DIR" "$ENV_FILE" || {
    echo "VAULT_DIR=\"\${VAULT_DIR:-\$HOME/vault}\"" >> "$ENV_FILE"
    echo "Added VAULT_DIR to ~/.tmux/env.sh"
  }
  grep -q "CHECKPOINT_DIR" "$ENV_FILE" || {
    echo "CHECKPOINT_DIR=\"\${CHECKPOINT_DIR:-\$HOME/vault/Checkpoints}\"" >> "$ENV_FILE"
    echo "Added CHECKPOINT_DIR to ~/.tmux/env.sh"
  }
  grep -q "ANTHROPIC_BASE_URL" "$ENV_FILE" || {
    echo "ANTHROPIC_BASE_URL=\"\${ANTHROPIC_BASE_URL:-http://localhost:4000}\"" >> "$ENV_FILE"
    echo "Added ANTHROPIC_BASE_URL to ~/.tmux/env.sh"
  }
  grep -q "CLAUDE_MODEL" "$ENV_FILE" || {
    echo "CLAUDE_MODEL=\"\${CLAUDE_MODEL:-claude-opus-4-8}\"" >> "$ENV_FILE"
    echo "Added CLAUDE_MODEL to ~/.tmux/env.sh"
  }
  grep -q "CLAUDE_EFFORT" "$ENV_FILE" || {
    echo "CLAUDE_EFFORT=\"\${CLAUDE_EFFORT:-xhigh}\"" >> "$ENV_FILE"
    echo "Added CLAUDE_EFFORT to ~/.tmux/env.sh"
  }
  grep -q "SLACK_BUS_ENABLED" "$ENV_FILE" || {
    echo "export SLACK_BUS_ENABLED=\"\${SLACK_BUS_ENABLED:-0}\"" >> "$ENV_FILE"
    echo "Added SLACK_BUS_ENABLED to ~/.tmux/env.sh"
  }
  grep -q "SLACK_A2A_SAMEHOST" "$ENV_FILE" || {
    echo "export SLACK_A2A_SAMEHOST=\"\${SLACK_A2A_SAMEHOST:-local}\"" >> "$ENV_FILE"
    echo "Added SLACK_A2A_SAMEHOST to ~/.tmux/env.sh"
  }
  grep -q "SENDMESSAGE_BUS_ENABLED" "$ENV_FILE" || {
    echo "export SENDMESSAGE_BUS_ENABLED=\"\${SENDMESSAGE_BUS_ENABLED:-0}\"" >> "$ENV_FILE"
    echo "Added SENDMESSAGE_BUS_ENABLED to ~/.tmux/env.sh"
  }
fi

# Enable systemd lingering so the user slice (and tmux, Claude, Docker, etc.)
# survives SSH disconnects. Without this, systemd tears down user-*.slice when
# the last login session ends, killing all background processes.
# $USER is not always exported (containers, some CI, cron) and this script runs under
# `set -u`, so fall back to `id -un`.
_user="${USER:-$(id -un)}"
if [[ "$(loginctl show-user "$_user" --property=Linger 2>/dev/null)" != "Linger=yes" ]]; then
  if sudo loginctl enable-linger "$_user" 2>/dev/null; then
    echo "Enabled systemd linger for $_user"
  else
    echo "  WARNING: enable-linger failed (no sudo, or no systemd) — background services won't persist across logout; harmless for a trial"
  fi
else
  echo "Systemd linger already enabled"
fi

# Install systemd user units
# Resolve real node binary path (fnm uses per-shell shims that systemd can't see)
NODE_BIN="$(readlink -f "$(command -v node 2>/dev/null)" 2>/dev/null || echo "/usr/bin/node")"
# Resolve doppler binary path. slack-bridge.service resolves SLACK_* through the secrets
# chain (scripts/secrets/secret-run.sh), which self-defaults to env,doppler(nexus/prd); the
# doppler backend reads this absolute path via Environment=DOPPLER_BIN (systemd's PATH won't
# find a shimmed CLI). Fall back to /usr/bin/doppler; if doppler is absent the unit still
# installs and the bridge boot-guards to a clean no-op (or set NEXUS_SECRETS_BACKENDS=env,aws-sm
# to source SLACK_* from AWS Secrets Manager instead).
DOPPLER_BIN="$(readlink -f "$(command -v doppler 2>/dev/null)" 2>/dev/null || echo "/usr/bin/doppler")"
[ -x "$DOPPLER_BIN" ] || echo "  WARNING: doppler CLI not found at $DOPPLER_BIN — slack-bridge will no-op until doppler is installed + authed, or set another secrets backend (NEXUS_SECRETS_BACKENDS)"

mkdir -p "$HOME/.config/systemd/user"
for unit in "$SCRIPT_DIR"/systemd/*.service "$SCRIPT_DIR"/systemd/*.timer; do
  [ -f "$unit" ] || continue
  name=$(basename "$unit")
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$NEXUS_DIR|g" -e "s|__NODE_BIN__|$NODE_BIN|g" -e "s|__DOPPLER_BIN__|$DOPPLER_BIN|g" \
    "$unit" > "$HOME/.config/systemd/user/$name"
  systemctl --user enable "$name" 2>/dev/null || true
  if [[ "$name" == *.timer ]]; then
    systemctl --user start "$name" 2>/dev/null || true
    echo "  Enabled + started systemd timer: $name"
  else
    echo "  Enabled systemd unit: $name"
  fi
done
systemctl --user daemon-reload 2>/dev/null || echo "  WARNING: 'systemctl --user' unavailable (no user systemd bus — e.g. a container or WSL without it); skipping unit reload"

# Crash-breadcrumb is a long-running logger (the unit loop only *enables* services);
# start it now so telemetry begins without waiting for a reboot. boot-notify is a
# boot-time oneshot — enabled above, it fires on the next boot (guarded against
# mid-session runs by an uptime check), so it is intentionally not started here.
systemctl --user restart crash-breadcrumb.service 2>/dev/null || true

# Slack bridge — install deps + start the long-running service now (the loop
# above only enables .service units; timers it starts). If SLACK_* tokens are
# unset in .env the bridge boot-guards to exit 0, so starting it is harmless.
if [ -d "$NEXUS_DIR/slack-bridge" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "  Installing slack-bridge dependencies..."
    ( cd "$NEXUS_DIR/slack-bridge" && npm install --silent ) || true
  else
    echo "  WARNING: npm not found — slack-bridge deps not installed"
  fi
  systemctl --user restart slack-bridge.service 2>/dev/null || true
  echo "  Started slack-bridge.service (no-ops if SLACK_* tokens are unset in .env)"
fi

# ── herdr base config (only when herdr is installed; a plain tmux-default box skips this) ──
# Deploys the plugin-free base UX config + symlinks the workspace category vocab. Plugins
# (e.g. the nexus-fleet repo picker) are OPT-IN via scripts/herdr-plugin-install.sh. The
# per-machine NEXUS_SUBSTRATE=herdr flip stays out of the repo (see docs/herdr-linux-setup.md).
if command -v herdr >/dev/null 2>&1 && [ -d "$SCRIPT_DIR/herdr" ]; then
  mkdir -p "$HOME/.config/herdr"
  # Resolve the real herdr binary path (the systemd herdr server has a stripped PATH,
  # so the arrow-focus command shims in config.toml need an absolute path).
  HERDR_BIN="$(readlink -f "$(command -v herdr 2>/dev/null)" 2>/dev/null || echo "$HOME/.local/bin/herdr")"
  # Base config is a per-machine copy (plugin-free) — install-plugin appends opt-in chords.
  sed -e "s|__HOME__|$HOME|g" -e "s|__HERDR_BIN__|$HERDR_BIN|g" "$SCRIPT_DIR/herdr/config.toml" > "$HOME/.config/herdr/config.toml"
  # Re-append keybindings for any already-linked bundled plugin (base re-run preserves opt-in chords).
  for _pdir in "$NEXUS_DIR"/plugins/*/; do
    [ -f "$_pdir/herdr-plugin.toml" ] || continue
    _pid="$(sed -n 's/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$_pdir/herdr-plugin.toml" | head -1)"
    case "$(herdr plugin list --json 2>/dev/null || true)" in
      *"\"$_pid\""*) "$NEXUS_DIR/scripts/herdr-plugin-install.sh" "$(basename "$_pdir")" || true ;;
    esac
  done
  [ -f "$NEXUS_DIR/config/workspace-categories.txt" ] && \
    ln -sfn "$NEXUS_DIR/config/workspace-categories.txt" "$HOME/.tmux/workspace-categories.txt"
  herdr server reload-config >/dev/null 2>&1 || true
  echo "Installed herdr base config (~/.config/herdr/) + workspace-categories.txt — plugins opt-in: scripts/herdr-plugin-install.sh <name>"
fi

# Merge claude settings
mkdir -p "$HOME/.claude"

# Claude hooks — auto-checkpoint (Stop hook: background, selective memory note)
mkdir -p "$HOME/.claude/hooks" "$HOME/.claude/auto-checkpoint"
if [ -f "$MAC_DIR/claude-hooks/auto-checkpoint.sh" ]; then
  chmod +x "$MAC_DIR/claude-hooks/auto-checkpoint.sh"
  ln -sf "$MAC_DIR/claude-hooks/auto-checkpoint.sh" "$HOME/.claude/hooks/auto-checkpoint.sh"
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$NEXUS_DIR|g" \
    "$MAC_DIR/claude-hooks/auto-checkpoint-mcp.json" > "$HOME/.claude/auto-checkpoint/mcp.json"
  echo "Installed auto-checkpoint hook (~/.claude/hooks/auto-checkpoint.sh)"
fi

SETTINGS="$HOME/.claude/settings.json"
TEMPLATE="$MAC_DIR/claude-settings.json"

if [ ! -f "$SETTINGS" ]; then
  cp "$TEMPLATE" "$SETTINGS"
  echo "Created ~/.claude/settings.json from template"
else
  cp "$SETTINGS" "${SETTINGS}.bak"
  [ -L "$SETTINGS" ] && cp --remove-destination "$(readlink "$SETTINGS")" "$SETTINGS"

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
  echo "Merged claude-settings.json into ~/.claude/settings.json (backup at settings.json.bak)"
fi

# Merge MCP servers into ~/.claude/settings.json
# Both Spark and agent-memory run as always-on Docker services with SSE transport.

# Probe an HTTP(S) endpoint and report reachability without blocking on SSE
# connections (which would never close on their own). Returns 0 if the server
# answered 200, 1 otherwise.
check_mcp_endpoint() {
  local url=$1
  local name=$2
  local code
  # curl writes the HTTP code before --max-time fires; on connection failure it
  # writes "000". The trailing `|| true` keeps a non-zero curl exit (e.g. 28 on
  # SSE timeout) from triggering set -e. The default-fallback handles the rare
  # case where curl produces no output at all.
  code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 2 "$url" 2>/dev/null || true)
  code="${code:-000}"
  if [[ "$code" == 2* ]]; then
    echo "  [ok]   $name reachable at $url (HTTP $code)"
    return 0
  else
    echo "  [warn] $name NOT reachable at $url (HTTP $code) — config will still be written"
    echo "         Start the agents-nexus stack with: cd $NEXUS_DIR && docker compose up -d"
    return 1
  fi
}

setup_mcp_config() {
  local settings_file="$HOME/.claude/settings.json"

  # Verify the docker MCP services are actually up before wiring them in.
  echo "Checking agents-nexus MCP service reachability..."
  check_mcp_endpoint "http://localhost:8343/sse" "Spark MCP" || true
  check_mcp_endpoint "http://localhost:8330/sse" "agent-memory MCP" || true

  # On Mac, Spark is a CLI binary — use stdio. On Linux, both are Docker SSE.
  local spark_cmd
  spark_cmd=$(command -v spark 2>/dev/null || echo "")

  cp "$settings_file" "${settings_file}.mcp-bak"
  node - "$settings_file" "$spark_cmd" <<'NODEOF'
const [,, settingsPath, sparkCmd] = process.argv;
const fs = require('fs');
const cfg = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
cfg.mcpServers ??= {};
if (sparkCmd) {
  cfg.mcpServers.spark = { command: sparkCmd, args: ["serve"] };
} else {
  cfg.mcpServers.spark = { type: "sse", url: "http://localhost:8343/sse" };
}
cfg.mcpServers["agent-memory"] = { type: "sse", url: "http://localhost:8330/sse" };
fs.writeFileSync(settingsPath, JSON.stringify(cfg, null, 2) + '\n');
NODEOF
  echo "  Merged MCP servers (spark + agent-memory) into ~/.claude/settings.json"
}
setup_mcp_config

# Symlink skills to ~/.claude/skills so they're available in all projects
mkdir -p "$HOME/.claude/skills"
for skill_dir in "$NEXUS_DIR"/skills/*/; do
  [ -d "$skill_dir" ] || continue
  name=$(basename "$skill_dir")
  ln -sf "$skill_dir" "$HOME/.claude/skills/$name"
  echo "  Linked skill: $name"
done

# Symlink commands to ~/.claude/commands (e.g. opsx slash commands)
mkdir -p "$HOME/.claude/commands"
for cmd_dir in "$NEXUS_DIR"/commands/*/; do
  [ -d "$cmd_dir" ] || continue
  name=$(basename "$cmd_dir")
  ln -sf "$cmd_dir" "$HOME/.claude/commands/$name"
  echo "  Linked command: $name"
done

# Install OpenSpec CLI if not present
if ! command -v openspec &>/dev/null; then
  if command -v npm &>/dev/null; then
    npm install -g @fission-ai/openspec@latest || echo "  WARNING: OpenSpec install failed (optional) — skipping; run 'npm install -g @fission-ai/openspec' later if you want it"
    echo "  Installed OpenSpec CLI"
  else
    echo "  WARNING: npm not found — install OpenSpec manually: npm install -g @fission-ai/openspec"
  fi
else
  echo "  OpenSpec CLI already installed: $(openspec --version 2>/dev/null || echo 'ok')"
fi

# Codex multi-vendor (Tier 5): mirror the fleet's Claude skills into codex so codex
# sessions/agents inherit them (idempotent symlinks; no-ops without codex installed).
if command -v codex >/dev/null 2>&1 && [ -x "$NEXUS_DIR/scripts/sync-codex-skills.sh" ]; then
  echo "Syncing fleet skills -> codex..."
  "$NEXUS_DIR/scripts/sync-codex-skills.sh" || echo "  (codex skill sync failed — non-fatal)"
else
  echo "  codex not on PATH — skipping codex skill sync"
fi

# Source bashrc
MARKER="# agent-orchestration"
if ! grep -qF "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
  echo "" >> "$HOME/.bashrc"
  echo "$MARKER" >> "$HOME/.bashrc"
  echo "source \"$SCRIPT_DIR/bashrc\"" >> "$HOME/.bashrc"
  echo "Added source line to ~/.bashrc"
else
  echo "~/.bashrc already sources agent-orchestration"
fi

echo ""
echo "Done. Next steps:"
echo "  source ~/.bashrc"
echo "  tmux source ~/.tmux.conf"
echo "  work    # attach to agents session"
