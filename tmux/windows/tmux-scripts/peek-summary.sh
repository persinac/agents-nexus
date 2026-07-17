#!/usr/bin/env bash
# Enhanced peek: show a one-line status summary above the raw pane output.
# Usage: peek-summary.sh <slot>
#
# Parses the captured pane to extract:
#   - Current state (working/waiting/idle/stuck)
#   - Last tool used or last action
#   - How long in current state

SLOT="$1"
[ -z "$SLOT" ] && exit 1

TARGET="agents:${SLOT}"

# ── Gather state ───────────────────────────────────────────────
WAITING=$(tmux show-option -wv -t "$TARGET" @waiting 2>/dev/null)
LAST_TOOL=$(tmux show-option -wv -t "$TARGET" @last_tool 2>/dev/null)
WAIT_SINCE=$(tmux show-option -wv -t "$TARGET" @wait_since 2>/dev/null)
WNAME=$(tmux display-message -t "$TARGET" -p '#W' 2>/dev/null)
NOW=$(date +%s)

format_dur() {
  local E=$1
  if [ "$E" -lt 60 ]; then printf "%ds" "$E"
  elif [ "$E" -lt 3600 ]; then printf "%dm%ds" $((E/60)) $((E%60))
  else printf "%dh%dm" $((E/3600)) $(((E%3600)/60))
  fi
}

# ── Capture pane content ───────────────────────────────────────
CONTENT=$(tmux capture-pane -t "$TARGET" -p 2>/dev/null)

# ── Extract last action from pane content ──────────────────────
# Look for the last tool use indicator or Claude output marker.
# Claude Code shows tool names like "Read(...)", "Edit(...)", "Bash(...)", etc.
LAST_ACTION=$(echo "$CONTENT" | grep -oE '(Read|Write|Edit|Bash|Grep|Glob|Agent|WebSearch|WebFetch)\(' | tail -1 | tr -d '(')

# If no tool found, look for the last status/progress line
if [ -z "$LAST_ACTION" ]; then
  # Look for lines that indicate what Claude is doing (task markers, file paths, etc.)
  LAST_ACTION=$(echo "$CONTENT" | grep -E '^\s*(Reading|Writing|Editing|Searching|Running|Creating|Updated|Created)' | tail -1 | head -c 60)
fi

# ── Build status line ─────────────────────────────────────────
case "$WAITING" in
  1)
    DUR=""
    [ -n "$WAIT_SINCE" ] && DUR=" for $(format_dur $((NOW - WAIT_SINCE)))"
    STATUS="\033[1;31m WAITING FOR INPUT${DUR}\033[0m"
    ;;
  2)
    STATUS="\033[1;37m IDLE\033[0m"
    ;;
  0)
    if [ -n "$LAST_TOOL" ] && [ $((NOW - LAST_TOOL)) -gt 600 ]; then
      DUR=$(format_dur $((NOW - LAST_TOOL)))
      STATUS="\033[1;33m POSSIBLY STUCK (no tool use in ${DUR})\033[0m"
    else
      DUR=""
      [ -n "$LAST_TOOL" ] && DUR=" (last tool $(format_dur $((NOW - LAST_TOOL))) ago)"
      STATUS="\033[1;32m WORKING${DUR}\033[0m"
    fi
    ;;
  *)
    STATUS="\033[1;37m UNKNOWN\033[0m"
    ;;
esac

# ── Render ─────────────────────────────────────────────────────
echo ""
printf "  \033[1m[%s] %s\033[0m  " "$SLOT" "$WNAME"
printf "%b" "$STATUS"
[ -n "$LAST_ACTION" ] && printf "  \033[90m› %s\033[0m" "$LAST_ACTION"
echo ""
echo "  ────────────────────────────────────────────────────────────────"
echo ""
echo "$CONTENT" | tail -26
echo ""
echo "  [press any key]"
read -rsn1
