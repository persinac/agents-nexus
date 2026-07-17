#!/usr/bin/env bash
# Notification hook: fires on permission prompts, questions, etc.
# Sets @waiting=1 (red) when Claude needs user input.

INPUT=$(cat)

# Chain memory event early (works even outside tmux)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$INPUT" | "$SCRIPT_DIR/hook-memory.sh" permission_wait 2>/dev/null

# tmux sets TMUX_PANE; herdr sets HERDR_PANE_ID. Fold herdr's in so the guard, the
# classifier PANE=, the registry lookup, and the substrate calls all work in both.
TMUX_PANE="${TMUX_PANE:-${HERDR_PANE_ID:-}}"
[ -n "$TMUX_PANE" ] || exit 0

NTYPE=$(echo "$INPUT" | sed -n 's/.*"notification_type" *: *"\([^"]*\)".*/\1/p' | head -1)

# Only go red for genuine approval/input requests.
# idle_prompt fires when Claude finishes a turn — Stop hook already handles that (→ @waiting=2).
case "$NTYPE" in
  permission_prompt|elicitation_dialog) ;;
  *) exit 0 ;;
esac

NOW=$(date +%s)
WNAME=$("$HOME/.tmux/substrate.sh" pane-field "$TMUX_PANE" '#W' 2>/dev/null)

# Agent name — used by both the classifier and the Slack post.
AGENT_NAME=$(grep '^NAME=' "$HOME/.tmux/registry/$TMUX_PANE" 2>/dev/null | cut -d= -f2)
[ -z "$AGENT_NAME" ] && AGENT_NAME="${WNAME:-agent}"

# Auto-approve gate + middle-man summary. The brain categorizes the pending tool:
#   exit 0  -> read-only: answer "1. Yes" locally, no human, no Slack
#   exit 10 -> needs a human: prints the /notify body ([category] + summary) on stdout
# Fails safe to "modify" (ask) on any error.
CLASSIFY_PY="$HOME/.tmux/.classify-venv/bin/python"
BODY=""
if [ "$NTYPE" = "permission_prompt" ] && [ -x "$CLASSIFY_PY" ]; then
  # Optional external timeout (macOS lacks `timeout`); litellm bounds the API call itself.
  TIMEOUT_BIN=""
  if command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN="timeout 20"
  elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout 20"; fi
  BODY=$(printf '%s' "$INPUT" | AN="$AGENT_NAME" PANE="$TMUX_PANE" KIND="$NTYPE" WAIT_SINCE="$NOW" FB="needs input ($NTYPE)" \
    $TIMEOUT_BIN "$CLASSIFY_PY" "$SCRIPT_DIR/notify-classify.py" 2>/dev/null)
  RC=$?
  if [ "$RC" -eq 0 ]; then
    ( sleep 0.4; "$HOME/.tmux/substrate.sh" send "$TMUX_PANE" 1 2>/dev/null ) &
    "$HOME/.tmux/substrate.sh" report-state "$TMUX_PANE" working "$NOW" 2>/dev/null
    echo "$NOW auto-approve $TMUX_PANE" >> "$HOME/.tmux/auto-approve.log" 2>/dev/null
    exit 0
  fi
fi

# --- needs a human: flag + desktop notify + surface to Slack ---
"$HOME/.tmux/substrate.sh" report-state needs-input "$TMUX_PANE" "$NTYPE" "$NOW" 2>/dev/null

# Desktop notification — OS-guarded so this hook is the ONE shared copy (no per-OS override):
#   macOS  → osascript bubble + sound
#   Linux  → notify-send bubble if a console session is attached, plus a bell (\a) that iTerm2 /
#            Windows Terminal turn into a system notification for SSH clients.
case "$OSTYPE" in
  darwin*)
    osascript -e "display notification \"Agent ${WNAME:-?} needs input ($NTYPE)\" with title \"Claude Code\" sound name \"Glass\"" 2>/dev/null & ;;
  *)
    command -v notify-send >/dev/null 2>&1 && \
      notify-send "Claude Code" "Agent ${WNAME:-?} needs input ($NTYPE)" 2>/dev/null &
    printf '\a' ;;
esac

# Slack round-trip — backgrounded; curl no-ops if the bridge is down.
(
  # The brain set BODY for a modify decision; otherwise (elicitation, or no classifier
  # venv) fall back to the deterministic payload helper.
  if [ -z "$BODY" ]; then
    BODY=$(printf '%s' "$INPUT" | AN="$AGENT_NAME" PANE="$TMUX_PANE" FB="needs input ($NTYPE)" KIND="$NTYPE" WAIT_SINCE="$NOW" \
      python3 "$SCRIPT_DIR/notify-payload.py" 2>/dev/null)
  fi
  printf '%s' "$BODY" \
    | curl -m 2 -s -o /dev/null -X POST "http://127.0.0.1:${SLACK_BRIDGE_PORT:-8788}/notify" -H 'Content-Type: application/json' --data @- 2>/dev/null
) &

echo "$NOW wait $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

exit 0
