#!/usr/bin/env bash
# PreToolUse hook: clear waiting flag, log agent action for APM.
# Bridges Windows Claude Code → MSYS2 tmux.

HOME_DIR="${HOME:-/c/Users/$USER}"
DEBUG_LOG="$HOME_DIR/.tmux/hook-debug.log"
LOG="$HOME_DIR/.tmux/apm.log"

log_debug() { echo "$(date '+%H:%M:%S') TOOL: $*" >> "$DEBUG_LOG" 2>/dev/null; }

# Read JSON from stdin
INPUT=$(cat)

# Extract cwd from JSON
CWD=$(echo "$INPUT" | sed -n 's/.*"cwd" *: *"\([^"]*\)".*/\1/p' | head -1)

# Find tmux binary
TMUX_BIN="/usr/bin/tmux"
[ -x "$TMUX_BIN" ] || TMUX_BIN="/c/msys64/usr/bin/tmux.exe"
[ -x "$TMUX_BIN" ] || exit 0
$TMUX_BIN list-sessions &>/dev/null || exit 0

# Match cwd to tmux pane
PANE=""
if [ -n "$CWD" ]; then
  CWD_NORM=$(printf '%s' "$CWD" | tr '\\' '/' | sed 's|//|/|g; s|^C:|/c|; s|^c:|/c|')

  [ -z "$CWD_NORM" ] && exit 0

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

[ -z "$PANE" ] && exit 0

NOW=$(date +%s)

$TMUX_BIN set-window-option -t "$PANE" @waiting 0 2>/dev/null
$TMUX_BIN set-window-option -t "$PANE" @last_tool "$NOW" 2>/dev/null
$TMUX_BIN set-option -wu -t "$PANE" @wait_since 2>/dev/null

echo "$NOW agent $PANE" >> "$LOG" 2>/dev/null

exit 0
