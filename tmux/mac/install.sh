#!/usr/bin/env bash
# Symlinks config files from this repo into their expected locations.
# Run from the repo root: ./mac/install.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ln -sf "$SCRIPT_DIR/tmux.conf" "$HOME/.tmux.conf"

mkdir -p "$HOME/.tmux"
for script in "$SCRIPT_DIR"/tmux-scripts/*.sh; do
  chmod +x "$script"
  ln -sf "$script" "$HOME/.tmux/$(basename "$script")"
done
for script in "$SCRIPT_DIR"/tmux-scripts/*.py; do
  [ -f "$script" ] || continue
  chmod +x "$script"
  ln -sf "$script" "$HOME/.tmux/$(basename "$script")"
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
    echo "  WARNING: venv creation failed (python3 -m venv) — gate stays inert until resolved"
  fi
else
  echo "Auto-approve classifier venv OK (litellm importable)"
fi

# Launchd agents — substitute __HOME__ and __AGENTS_NEXUS_DIR__ placeholders
NEXUS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
mkdir -p "$HOME/Library/LaunchAgents"
for plist in "$SCRIPT_DIR"/launchd/*.plist; do
  [ -f "$plist" ] || continue
  name=$(basename "$plist")
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$NEXUS_DIR|g" \
    "$plist" > "$HOME/Library/LaunchAgents/$name"
  launchctl unload "$HOME/Library/LaunchAgents/$name" 2>/dev/null || true
  launchctl load "$HOME/Library/LaunchAgents/$name"
  echo "  Loaded launchd: $name"
done

# Write machine-specific env (sourced by tmux scripts at runtime)
ENV_FILE="$HOME/.tmux/env.sh"
REPO_DIR_DEFAULT="$HOME/repos"
if [ ! -f "$ENV_FILE" ]; then
  echo "REPO_DIR=\"\${REPO_DIR:-$REPO_DIR_DEFAULT}\"" > "$ENV_FILE"
  echo "NOTES_DIR=\"\${NOTES_DIR:-\$HOME/notes}\"" >> "$ENV_FILE"
  echo "AGENTS_NEXUS_DIR=\"\${AGENTS_NEXUS_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}\"" >> "$ENV_FILE"
  echo "CLAUDE_MODEL=\"\${CLAUDE_MODEL:-claude-opus-4-8}\"" >> "$ENV_FILE"
  echo "Created ~/.tmux/env.sh (edit REPO_DIR/NOTES_DIR/AGENTS_NEXUS_DIR if your paths differ)"
else
  # Add NOTES_DIR if missing
  if ! grep -q "NOTES_DIR" "$ENV_FILE"; then
    echo "NOTES_DIR=\"\${NOTES_DIR:-\$HOME/notes}\"" >> "$ENV_FILE"
    echo "Added NOTES_DIR to ~/.tmux/env.sh"
  fi
  # Add AGENTS_NEXUS_DIR if missing
  if ! grep -q "AGENTS_NEXUS_DIR" "$ENV_FILE"; then
    echo "AGENTS_NEXUS_DIR=\"\${AGENTS_NEXUS_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}\"" >> "$ENV_FILE"
    echo "Added AGENTS_NEXUS_DIR to ~/.tmux/env.sh"
  fi
  # Add EXTRA_REPO_DIRS if missing
  if ! grep -q "EXTRA_REPO_DIRS" "$ENV_FILE"; then
    echo "EXTRA_REPO_DIRS=\"\${EXTRA_REPO_DIRS:-\$HOME/projects}\"" >> "$ENV_FILE"
    echo "Added EXTRA_REPO_DIRS to ~/.tmux/env.sh"
  else
    echo "~/.tmux/env.sh already exists — verify REPO_DIR/NOTES_DIR/EXTRA_REPO_DIRS are correct"
  fi
  if ! grep -q "CHECKPOINT_DIR" "$ENV_FILE"; then
    echo "CHECKPOINT_DIR=\"\${CHECKPOINT_DIR:-\$HOME/vault/Checkpoints}\"" >> "$ENV_FILE"
    echo "Added CHECKPOINT_DIR to ~/.tmux/env.sh"
  fi
  # Add CLAUDE_MODEL if missing
  if ! grep -q "CLAUDE_MODEL" "$ENV_FILE"; then
    echo "CLAUDE_MODEL=\"\${CLAUDE_MODEL:-claude-opus-4-8}\"" >> "$ENV_FILE"
    echo "Added CLAUDE_MODEL to ~/.tmux/env.sh"
  fi
fi

mkdir -p "$HOME/.claude"

# Claude hooks — auto-checkpoint (Stop hook: background, selective memory note)
mkdir -p "$HOME/.claude/hooks" "$HOME/.claude/auto-checkpoint"
if [ -f "$SCRIPT_DIR/claude-hooks/auto-checkpoint.sh" ]; then
  chmod +x "$SCRIPT_DIR/claude-hooks/auto-checkpoint.sh"
  ln -sf "$SCRIPT_DIR/claude-hooks/auto-checkpoint.sh" "$HOME/.claude/hooks/auto-checkpoint.sh"
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$NEXUS_DIR|g" \
    "$SCRIPT_DIR/claude-hooks/auto-checkpoint-mcp.json" > "$HOME/.claude/auto-checkpoint/mcp.json"
  echo "Installed auto-checkpoint hook (~/.claude/hooks/auto-checkpoint.sh)"
fi

SETTINGS="$HOME/.claude/settings.json"
TEMPLATE="$SCRIPT_DIR/claude-settings.json"

if [ ! -f "$SETTINGS" ]; then
  cp "$TEMPLATE" "$SETTINGS"
  echo "Created ~/.claude/settings.json from template"
else
  # Back up first
  cp "$SETTINGS" "${SETTINGS}.bak"

  # If it's a symlink, unlink it so we can write a real merged file
  if [ -L "$SETTINGS" ]; then
    cp --remove-destination "$(readlink "$SETTINGS")" "$SETTINGS"
  fi

  # Smart merge: add repo hooks (dedup by command) + union permissions
  node - "$SETTINGS" "$TEMPLATE" <<'EOF'
const [,, existingPath, templatePath] = process.argv;
const existing = JSON.parse(require('fs').readFileSync(existingPath, 'utf8'));
const template = JSON.parse(require('fs').readFileSync(templatePath, 'utf8'));

// Merge hooks: for each event in template, append entries whose command isn't already present
for (const [event, entries] of Object.entries(template.hooks ?? {})) {
  existing.hooks ??= {};
  existing.hooks[event] ??= [];
  for (const entry of entries) {
    const cmd = entry.hooks?.[0]?.command;
    const alreadyPresent = existing.hooks[event].some(e => e.hooks?.[0]?.command === cmd);
    if (!alreadyPresent) existing.hooks[event].push(entry);
  }
}

// Union permissions.allow
const existingPerms = existing.permissions?.allow ?? [];
const templatePerms = template.permissions?.allow ?? [];
existing.permissions ??= {};
existing.permissions.allow = [...new Set([...existingPerms, ...templatePerms])];

require('fs').writeFileSync(existingPath, JSON.stringify(existing, null, 2) + '\n');
EOF

  echo "Merged claude-settings.json into ~/.claude/settings.json (backup at settings.json.bak)"
fi

# Append zshrc sourcing if not already present
MARKER="# agent-orchestration"
if ! grep -qF "$MARKER" "$HOME/.zshrc" 2>/dev/null; then
  echo "" >> "$HOME/.zshrc"
  echo "$MARKER" >> "$HOME/.zshrc"
  echo "source \"$SCRIPT_DIR/zshrc\"" >> "$HOME/.zshrc"
  echo "Added source line to ~/.zshrc"
else
  echo "~/.zshrc already sources agent-orchestration"
fi

setup_mcp_config() {
  local template="$SCRIPT_DIR/claude-code-config.json"
  if [ ! -f "$template" ]; then
    echo "  WARNING: claude-code-config.json template not found, skipping MCP config"
    return
  fi

  local config_file="$HOME/.claude/claude_code_config.json"

  # Auto-detect spark binary
  local spark_cmd
  spark_cmd=$(command -v spark 2>/dev/null || echo "/usr/local/bin/spark")

  # Auto-detect agent-memory dir by searching common locations
  local agent_memory_dir=""
  local search_paths=(
    "$HOME/minions/minions-suite/agent-memory"
    "$HOME/garner/repos/minions-suite/agent-memory"
    "$HOME/repos/minions-suite/agent-memory"
    "$HOME/projects/minions-suite/agent-memory"
  )
  for p in "${search_paths[@]}"; do
    if [ -d "$p" ]; then
      agent_memory_dir="$p"
      break
    fi
  done

  if [ -z "$agent_memory_dir" ]; then
    echo ""
    echo "  agent-memory dir not auto-detected."
    read -r -p "  Enter path to agent-memory dir (blank to skip): " agent_memory_dir
    agent_memory_dir="${agent_memory_dir/#\~/$HOME}"
    if [ -z "$agent_memory_dir" ]; then
      echo "  Skipping agent-memory MCP config."
      return
    fi
  fi

  local agent_memory_python="$agent_memory_dir/.venv/bin/python3"

  local tmp_file="${config_file}.new"
  sed \
    -e "s|__SPARK_CMD__|$spark_cmd|g" \
    -e "s|__AGENT_MEMORY_PYTHON__|$agent_memory_python|g" \
    -e "s|__AGENT_MEMORY_DIR__|$agent_memory_dir|g" \
    "$template" > "$tmp_file"

  if [ ! -f "$config_file" ]; then
    mv "$tmp_file" "$config_file"
    echo "  Created ~/.claude/claude_code_config.json"
  else
    # Merge: add missing servers, leave existing ones untouched
    node - "$config_file" "$tmp_file" <<'EOF'
const fs = require('fs');
const [,, existingPath, incomingPath] = process.argv;
const existing = JSON.parse(fs.readFileSync(existingPath, 'utf8'));
const incoming = JSON.parse(fs.readFileSync(incomingPath, 'utf8'));
existing.mcpServers ??= {};
for (const [name, cfg] of Object.entries(incoming.mcpServers ?? {})) {
  if (!existing.mcpServers[name]) {
    existing.mcpServers[name] = cfg;
    process.stderr.write(`  Added MCP server: ${name}\n`);
  } else {
    process.stderr.write(`  [ok] MCP server: ${name} (already configured)\n`);
  }
}
fs.writeFileSync(existingPath, JSON.stringify(existing, null, 2) + '\n');
EOF
    rm "$tmp_file"
    echo "  Updated ~/.claude/claude_code_config.json"
  fi
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
    npm install -g @fission-ai/openspec@latest
    echo "  Installed OpenSpec CLI"
  else
    echo "  WARNING: npm not found — install OpenSpec manually: npm install -g @fission-ai/openspec"
  fi
else
  echo "  OpenSpec CLI already installed: $(openspec --version 2>/dev/null || echo 'ok')"
fi

echo "Done. Reload with: tmux source ~/.tmux.conf"
