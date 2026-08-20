#!/usr/bin/env bash
# Summarise the permission-gate decision log.
#
#   gate-report.sh [hours]     default 24
#
# Reads ~/.tmux/gate-decisions.log, written by notify-classify.py and the two
# PreToolUse guards. Format is space-separated, one decision per line:
#
#   <epoch> <decision> <tier> <pane> <head> <sha12>
#
# The log deliberately contains NO command text -- only the command HEAD (a binary
# name) and a truncated sha256. See _log_decision() in notify-classify.py for why:
# commands routinely carry inline tokens, and moving that hazard from the transcript
# into a durable log file would not have fixed it. Use the sha to correlate a specific
# decision back to a session transcript when the full text is genuinely needed.
#
# Reads with `grep -a` throughout: a sibling log (apm.log) carries a NUL byte, which
# makes GNU grep treat the file as binary and silently print nothing -- a false zero
# that has already produced wrong conclusions on this box.

set -uo pipefail

HOURS="${1:-24}"
LOG="${NEXUS_TMUX_DIR:-$HOME/.tmux}/gate-decisions.log"

if [ ! -f "$LOG" ]; then
  echo "no decision log at $LOG"
  echo "(nothing has passed through the gate since logging was added, or NEXUS_TMUX_DIR differs)"
  exit 0
fi

CUTOFF=$(( $(date +%s) - HOURS * 3600 ))

rows() { grep -a . "$LOG" 2>/dev/null | awk -v c="$CUTOFF" 'NF==6 && $1>=c'; }

TOTAL=$(rows | wc -l)
if [ "$TOTAL" -eq 0 ]; then
  echo "no gate decisions in the last ${HOURS}h"
  exit 0
fi

echo "=== gate decisions, last ${HOURS}h (n=$TOTAL) ==="
rows | awk '
  { d[$2]++; t[$2" / "$3]++ }
  END {
    for (k in d) printf "  %-8s %6d  %5.1f%%\n", k, d[k], 100*d[k]/NR
    print ""
    print "  by tier:"
    for (k in t) printf "    %-32s %6d\n", k, t[k]
  }' | sort -k2 -rn

echo
echo "=== what still needs a human (ask/block), by command ==="
rows | awk '$2!="approve" {print "    " $3, $5}' | sort | uniq -c | sort -rn | head -15

echo
echo "=== most common auto-approved commands ==="
rows | awk '$2=="approve" {print "    " $3, $5}' | sort | uniq -c | sort -rn | head -10

echo
echo "auto-approve rate: $(rows | awk '$2=="approve"{a++} END{printf "%.1f%%", 100*a/NR}')"
