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

# Resolve target to a tmux send-keys destination (DEST).
# A pane id (%NN) is exact — immune to window renumbering, stale/duplicate registry
# slots, and active-pane drift — so it's the preferred target. A bare number is a
# window index (legacy/explicit). A name is resolved via the registry to its live
# window index.
DEST=""
if [[ "$TARGET" =~ ^%[0-9]+$ ]]; then
  if tmux list-panes -a -F '#{pane_id}' 2>/dev/null | grep -qx "$TARGET"; then
    DEST="$TARGET"
  else
    echo "Pane not found: $TARGET"; exit 1
  fi
else
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
  DEST="${SESSION}:${SLOT}"
fi

# Flatten to single line — newlines break send-keys
MSG=$(printf '%s' "$MSG" | tr '\n' ' ' | sed 's/  */ /g; s/^ *//; s/ *$//')

if [[ "$MSG" =~ ^[0-9]$ ]]; then
  tmux send-keys -t "$DEST" "$MSG"
else
  tmux send-keys -l -t "$DEST" "$MSG"
  tmux send-keys -t "$DEST" Enter
fi

echo "Sent to ${TARGET} (${DEST}): ${MSG}"
