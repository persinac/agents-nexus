#!/usr/bin/env bash
# Send a message from one agent to another via tmux.
# Usage: agent-send.sh <window_index> <message>
# Called by Claude Code agents to communicate with other agents.

SLOT="${1:?"Usage: agent-send.sh <slot> <message>"}"
shift
MSG="$*"
[ -z "$MSG" ] && { echo "No message provided"; exit 1; }

TMUX_BIN="/usr/bin/tmux"
[ -x "$TMUX_BIN" ] || TMUX_BIN="/c/msys64/usr/bin/tmux.exe"
[ -x "$TMUX_BIN" ] || { echo "tmux not found"; exit 1; }

SESSION="agents"

# Send the message
if [[ "$MSG" =~ ^[0-9]$ ]]; then
  $TMUX_BIN send-keys -t "${SESSION}:${SLOT}" "$MSG"
else
  $TMUX_BIN send-keys -t "${SESSION}:${SLOT}" "$MSG" Enter
fi

echo "Sent to agent ${SLOT}: ${MSG}"
