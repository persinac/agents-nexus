#!/usr/bin/env bash
# prompt-surfacer.sh — backstop that surfaces an agent's permission prompts to Slack when
# its Claude `Notification` hook isn't doing so.
#
# A per-agent Notification hook can silently stop firing (observed: an agent kept hitting
# Edit/Write prompts but its hook went quiet, so no card ever posted and the prompts were
# invisible in Slack). This poller captures the pane, detects the Claude permission-prompt
# frame, and POSTs the bridge's /notify to create the SAME tappable ✅Approve/❌Deny card the
# hook would have. It does NOT answer prompts — the human still taps the card; this only makes
# the prompt visible. Scoped to one pane, time-bounded, self-stopping.
#
# Usage: prompt-surfacer.sh <pane> <name> [max_seconds]
set -u

PANE="${1:?usage: prompt-surfacer.sh <pane> <name> [max_seconds]}"
NAME="${2:?agent name (as registered)}"
MAX="${3:-1800}"
SUB="$HOME/.tmux/substrate.sh"
PORT="${SLACK_BRIDGE_PORT:-8788}"
SIG='do you want to|❯ *[0-9]\. *yes'

start=$(date +%s)
surfaced=0
last=""
echo "[surfacer] $NAME ($PANE) → posting prompt cards to /notify, cap ${MAX}s, from $(date +%H:%M:%S)"

post_card() {
  local action="$1"
  local body
  # Omit wait_since ON PURPOSE. The bridge's resolveRequest has a same-prompt guard that
  # compares the card's wait_since to the pane's @wait_since sidecar and drops the card as
  # "stale" if they differ. When the agent's Notification hook is dead, that sidecar is never
  # set — so any wait_since we invent would mismatch and every tap would be rejected. Leaving
  # it empty makes the guard fall through, so a tap delivers the digit straight to the pane.
  body=$(python3 -c "import json,sys; print(json.dumps({'name':sys.argv[1],'pane':sys.argv[2],'kind':'permission_prompt','summary':sys.argv[3]}))" \
    "$NAME" "$PANE" "$action" 2>/dev/null)
  printf '%s' "$body" | curl -m 3 -s -o /dev/null -X POST "http://127.0.0.1:$PORT/notify" -H 'Content-Type: application/json' --data @- 2>/dev/null
}

while :; do
  (( $(date +%s) - start >= MAX )) && { echo "[surfacer] ${MAX}s cap reached; stop"; break; }

  cap=$("$SUB" capture "$PANE" 14 2>/dev/null) || { echo "[surfacer] pane gone; stop"; break; }

  if grep -qiE "$SIG" <<<"$cap"; then
    action=$(grep -iE "do you want to" <<<"$cap" | head -1 | sed 's/^ *//;s/ *$//')
    [ -z "$action" ] && action="needs your approval"
    if [ "$action" != "$last" ]; then
      post_card "$action"
      surfaced=$((surfaced + 1))
      last="$action"
      echo "[surfacer] #$surfaced $(date +%H:%M:%S) — $action"
    fi
    # Wait until this prompt clears (reset dedup) or a different prompt appears (re-surface),
    # so each distinct prompt is carded exactly once.
    for _ in $(seq 1 20); do
      sleep 2
      c2=$("$SUB" capture "$PANE" 14 2>/dev/null) || break
      grep -qiE "$SIG" <<<"$c2" || { last=""; break; }
      a2=$(grep -iE "do you want to" <<<"$c2" | head -1 | sed 's/^ *//;s/ *$//')
      [ -n "$a2" ] && [ "$a2" != "$action" ] && break
    done
  else
    sleep 4
  fi
done

echo "[surfacer] EXIT $(date +%H:%M:%S): surfaced $surfaced prompt(s) for $NAME"
