#!/usr/bin/env bash
# Stop hook: mark window as idle (grey).
# Red is set by Notification hook when Claude needs user input.

# Read JSON from stdin (consumed once, passed to memory hook via echo)
INPUT=$(cat 2>/dev/null)

# Chain memory event early (works even outside tmux)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$INPUT" | "$SCRIPT_DIR/hook-memory.sh" session_idle 2>/dev/null

# Chain auto-cache (snapshot conversation tail to ~/.tmux/cache/)
"$SCRIPT_DIR/hook-autocache.sh" "$PWD" 2>/dev/null &

# tmux sets TMUX_PANE; herdr sets HERDR_PANE_ID. Fold herdr's in so idle-state +
# surfacing work in both. No-op for tmux agents (TMUX_PANE set).
TMUX_PANE="${TMUX_PANE:-${HERDR_PANE_ID:-}}"
[ -n "$TMUX_PANE" ] || exit 0

NOW=$(date +%s)
"$HOME/.tmux/substrate.sh" report-state "$TMUX_PANE" idle 2>/dev/null
echo "$NOW stop $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

# --- Surface to Slack when this turn needs the HUMAN (not another agent) ---
# The "middle" between full-naive (flood) and permission-only surfacing: a
# classifier (stop-classify.py) inspects the turn's final message and POSTs /notify
# ONLY when the agent is blocked on the operator — progress, FYI, and agent-to-agent
# chatter stay silent. Backgrounded so it never delays the agent. Opt out with
# SLACK_STOP_SURFACE=0; inert without the classifier venv. Per-agent cooldown
# (SLACK_STOP_SURFACE_COOLDOWN, default 90s) keeps a chatty agent from flooding.
CLASSIFY_PY="$HOME/.tmux/.classify-venv/bin/python"
if [ "${SLACK_STOP_SURFACE:-1}" != "0" ] && [ -x "$CLASSIFY_PY" ]; then
  AGENT_NAME=$(grep '^NAME=' "$HOME/.tmux/registry/$TMUX_PANE" 2>/dev/null | cut -d= -f2)
  LAST=$("$HOME/.tmux/substrate.sh" pane-opt "$TMUX_PANE" @last_surface 2>/dev/null)
  COOLDOWN="${SLACK_STOP_SURFACE_COOLDOWN:-90}"
  if [ -n "$AGENT_NAME" ] && { [ -z "$LAST" ] || [ "$(( NOW - LAST ))" -ge "$COOLDOWN" ]; }; then
    (
      TO=""; command -v timeout >/dev/null 2>&1 && TO="timeout 20"
      BODY=$(printf '%s' "$INPUT" | AN="$AGENT_NAME" PANE="$TMUX_PANE" \
        $TO "$CLASSIFY_PY" "$SCRIPT_DIR/stop-classify.py" 2>/dev/null)
      RC=$?
      if [ "$RC" -eq 10 ] && [ -n "$BODY" ]; then
        printf '%s' "$BODY" | curl -m 3 -s -o /dev/null -X POST \
          "http://127.0.0.1:${SLACK_BRIDGE_PORT:-8788}/notify" \
          -H 'Content-Type: application/json' --data @- 2>/dev/null
        "$HOME/.tmux/substrate.sh" set-opt "$TMUX_PANE" @last_surface "$NOW" 2>/dev/null
      fi
    ) &
  fi
fi

exit 0
