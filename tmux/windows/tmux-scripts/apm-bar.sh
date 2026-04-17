#!/usr/bin/env bash
LOG="$HOME/.tmux/apm.log"
[ -f "$LOG" ] || { printf " --a/--h "; exit; }
NOW=$(date +%s)
awk -v now="$NOW" '
  $1 > (now-60) {
    if ($2 == "agent") a++
    else if ($2 == "human-q" || $2 == "human-v" || $2 == "switch" || $2 ~ /^tmux-/) h++
  }
  END { printf " %da/%dh ", a+0, h+0 }
' "$LOG"
