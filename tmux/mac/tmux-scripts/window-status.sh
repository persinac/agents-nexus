#!/usr/bin/env bash
# Renders a single tmux window status segment with correct color and duration.
# Usage: window-status.sh <window_id> <window_index> <window_name> [current]
#
# @waiting states:
#   0 = actively working (PreToolUse fired) → GREEN
#       (but YELLOW + timer if no tool use in >10min — stuck agent)
#   1 = needs user input (Notification fired) → RED + timer
#   2 = finished/idle (Stop fired)           → GREY
#   unset = fresh window, no hooks fired yet → GREEN if claude running, else GREY

WINDOW="$1"
INDEX="$2"
NAME="$3"
IS_CURRENT="$4"

[ -z "$WINDOW" ] && exit 0

WAITING=$(tmux show-option -wv -t "$WINDOW" @waiting 2>/dev/null)
CMD=$(tmux display-message -t "$WINDOW" -p '#{pane_current_command}' 2>/dev/null)
IS_CLAUDE=false
# Match 'claude' or versioned binaries like '2.1.88' (Mac symlinks claude to its version)
[[ "$CMD" == "claude" || "$CMD" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && IS_CLAUDE=true

format_dur() {
  local E=$1
  if [ "$E" -lt 60 ]; then printf "%ds" "$E"
  elif [ "$E" -lt 3600 ]; then printf "%dm%ds" $((E/60)) $((E%60))
  else printf "%dh%dm" $((E/3600)) $(((E%3600)/60))
  fi
}

# State 1: RED — needs user input (permission prompt, question, etc.)
if [ "$WAITING" = "1" ]; then
  SINCE=$(tmux show-option -wv -t "$WINDOW" @wait_since 2>/dev/null)
  DUR=""
  if [ -n "$SINCE" ]; then
    DUR=$(format_dur $(($(date +%s) - SINCE)))
  fi
  printf "#[fg=red bold] %s:%s(%s) " "$INDEX" "$NAME" "$DUR"
  exit 0
fi

# State 2: GREEN — claude is running (or YELLOW if stuck)
if $IS_CLAUDE; then
  STUCK_THRESHOLD=600  # 10 minutes
  LAST_TOOL=$(tmux show-option -wv -t "$WINDOW" @last_tool 2>/dev/null)
  IS_STUCK=false
  if [ -n "$LAST_TOOL" ]; then
    ELAPSED=$(( $(date +%s) - LAST_TOOL ))
    [ "$ELAPSED" -gt "$STUCK_THRESHOLD" ] && IS_STUCK=true
  fi

  if $IS_STUCK; then
    DUR=$(format_dur "$ELAPSED")
    if [ "$IS_CURRENT" = "current" ]; then
      printf "#[fg=#f9e2af bold] %s:%s(%s) " "$INDEX" "$NAME" "$DUR"
    else
      printf "#[fg=#f9e2af] %s:%s(%s) " "$INDEX" "$NAME" "$DUR"
    fi
  elif [ "$IS_CURRENT" = "current" ]; then
    printf "#[fg=#a6e3a1 bold] %s:%s " "$INDEX" "$NAME"
  else
    printf "#[fg=#a6e3a1] %s:%s " "$INDEX" "$NAME"
  fi
  exit 0
fi

# State 3: GREY — no claude process (shell prompt, exited, or dispatcher)
if [ "$IS_CURRENT" = "current" ]; then
  printf "#[fg=white bold] %s:%s " "$INDEX" "$NAME"
else
  printf "#[fg=#585b70] %s:%s " "$INDEX" "$NAME"
fi
