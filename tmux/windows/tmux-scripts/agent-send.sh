#!/usr/bin/env bash
# Send a message from one agent to another via tmux.
# Usage: agent-send.sh [--stdin | --body-file F] <slot_or_name> <message>
# Accepts a slot number (fast path) or an agent name (registry lookup).
#
# ── SECURITY: message-body command injection (SEND side) ──
# A body passed as an INLINE double-quoted arg is expanded by the CALLER's shell
# BEFORE this script runs — so $(...) and backticks in the body EXECUTE on the
# sending host. Real RCE when relaying UNTRUSTED text (e.g. a log line). Cannot be
# fixed inside this script. For untrusted/log/code text, pipe it instead:
#   printf '%s' "$body" | agent-send.sh --stdin <slot_or_name>
# or use --body-file <path>, or capture into a var and pass "$body" (a variable's
# value is not re-expanded). Single-quoting the whole arg is NOT a general fix.

READ_STDIN=0
BODY_FILE=""
while true; do
  case "$1" in
    --stdin)     READ_STDIN=1; shift ;;
    --body-file) BODY_FILE="${2:?"--body-file needs a path"}"; shift 2 ;;
    *) break ;;
  esac
done

TARGET="${1:?"Usage: agent-send.sh [--stdin|--body-file F] <slot_or_name> <message>"}"
shift
# Accept --stdin / --body-file in the body position too, not only leading.
case "${1:-}" in
  --stdin)     READ_STDIN=1; shift ;;
  --body-file) BODY_FILE="${2:?"--body-file needs a path"}"; shift 2 ;;
esac
if [ "$READ_STDIN" = "1" ]; then
  MSG="$(cat)"
elif [ -n "$BODY_FILE" ]; then
  [ -r "$BODY_FILE" ] || { echo "agent-send: --body-file not readable: $BODY_FILE" >&2; exit 1; }
  MSG="$(cat -- "$BODY_FILE")"
else
  MSG="$*"
fi
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
