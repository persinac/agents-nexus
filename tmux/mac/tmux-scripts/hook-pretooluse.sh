#!/usr/bin/env bash
# PreToolUse hook: clear waiting flag, log agent action for APM.

[ -n "$TMUX_PANE" ] || exit 0

NOW=$(date +%s)
tmux set-window-option -t "$TMUX_PANE" @waiting 0 2>/dev/null
tmux set-window-option -t "$TMUX_PANE" @last_tool "$NOW" 2>/dev/null
tmux set-option -wu -t "$TMUX_PANE" @wait_since 2>/dev/null
echo "$NOW agent $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

exit 0
