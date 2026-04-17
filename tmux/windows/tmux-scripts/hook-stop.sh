#!/usr/bin/env bash
# Stop hook: mark window as idle (grey). NOT red — red is set by Notification hook
# when Claude specifically needs user input (permission prompt, etc).
#
# Claude Code hooks receive JSON on stdin with { cwd, session_id, ... }.
# We match cwd to a tmux pane's current path to find our pane.

HOME_DIR="${HOME:-/c/Users/$USER}"
DEBUG_LOG="$HOME_DIR/.tmux/hook-debug.log"
LOG="$HOME_DIR/.tmux/apm.log"

log_debug() { echo "$(date '+%H:%M:%S') STOP: $*" >> "$DEBUG_LOG" 2>/dev/null; }

# Read JSON from stdin (Claude Code passes hook context here)
INPUT=$(cat)
log_debug "stdin: $INPUT"

# Extract cwd from JSON
CWD=$(echo "$INPUT" | sed -n 's/.*"cwd" *: *"\([^"]*\)".*/\1/p' | head -1)
log_debug "cwd: $CWD"

# Find tmux binary
TMUX_BIN="/usr/bin/tmux"
[ -x "$TMUX_BIN" ] || TMUX_BIN="/c/msys64/usr/bin/tmux.exe"
if ! [ -x "$TMUX_BIN" ]; then
  log_debug "tmux not found"
  exit 0
fi

# Check if tmux server is running
if ! $TMUX_BIN list-sessions &>/dev/null; then
  log_debug "no tmux server"
  exit 0
fi

log_debug "tmux server found"

# Strategy 1: match cwd to a tmux pane's current path
PANE=""
if [ -n "$CWD" ]; then
  # Normalize: JSON gives us C:\\projects\\... — convert to /c/projects/...
  # Use tr for backslash conversion (more reliable than sed for this)
  CWD_NORM=$(printf '%s' "$CWD" | tr '\\' '/' | sed 's|//|/|g; s|^C:|/c|; s|^c:|/c|')
  log_debug "cwd_norm: $CWD_NORM"

  [ -z "$CWD_NORM" ] && { log_debug "cwd_norm empty, skipping match"; exit 0; }

  while IFS='|' read -r pane_id pane_path; do
    PANE_NORM=$(printf '%s' "$pane_path" | tr '\\' '/' | sed 's|//|/|g; s|^C:|/c|; s|^c:|/c|')
    if [ "$CWD_NORM" = "$PANE_NORM" ]; then
      PANE="$pane_id"
      log_debug "matched pane by cwd: $PANE (path: $pane_path)"
      break
    fi
  done < <($TMUX_BIN list-panes -a -F '#{pane_id}|#{pane_current_path}' 2>/dev/null)
fi

# Strategy 2: if only one claude pane exists, use it
if [ -z "$PANE" ]; then
  CLAUDE_PANES=$($TMUX_BIN list-panes -a -F '#{pane_id}|#{pane_current_command}' 2>/dev/null \
    | grep -i 'claude' | cut -d'|' -f1)
  COUNT=$(echo "$CLAUDE_PANES" | grep -c . 2>/dev/null)
  if [ "$COUNT" -eq 1 ]; then
    PANE=$(echo "$CLAUDE_PANES" | head -1)
    log_debug "matched single claude pane: $PANE"
  else
    log_debug "multiple or no claude panes ($COUNT), can't match"
  fi
fi

if [ -z "$PANE" ]; then
  log_debug "no pane found, exiting"
  exit 0
fi

NOW=$(date +%s)
WNAME=$($TMUX_BIN display-message -t "$PANE" -p '#W' 2>/dev/null)

# Set @waiting=2 → grey (idle/done). Red (1) is only set by Notification hook.
$TMUX_BIN set-window-option -t "$PANE" @waiting 2 2>/dev/null
log_debug "set @waiting=2 (idle) on $PANE (rc=$?)"

# APM log
echo "$NOW stop $PANE" >> "$LOG" 2>/dev/null
log_debug "done, pane=$PANE wname=$WNAME"

exit 0
