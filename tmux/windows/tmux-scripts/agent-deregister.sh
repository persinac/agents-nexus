#!/usr/bin/env bash
# Remove this agent's registry entry when the pane dies.
# Usage: agent-deregister.sh <pane_id>
# Called from the tmux pane-died hook with #{pane_id}.

PANE_ID="${1}"
[ -z "$PANE_ID" ] && exit 0

# Log session_end before removing the registry entry (which has the CWD)
REGISTRY_FILE="$HOME/.tmux/registry/${PANE_ID}"
if [ -f "$REGISTRY_FILE" ]; then
  CWD=$(grep '^CWD=' "$REGISTRY_FILE" | cut -d= -f2)
  [ -n "$CWD" ] && "$HOME/.tmux/memory-hook.py" session_end "$PANE_ID" "$CWD" &
fi

rm -f "$HOME/.tmux/registry/${PANE_ID}"
