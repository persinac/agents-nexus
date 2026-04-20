#!/usr/bin/env bash
# Stop hook: mark window as idle (grey).
# Red is set by Notification hook when Claude needs user input.

# Read JSON from stdin (consumed once, passed to memory hook via echo)
INPUT=$(cat 2>/dev/null)

# Chain memory event early (works even outside tmux)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$INPUT" | "$SCRIPT_DIR/hook-memory.sh" session_idle 2>/dev/null

[ -n "$TMUX_PANE" ] || exit 0

NOW=$(date +%s)
tmux set-window-option -t "$TMUX_PANE" @waiting 2 2>/dev/null
echo "$NOW stop $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

exit 0
