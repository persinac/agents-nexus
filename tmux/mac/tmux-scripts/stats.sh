#!/usr/bin/env bash
LOG="$HOME/.tmux/apm.log"
NOW=$(date +%s)
TODAY_START=$(date -v0H -v0M -v0S +%s 2>/dev/null || date -d "today 00:00:00" +%s 2>/dev/null)

# Prune entries older than 24h
if [ -f "$LOG" ]; then
  CUTOFF=$((NOW - 86400))
  awk -v c="$CUTOFF" '$1 >= c' "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi

# Rolling 60s APM
read AGENT_APM HUMAN_APM <<< $(awk -v now="$NOW" '
  $1 > (now-60) {
    if ($2 == "agent") a++
    else if ($2 == "human-q" || $2 == "human-v" || $2 == "switch" || $2 ~ /^tmux-/) h++
  }
  END { print a+0, h+0 }
' "$LOG" 2>/dev/null)

TOTAL_APM=$((AGENT_APM + HUMAN_APM))

# Today's totals
read TODAY_TOOLS TODAY_Q TODAY_V TODAY_SWITCH TODAY_TMUX <<< $(awk -v start="$TODAY_START" '
  $1 >= start {
    if ($2 == "agent") tools++
    if ($2 == "human-q") q++
    if ($2 == "human-v") v++
    if ($2 == "switch") s++
    if ($2 ~ /^tmux-/) t++
  }
  END { print tools+0, q+0, v+0, s+0, t+0 }
' "$LOG" 2>/dev/null)

# Avg response latency
AVG_LATENCY=$(awk '
  $2 == "wait"   { last_wait = $1 }
  $2 == "human-q" && last_wait > 0 {
    diff = $1 - last_wait
    if (diff > 0 && diff < 600) { total += diff; count++ }
    last_wait = 0
  }
  END { if (count > 0) printf "%.0fs", total/count; else print "n/a" }
' "$LOG" 2>/dev/null)

ACTIVE=$(tmux list-windows -t agents -F "#{pane_current_command}" 2>/dev/null | grep -cv "^zsh$" || echo 0)

clear
echo ""
echo " +------------------------------------------+"
echo " |            APM Dashboard                 |"
echo " +------------------------------------------+"
echo " |                                          |"
printf " |  Your APM:    %-4s  (q:%s  v:%s)        |\n" "$HUMAN_APM" "$TODAY_Q" "$TODAY_V"
printf " |  Agent APM:   %-4s  (tool uses/min)     |\n" "$AGENT_APM"
printf " |  Combined:    %-4s                      |\n" "$TOTAL_APM"
echo " |                                          |"
printf " |  Avg response time:  %-6s             |\n" "$AVG_LATENCY"
printf " |  Active agents:      %-3s               |\n" "$ACTIVE"
echo " |                                          |"
echo " |  ---- Today -------------------------    |"
printf " |  Tools run:   %-5s                     |\n" "$TODAY_TOOLS"
printf " |  q commands:  %-5s                     |\n" "$TODAY_Q"
printf " |  Switches:    %-5s                     |\n" "$TODAY_SWITCH"
printf " |  tmux cmds:   %-5s                     |\n" "$TODAY_TMUX"
echo " |                                          |"
echo " |  [press any key]                         |"
echo " +------------------------------------------+"
echo ""
read -rsn1
