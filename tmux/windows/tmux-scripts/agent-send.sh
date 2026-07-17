#!/usr/bin/env bash
# Send a message from one agent to another via tmux.
# Usage: agent-send.sh <slot_or_name> <message>
# Accepts a slot number (fast path) or an agent name (registry lookup).

TARGET="${1:?"Usage: agent-send.sh <slot_or_name> <message>"}"
shift
MSG="$*"
[ -z "$MSG" ] && { echo "No message provided"; exit 1; }

# Ensure $HOME resolves to a writable Windows path
: "${USER:=${USERNAME:=$(whoami)}}"
HOME_DIR="${HOME:-/c/Users/$USER}"
case "$HOME_DIR" in
  /home/*) HOME_DIR="/c/msys64${HOME_DIR}" ;;
esac

TMUX_BIN="/usr/bin/tmux"
[ -x "$TMUX_BIN" ] || TMUX_BIN="/c/msys64/usr/bin/tmux.exe"
[ -x "$TMUX_BIN" ] || { echo "tmux not found"; exit 1; }

SESSION="${TMUX_AGENT_SESSION:-agents}"
REGISTRY_DIR="$HOME_DIR/.tmux/registry"

# Resolve target to a slot number
SLOT=""
if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  SLOT="$TARGET"
else
  # Name-based lookup: scan registry for matching window name
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    name=$(grep '^NAME=' "$f" | cut -d= -f2)
    pane_id=$(grep '^PANE_ID=' "$f" | cut -d= -f2)
    if [ "$name" = "$TARGET" ]; then
      SLOT=$($TMUX_BIN display-message -t "$pane_id" -p '#{window_index}' 2>/dev/null)
      [ -n "$SLOT" ] && break
      rm -f "$f"
    fi
  done
  [ -z "$SLOT" ] && { echo "Agent not found: $TARGET"; exit 1; }
fi

# Flatten to single line — newlines break send-keys
MSG=$(printf '%s' "$MSG" | tr '\n' ' ' | sed 's/  */ /g; s/^ *//; s/ *$//')

# Send the message
if [[ "$MSG" =~ ^[0-9]$ ]]; then
  $TMUX_BIN send-keys -t "${SESSION}:${SLOT}" "$MSG"
else
  $TMUX_BIN send-keys -l -t "${SESSION}:${SLOT}" "$MSG"
  $TMUX_BIN send-keys -t "${SESSION}:${SLOT}" Enter
fi

echo "Sent to ${TARGET} (slot ${SLOT}): ${MSG}"
