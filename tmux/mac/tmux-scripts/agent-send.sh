#!/usr/bin/env bash
# Send a message from one agent to another via tmux.
# Usage: agent-send.sh <slot_or_name> <message>
# Accepts a slot number (fast path) or an agent name (registry lookup).

TARGET="${1:?"Usage: agent-send.sh <slot_or_name> <message>"}"
shift
MSG="$*"
[ -z "$MSG" ] && { echo "No message provided"; exit 1; }

SESSION="${TMUX_AGENT_SESSION:-agents}"
REGISTRY_DIR="$HOME/.tmux/registry"

# Resolve target to a slot number
SLOT=""
if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  SLOT="$TARGET"
else
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    name=$(grep '^NAME=' "$f" | cut -d= -f2)
    pane_id=$(grep '^PANE_ID=' "$f" | cut -d= -f2)
    if [ "$name" = "$TARGET" ]; then
      SLOT=$(tmux display-message -t "$pane_id" -p '#{window_index}' 2>/dev/null)
      [ -n "$SLOT" ] && break
      rm -f "$f"
    fi
  done
  [ -z "$SLOT" ] && { echo "Agent not found: $TARGET"; exit 1; }
fi

# Flatten to single line — newlines break send-keys
MSG=$(printf '%s' "$MSG" | tr '\n' ' ' | sed 's/  */ /g; s/^ *//; s/ *$//')

if [[ "$MSG" =~ ^[0-9]$ ]]; then
  tmux send-keys -t "${SESSION}:${SLOT}" "$MSG"
else
  tmux send-keys -l -t "${SESSION}:${SLOT}" "$MSG"
  tmux send-keys -t "${SESSION}:${SLOT}" Enter
fi

echo "Sent to ${TARGET} (slot ${SLOT}): ${MSG}"
