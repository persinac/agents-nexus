#!/usr/bin/env bash
# Launch claude with recent checkpoint context injected as the opening message.
# Reads checkpoint notes from $CHECKPOINT_DIR (or legacy $NOTES_DIR) for the current repo (past 3 days).
# Also registers this agent in the shared registry (~/.tmux/registry/) and
# injects agent communication tool instructions into the startup prompt.
# Falls back to plain `claude` if no notes found.

[ -f "$HOME/.tmux/env.sh" ] && source "$HOME/.tmux/env.sh"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-http://localhost:4000}"

# Resolve checkpoint source — prefer CHECKPOINT_DIR (matches the writer skill);
# fall back to NOTES_DIR for back-compat with older env.sh installs.
CHECKPOINT_SRC="${CHECKPOINT_DIR:-${NOTES_DIR:-$HOME/vault/Checkpoints}}"
REPO_PATH="${PWD}"
project_slug="${PROJECT_SLUG:-$(basename "$REPO_PATH")}"

# ── Agent-memory Python (venv used by the MCP server) ──────────────────────
_AGENT_MEM_VENV="${AGENTS_NEXUS_DIR:-$HOME/repos/agents-nexus}/mnemon/.venv"
if [ -x "$_AGENT_MEM_VENV/bin/python3" ]; then
  MEMORY_PYTHON="$_AGENT_MEM_VENV/bin/python3"
elif [ -x "$_AGENT_MEM_VENV/Scripts/python3.exe" ]; then
  MEMORY_PYTHON="$_AGENT_MEM_VENV/Scripts/python3.exe"
else
  MEMORY_PYTHON="python3"
fi

# ── Register this agent in the shared registry ─────────────────────────────
# Files are keyed by pane ID (stable — unaffected by renumber-windows).
MY_PANE_ID="$TMUX_PANE"
MY_SLOT=$(tmux display-message -p '#{window_index}' 2>/dev/null)
MY_NAME="$project_slug"

# Lock the window name so tmux doesn't override it with the process name
tmux rename-window -t "$MY_PANE_ID" "$MY_NAME" 2>/dev/null
tmux set-window-option -t "$MY_PANE_ID" automatic-rename off 2>/dev/null

if [ -n "$MY_PANE_ID" ] && [ -n "$MY_SLOT" ]; then
  mkdir -p "$HOME/.tmux/registry"
  printf 'SLOT=%s\nNAME=%s\nCWD=%s\nAT=%s\nPANE_ID=%s\n' \
    "$MY_SLOT" "$MY_NAME" "$REPO_PATH" "$(date +%s)" "$MY_PANE_ID" \
    > "$HOME/.tmux/registry/${MY_PANE_ID}"
  # Log session_start to memory buffer
  "$HOME/.tmux/memory-hook.py" session_start "$MY_PANE_ID" "$REPO_PATH" &
fi

# BSD date (Mac): -v-3d; GNU date (Linux): -d '3 days ago'
cutoff=$(date -v-3d +%Y-%m-%d 2>/dev/null || date -d '3 days ago' +%Y-%m-%d)

# Collect matching checkpoint files from the past 3 days, sorted oldest→newest
context=""
while IFS= read -r f; do
  date_part=$(basename "$f" | cut -c1-10)
  if [[ "$date_part" > "$cutoff" || "$date_part" = "$cutoff" ]]; then
    context+="$(cat "$f")"$'\n\n'
  fi
done < <(ls -1 "$CHECKPOINT_SRC"/*-${project_slug}-checkpoint.md 2>/dev/null | sort)

# ── Agent communication tools ─────────────────────────────────────────────
REGISTRY_SCRIPT="$HOME/.tmux/agent-registry.sh"
SEND_SCRIPT="$HOME/.tmux/agent-send.sh"
registry_section=""
if [ -x "$REGISTRY_SCRIPT" ]; then
  peers_output=$("$REGISTRY_SCRIPT" peers --exclude "$MY_PANE_ID" 2>/dev/null || true)
  registry_section="## Agent Communication
You are part of a multi-agent system. ALWAYS pass --exclude ${MY_PANE_ID} to avoid listing yourself.
  - \`$REGISTRY_SCRIPT peers --exclude ${MY_PANE_ID}\` — list all active agents
  - \`$SEND_SCRIPT <slot_or_name> <message>\` — send a message to a specific agent
  - \`$REGISTRY_SCRIPT broadcast --exclude ${MY_PANE_ID} <message>\` — send to ALL other agents
  - \`$REGISTRY_SCRIPT whoami --exclude ${MY_PANE_ID}\` — show your own slot, name, and directory

### Current Peers
\`\`\`
${peers_output}
\`\`\`
Re-run peers before messaging to get up-to-date slot numbers."
fi

# ── Query prior knowledge from memory store ────────────────────────────────
memory_section=""
if [ -x "$HOME/.tmux/memory-recall.py" ]; then
  memory_section=$("$MEMORY_PYTHON" "$HOME/.tmux/memory-recall.py" "$project_slug" 2>/dev/null || true)
fi

# ── Build claude args ──────────────────────────────────────────────────────
claude_args=()
[ -n "$MY_NAME" ]       && claude_args+=(--name "$MY_NAME")
[ -n "$CLAUDE_MODEL" ]  && claude_args+=(--model "$CLAUDE_MODEL")
[ -n "$CLAUDE_EFFORT" ] && claude_args+=(--effort "$CLAUDE_EFFORT")

# ── Launch claude with assembled context ───────────────────────────────────
if [ -n "$context" ] || [ -n "$registry_section" ] || [ -n "$memory_section" ]; then
  prompt=""
  if [ -n "$context" ]; then
    prompt="Here are recent checkpoint notes for this project (past 3 days). Please review them to get up to speed before we begin:"$'\n\n'"${context}"
  fi
  if [ -n "$memory_section" ]; then
    [ -n "$prompt" ] && prompt="${prompt}"$'\n\n'
    prompt="${prompt}${memory_section}"
  fi
  if [ -n "$registry_section" ]; then
    [ -n "$prompt" ] && prompt="${prompt}"$'\n\n'
    prompt="${prompt}${registry_section}"
  fi
  exec claude "${claude_args[@]}" "$prompt"
else
  exec claude "${claude_args[@]}"
fi
