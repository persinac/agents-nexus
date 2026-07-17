#!/usr/bin/env bash
# Launch claude with recent checkpoint context injected as the opening message.
# Reads checkpoint notes from $CHECKPOINT_DIR (or legacy $NOTES_DIR) for the current repo (past 3 days).
# Also registers this agent in the shared registry (~/.tmux/registry/) and
# injects agent communication tool instructions into the startup prompt.
# Falls back to plain `claude` if no notes found.

# Resolve checkpoint source — prefer CHECKPOINT_DIR (matches the writer skill);
# fall back to NOTES_DIR for back-compat with older env.sh installs.
CHECKPOINT_SRC="${CHECKPOINT_DIR:-${NOTES_DIR:-$HOME/vault/Checkpoints}}"
REPO_PATH="${PWD}"
project_slug="${PROJECT_SLUG:-$(basename "$REPO_PATH")}"

# ── Agent-memory Python (venv used by the MCP server) ──────────────────────
_AGENT_MEM_VENV="${AGENTS_NEXUS_DIR:-/c/projects/agents-nexus}/mnemon/.venv"
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

# ── Tag LLM traffic so Langfuse names the trace after this window ───────────
# The proxy reads a `sess/<name>/` path prefix and uses it as the trace name +
# session id; without it every agent shows up as "claude-code". Slugify to a
# URL-path-safe segment (the proxy splits the prefix on the first "/").
# Only appends when a base URL is set (Windows may run claude direct, no proxy).
if [ -n "${ANTHROPIC_BASE_URL:-}" ] && [ -n "$MY_NAME" ]; then
  _sess_slug=$(printf '%s' "$MY_NAME" | tr -c 'A-Za-z0-9._-' '-')
  export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/}/sess/${_sess_slug}"
fi

if [ -n "$MY_PANE_ID" ] && [ -n "$MY_SLOT" ]; then
  mkdir -p "$HOME/.tmux/registry"
  printf 'SLOT=%s\nNAME=%s\nCWD=%s\nAT=%s\nPANE_ID=%s\n' \
    "$MY_SLOT" "$MY_NAME" "$REPO_PATH" "$(date +%s)" "$MY_PANE_ID" \
    > "$HOME/.tmux/registry/${MY_PANE_ID}"
  # Log session_start to memory buffer
  "$HOME/.tmux/memory-hook.py" session_start "$MY_PANE_ID" "$REPO_PATH" &
fi

# GNU date (MSYS2/Linux): -d '3 days ago'
cutoff=$(date -d '3 days ago' +%Y-%m-%d 2>/dev/null)

