#!/usr/bin/env bash
# Notification hook: fires on permission prompts, questions, etc.
# Sets @waiting=1 (red) when Claude needs user input.

HOME_DIR="${HOME:-/c/Users/$USER}"
DEBUG_LOG="$HOME_DIR/.tmux/hook-debug.log"
LOG="$HOME_DIR/.tmux/apm.log"

log_debug() { echo "$(date '+%H:%M:%S') NOTIF: $*" >> "$DEBUG_LOG" 2>/dev/null; }

INPUT=$(cat)
log_debug "stdin: $(echo "$INPUT" | head -c 200)"

# Extract fields
CWD=$(echo "$INPUT" | sed -n 's/.*"cwd" *: *"\([^"]*\)".*/\1/p' | head -1)
NTYPE=$(echo "$INPUT" | sed -n 's/.*"notification_type" *: *"\([^"]*\)".*/\1/p' | head -1)
log_debug "type: $NTYPE, cwd: $CWD"

# Only go red for genuine approval/input requests.
# idle_prompt fires when Claude finishes a turn — Stop hook already handles that (→ @waiting=2).
case "$NTYPE" in
  permission_prompt|elicitation_dialog) ;;
  *) log_debug "ignoring notification type: $NTYPE"; exit 0 ;;
esac

# Find tmux
TMUX_BIN="/usr/bin/tmux"
[ -x "$TMUX_BIN" ] || TMUX_BIN="/c/msys64/usr/bin/tmux.exe"
[ -x "$TMUX_BIN" ] || exit 0
$TMUX_BIN list-sessions &>/dev/null || exit 0

# Match cwd to tmux pane
PANE=""
if [ -n "$CWD" ]; then
  CWD_NORM=$(printf '%s' "$CWD" | tr '\\' '/' | sed 's|//|/|g; s|^C:|/c|; s|^c:|/c|')

  while IFS='|' read -r pane_id pane_path; do
    PANE_NORM=$(printf '%s' "$pane_path" | tr '\\' '/' | sed 's|//|/|g; s|^C:|/c|; s|^c:|/c|')
    if [ "$CWD_NORM" = "$PANE_NORM" ]; then
      PANE="$pane_id"
      break
    fi
  done < <($TMUX_BIN list-panes -a -F '#{pane_id}|#{pane_current_path}' 2>/dev/null)
fi

# Fallback: single claude pane
if [ -z "$PANE" ]; then
  CLAUDE_PANES=$($TMUX_BIN list-panes -a -F '#{pane_id}|#{pane_current_command}' 2>/dev/null \
    | grep -i 'claude' | cut -d'|' -f1)
  COUNT=$(echo "$CLAUDE_PANES" | grep -c . 2>/dev/null)
  [ "$COUNT" -eq 1 ] && PANE=$(echo "$CLAUDE_PANES" | head -1)
fi

[ -z "$PANE" ] && { log_debug "no pane found"; exit 0; }

NOW=$(date +%s)
WNAME=$($TMUX_BIN display-message -t "$PANE" -p '#W' 2>/dev/null)

$TMUX_BIN set-window-option -t "$PANE" @waiting 1 2>/dev/null
$TMUX_BIN set-option -w -t "$PANE" @wait_since "$NOW" 2>/dev/null
log_debug "set @waiting=1 on $PANE ($WNAME)"

# APM log
echo "$NOW wait $PANE" >> "$LOG" 2>/dev/null

exit 0
