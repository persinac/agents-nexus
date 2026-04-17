#!/usr/bin/env bash
# Notification hook: fires on permission prompts, questions, etc.
# Sets @waiting=1 (red) when Claude needs user input.

[ -n "$TMUX_PANE" ] || exit 0

# Read JSON from stdin
INPUT=$(cat)
NTYPE=$(echo "$INPUT" | sed -n 's/.*"notification_type" *: *"\([^"]*\)".*/\1/p' | head -1)

# Only go red for genuine approval/input requests.
# idle_prompt fires when Claude finishes a turn — Stop hook already handles that (→ @waiting=2).
case "$NTYPE" in
  permission_prompt|elicitation_dialog) ;;
  *) exit 0 ;;
esac

NOW=$(date +%s)
WNAME=$(tmux display-message -t "$TMUX_PANE" -p '#W' 2>/dev/null)

tmux set-window-option -t "$TMUX_PANE" @waiting 1 2>/dev/null
tmux set-option -w -t "$TMUX_PANE" @wait_since "$NOW" 2>/dev/null

# macOS notification
osascript -e "display notification \"Agent ${WNAME:-?} needs input ($NTYPE)\" with title \"Claude Code\" sound name \"Glass\"" 2>/dev/null &

echo "$NOW wait $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

exit 0
