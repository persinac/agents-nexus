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

# Write machine-specific env (sourced by tmux scripts at runtime)
ENV_FILE="$HOME/.tmux/env.sh"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
REPO_DIR="\${REPO_DIR:-$HOME/repos}"
NOTES_DIR="\${NOTES_DIR:-$HOME/notes}"
AGENTS_NEXUS_DIR="\${AGENTS_NEXUS_DIR:-$NEXUS_DIR}"
EXTRA_REPO_DIRS="\${EXTRA_REPO_DIRS:-}"
VAULT_DIR="\${VAULT_DIR:-$HOME/vault}"
CHECKPOINT_DIR="\${CHECKPOINT_DIR:-$HOME/vault/Checkpoints}"
EOF
  echo "Created ~/.tmux/env.sh"
else
  grep -q "NOTES_DIR" "$ENV_FILE" || {
    echo "NOTES_DIR=\"\${NOTES_DIR:-\$HOME/notes}\"" >> "$ENV_FILE"
    echo "Added NOTES_DIR to ~/.tmux/env.sh"
  }
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
fi

# Install systemd user units
mkdir -p "$HOME/.config/systemd/user"
for unit in "$SCRIPT_DIR"/systemd/*.service "$SCRIPT_DIR"/systemd/*.timer; do
  [ -f "$unit" ] || continue
  name=$(basename "$unit")
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$NEXUS_DIR|g" \
    "$unit" > "$HOME/.config/systemd/user/$name"
  systemctl --user enable "$name" 2>/dev/null || true
  if [[ "$name" == *.timer ]]; then
    systemctl --user start "$name" 2>/dev/null || true
    echo "  Enabled + started systemd timer: $name"
  else
    echo "  Enabled systemd unit: $name"
  fi
done
systemctl --user daemon-reload

# Merge claude settings
mkdir -p "$HOME/.claude"
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

# Set up MCP config
setup_mcp_config() {
  local config_file="$HOME/.claude/claude_code_config.json"
  local spark_cmd
  spark_cmd=$(command -v spark 2>/dev/null || echo "")

  local agent_memory_dir="$NEXUS_DIR/mnemon"
  local agent_memory_python="$agent_memory_dir/.venv/bin/python3"

  if [ ! -x "$agent_memory_python" ]; then
    echo "  WARNING: mnemon venv not found at $agent_memory_python — run 'uv venv && uv pip install -e .' in mnemon/ first"
  fi

  local mcp_config
  if [ -n "$spark_cmd" ]; then
    mcp_config=$(cat <<MCPEOF
{
  "mcpServers": {
    "guilty-spark": {
      "command": "$spark_cmd",
      "args": ["serve"]
    },
    "agent-memory": {
      "command": "$agent_memory_python",
      "args": ["-m", "agent_memory.server.mcp_server"],
      "cwd": "$agent_memory_dir"
    }
  }
}
MCPEOF
)
  else
    mcp_config=$(cat <<MCPEOF
{
  "mcpServers": {
    "agent-memory": {
      "command": "$agent_memory_python",
      "args": ["-m", "agent_memory.server.mcp_server"],
      "cwd": "$agent_memory_dir"
    }
  }
}
MCPEOF
)
  fi

  if [ ! -f "$config_file" ]; then
    echo "$mcp_config" > "$config_file"
    echo "  Created ~/.claude/claude_code_config.json"
  else
    echo "  ~/.claude/claude_code_config.json already exists — skipping"
  fi
}
setup_mcp_config

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
