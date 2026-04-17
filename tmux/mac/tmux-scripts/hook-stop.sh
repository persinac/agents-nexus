#!/usr/bin/env bash
# Stop hook: mark window as idle (grey).
# Red is set by Notification hook when Claude needs user input.

[ -n "$TMUX_PANE" ] || exit 0

NOW=$(date +%s)
tmux set-window-option -t "$TMUX_PANE" @waiting 2 2>/dev/null
echo "$NOW stop $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

exit 0
