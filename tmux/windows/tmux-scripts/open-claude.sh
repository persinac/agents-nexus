#!/usr/bin/env bash
# Launch claude with recent checkpoint context injected as the opening message.
# Reads checkpoint notes from $NOTES_DIR for the current repo (past 3 days).
# Also registers this agent in the shared registry (~/.tmux/registry/) and
# injects a list of other active agents into the startup prompt so agents can
# use /msg <slot> to communicate with peers.
# Falls back to plain `claude` if no notes found and no peers are active.

NOTES_DIR="${NOTES_DIR:-$HOME/garner/notes}"
REPO_PATH="${PWD}"
project_slug="${PROJECT_SLUG:-$(basename "$REPO_PATH")}"

# ── Agent-memory Python (venv used by the MCP server) ──────────────────────
_AGENT_MEM_VENV="$HOME/garner/repos/agents-nexus/mnemon/.venv"
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
MY_NAME=$(tmux display-message -p '#W' 2>/dev/null)

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
  done < <(ls -1 "$NOTES_DIR"/*-${project_slug}-checkpoint.md 2>/dev/null | sort)
fi

# ── Build peer agent list (other registered agents) ────────────────────────
# Slot numbers are resolved live from tmux so renumber-windows doesn't stale them.
registry_section=""
if [ -d "$HOME/.tmux/registry" ]; then
  other_agents=""
  for f in "$HOME/.tmux/registry"/*; do
    [ -f "$f" ] || continue
    r_pane_id=$(grep '^PANE_ID=' "$f" | cut -d= -f2)
    r_name=$(grep '^NAME=' "$f" | cut -d= -f2)
    r_cwd=$(grep '^CWD=' "$f" | cut -d= -f2)
    [ "$r_pane_id" = "$MY_PANE_ID" ] && continue  # skip self
    r_slot=$(tmux display-message -t "$r_pane_id" -p '#{window_index}' 2>/dev/null)
    [ -z "$r_slot" ] && continue  # stale entry — pane gone
    other_agents+="  - Slot ${r_slot}: ${r_name} (${r_cwd})"$'\n'
  done
  if [ -n "$other_agents" ]; then
    registry_section="## Active Agents"$'\n'"Other agents you can reach with /msg <slot> <message>:"$'\n'"${other_agents}"
  fi
fi

# ── Query prior knowledge from memory store ────────────────────────────────
memory_section=""
if [ -x "$HOME/.tmux/memory-recall.py" ]; then
  memory_section=$("$MEMORY_PYTHON" "$HOME/.tmux/memory-recall.py" "$project_slug" 2>/dev/null || true)
fi

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
  exec claude "$prompt"
else
  exec claude
fi
