#!/usr/bin/env bash
# PreToolUse hook: clear waiting flag, log agent action for APM.

# Read JSON from stdin (consumed once, passed to memory hook via echo)
INPUT=$(cat 2>/dev/null)

# Chain memory event early (works even outside tmux)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$INPUT" | "$SCRIPT_DIR/hook-memory.sh" tool_use 2>/dev/null

# Pin this pane's transcript path for automode-watchdog.py. PreToolUse is what
# makes the map reliable: a classifier denial IS a tool call, so the entry always
# exists before one can land. Self-skips the write when unchanged.
echo "$INPUT" | "$SCRIPT_DIR/record-transcript.sh" 2>/dev/null

# tmux sets TMUX_PANE; herdr sets HERDR_PANE_ID. Fold herdr's in so the guard + the
# substrate calls (backend=herdr) just work. No-op for tmux agents (TMUX_PANE set).
TMUX_PANE="${TMUX_PANE:-${HERDR_PANE_ID:-}}"
[ -n "$TMUX_PANE" ] || exit 0

NOW=$(date +%s)
"$HOME/.tmux/substrate.sh" report-state "$TMUX_PANE" working "$NOW" 2>/dev/null
echo "$NOW agent $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

exit 0
