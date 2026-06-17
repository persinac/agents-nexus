#!/usr/bin/env bash
# Notification hook: fires on permission prompts, questions, etc.
# Sets @waiting=1 (red) when Claude needs user input.

INPUT=$(cat)

# Chain memory event early (works even outside tmux)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$INPUT" | "$SCRIPT_DIR/hook-memory.sh" permission_wait 2>/dev/null

[ -n "$TMUX_PANE" ] || exit 0

NTYPE=$(echo "$INPUT" | sed -n 's/.*"notification_type" *: *"\([^"]*\)".*/\1/p' | head -1)

# Only go red for genuine approval/input requests.
# idle_prompt fires when Claude finishes a turn — Stop hook already handles that (→ @waiting=2).
case "$NTYPE" in
  permission_prompt|elicitation_dialog) ;;
  *) exit 0 ;;
esac

NOW=$(date +%s)
WNAME=$(tmux display-message -t "$TMUX_PANE" -p '#W' 2>/dev/null)

tmux set-window-option -t "$TMUX_PANE" @waiting 1 2>/dev/null
tmux set-option -w -t "$TMUX_PANE" @wait_since "$NOW" 2>/dev/null
tmux set-option -w -t "$TMUX_PANE" @wait_type "$NTYPE" 2>/dev/null

# macOS notification
osascript -e "display notification \"Agent ${WNAME:-?} needs input ($NTYPE)\" with title \"Claude Code\" sound name \"Glass\"" 2>/dev/null &

# Slack round-trip: ping the slack-bridge so the request surfaces in #nexus and a
# reply in that thread routes back here. Backgrounded with a short timeout; if the
# bridge isn't running, curl just no-ops — never blocks or fails the agent.
(
  AGENT_NAME=$(grep '^NAME=' "$HOME/.tmux/registry/$TMUX_PANE" 2>/dev/null | cut -d= -f2)
  [ -z "$AGENT_NAME" ] && AGENT_NAME="${WNAME:-agent}"
  # Build the POST body via the helper: it surfaces *what* the agent is asking
  # (pending tool call / question) from the transcript, falling back to the hook's
  # own message then $FB. Sibling script — same dir as this hook (~/.tmux symlink).
  printf '%s' "$INPUT" | AN="$AGENT_NAME" PANE="$TMUX_PANE" FB="needs input ($NTYPE)" KIND="$NTYPE" \
    python3 "$SCRIPT_DIR/notify-payload.py" 2>/dev/null \
    | curl -m 2 -s -o /dev/null -X POST "http://127.0.0.1:${SLACK_BRIDGE_PORT:-8788}/notify" -H 'Content-Type: application/json' --data @- 2>/dev/null
) &

echo "$NOW wait $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

exit 0
