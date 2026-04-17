#!/usr/bin/env bash
# Send a message from one agent to another via tmux.
# Usage: agent-send.sh <window_index> <message>

SLOT="${1:?"Usage: agent-send.sh <slot> <message>"}"
shift
MSG="$*"
[ -z "$MSG" ] && { echo "No message provided"; exit 1; }

SESSION="agents"

if [[ "$MSG" =~ ^[0-9]$ ]]; then
  tmux send-keys -t "${SESSION}:${SLOT}" "$MSG"
else
  tmux send-keys -t "${SESSION}:${SLOT}" "$MSG" Enter
fi

echo "Sent to agent ${SLOT}: ${MSG}"