# Collect matching checkpoint files from the past 3 days, sorted oldest→newest
context=""
if [ -n "$cutoff" ]; then
  while IFS= read -r f; do
    date_part=$(basename "$f" | cut -c1-10)
    if [[ "$date_part" > "$cutoff" || "$date_part" = "$cutoff" ]]; then
      context+="$(cat "$f")"$'\n\n'
    fi
  done < <(ls -1 "$CHECKPOINT_SRC"/*-${project_slug}-checkpoint.md 2>/dev/null | sort)
fi

# ── Auto-cache recovery (previous session context) ───────────────────────
CACHE_FILE="$HOME/.tmux/cache/${project_slug}.md"
cache_section=""
if [ -f "$CACHE_FILE" ]; then
  cache_age=$(( $(date +%s) - $(stat -c %Y "$CACHE_FILE" 2>/dev/null || stat -f %m "$CACHE_FILE" 2>/dev/null || echo 0) ))
  if [ "$cache_age" -lt 86400 ]; then
    cache_section=$(cat "$CACHE_FILE")
    mv "$CACHE_FILE" "${CACHE_FILE%.md}.used" 2>/dev/null
  else
    rm -f "$CACHE_FILE"
  fi
fi

# ── Agent communication tools ─────────────────────────────────────────────
REGISTRY_SCRIPT="$HOME/.tmux/agent-registry.sh"
SEND_SCRIPT="$HOME/.tmux/agent-send.sh"
registry_section=""
if [ -x "$REGISTRY_SCRIPT" ]; then
  BASH_EXE="/c/msys64/usr/bin/bash.exe"
  peers_output=$($BASH_EXE "$REGISTRY_SCRIPT" peers --exclude "$MY_PANE_ID" 2>/dev/null || true)
  # Is the Slack agent bus live? Probe the local bridge's health (cheap, <1s) so we
  # document the bus as the DEFAULT transport only when it can actually deliver.
  # `--via-slack` force-routes through the bridge regardless of the agent's
  # SLACK_BUS_ENABLED, so the bridge's own bus state — not an env var — is the
  # right signal. No bridge / bus off -> fall back to the tmux-only guidance.
  bus_on=0
  if command -v curl >/dev/null 2>&1; then
    case "$(curl -s --max-time 1 "http://127.0.0.1:${SLACK_BRIDGE_PORT:-8788}/health" 2>/dev/null)" in
      *'"bus":true'*) bus_on=1 ;;
    esac
  fi
  if [ "$bus_on" = "1" ]; then
    comms_body="**To message another agent, DEFAULT to the Slack agent bus (\`#nexus-agents\`).** Address the recipient by NAME — post once and the orchestrator delivers it idle-gated, buffered, and audited (and it reaches agents on other hosts):
  - \`$BASH_EXE $SEND_SCRIPT --via-slack <name> <message>\` — post to the bus; delivered to <name> when it next goes idle

Discovery (read-only — find who to address, then message them over the bus):
  - \`$BASH_EXE $REGISTRY_SCRIPT peers --exclude ${MY_PANE_ID}\` — list all active agents (slot, name, directory)
  - \`$BASH_EXE $REGISTRY_SCRIPT whoami --exclude ${MY_PANE_ID}\` — show your own slot, name, and directory

Fallback — direct tmux send (same-host only; NOT durable or auditable and can be missed). Prefer the bus; only use this for a local/ephemeral ping, and say that you did:
  - \`$BASH_EXE $SEND_SCRIPT <slot_or_name> <message>\` — send-keys straight to a same-host agent
  - \`$BASH_EXE $REGISTRY_SCRIPT broadcast --exclude ${MY_PANE_ID} <message>\` — send to ALL other agents at once"
  else
    comms_body="To message another agent:
  - \`$BASH_EXE $REGISTRY_SCRIPT peers --exclude ${MY_PANE_ID}\` — list all active agents
  - \`$BASH_EXE $SEND_SCRIPT <slot_or_name> <message>\` — send a message to a specific agent
  - \`$BASH_EXE $REGISTRY_SCRIPT broadcast --exclude ${MY_PANE_ID} <message>\` — send to ALL other agents
  - \`$BASH_EXE $REGISTRY_SCRIPT whoami --exclude ${MY_PANE_ID}\` — show your own slot, name, and directory"
  fi
  registry_section="## Agent Communication
You are part of a multi-agent system (agents may run across multiple hosts). ALWAYS pass --exclude ${MY_PANE_ID} to avoid listing yourself.

${comms_body}

### Current Peers
\`\`\`
${peers_output}
\`\`\`
Re-run peers before messaging to get up-to-date slot numbers; prefer name-based addressing over slot numbers."
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

# ── Pre-accept workspace trust for this launch dir ─────────────────────────
# Claude Code gates a per-directory trust dialog on
#   ~/.claude.json → projects["<dir>"].hasTrustDialogAccepted
# There is no global "trust all" switch, and a trusted parent does not reliably
# cascade, so every untrusted dir — including each fresh git worktree — prompts
# on launch. Seed the flag for THIS dir just before exec so the dialog never
# fires. flock guards concurrent fleet launches racing on the shared JSON
# (git-bash lacks flock — falls back to an unlocked write there).
seed_trust() {
  command -v jq >/dev/null 2>&1 || return 0
  local cfg="$HOME/.claude.json" tmp
  { [ -f "$cfg" ] && jq -e . "$cfg" >/dev/null 2>&1; } || return 0
  tmp="$(mktemp "${TMPDIR:-/tmp}/claude-trust.XXXXXX")" || return 0
  if jq --arg d "$REPO_PATH" '(.projects[$d].hasTrustDialogAccepted) = true' "$cfg" >"$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    mv "$tmp" "$cfg"
  else
    rm -f "$tmp"
  fi
}
if command -v flock >/dev/null 2>&1; then
  ( flock 200; seed_trust ) 200>"$HOME/.claude.json.lock"
else
  seed_trust
fi

# ── Launch claude with assembled context ───────────────────────────────────
if [ -n "$cache_section" ] || [ -n "$context" ] || [ -n "$registry_section" ] || [ -n "$memory_section" ]; then
  prompt=""
  if [ -n "$cache_section" ]; then
    prompt="Your previous session was interrupted. Here is the working context from that session — use it to pick up where you left off:"$'\n\n'"${cache_section}"
  fi
  if [ -n "$context" ]; then
    [ -n "$prompt" ] && prompt="${prompt}"$'\n\n'
    prompt="${prompt}Here are recent checkpoint notes for this project (past 3 days). Please review them to get up to speed before we begin:"$'\n\n'"${context}"
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
