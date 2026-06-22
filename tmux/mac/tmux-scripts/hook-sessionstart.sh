#!/usr/bin/env bash
# SessionStart hook: initialize @waiting for agents that (re)start and land IDLE.
#
# Why: @waiting=2 (idle) is set by the Stop hook, which only fires when a TURN
# COMPLETES. An agent brought up via `claude --resume` — or a fresh spawn given no
# seed — that lands directly at an idle prompt never runs a turn, so its Stop hook
# never fires and @waiting stays UNSET. The Slack A2A bus uses idle-gated delivery
# and treats unset as "busy", so it defers every message to that agent forever
# (see agent-memory note a76f1362d4c6).
#
# Fix: once the pane has genuinely settled at an idle prompt — and only if no other
# hook has set @waiting in the meantime (i.e. the agent did NOT resume into work) —
# set @waiting=2 so the bus will deliver. Agents that resume INTO work are left to
# their normal Stop hook. Fail-safe: if idle is never detected, do nothing (no
# regression vs. today).

cat >/dev/null 2>&1   # consume stdin (SessionStart JSON); we emit no added context

PANE="$TMUX_PANE"
[ -n "$PANE" ] || exit 0
command -v tmux >/dev/null 2>&1 || exit 0

# Bounded watcher: poll until the agent is tracked or has settled idle. Runs
# detached so it never blocks claude startup.
WATCH='
PANE="$1"
for _ in $(seq 1 40); do                                  # up to ~120s (40 x 3s)
  sleep 3
  # Any hook fired (PreToolUse=0 / Notification=1 / Stop=2) → already tracked.
  [ -n "$(tmux show-options -wqv -t "$PANE" @waiting 2>/dev/null)" ] && exit 0
  screen=$(tmux capture-pane -t "$PANE" -p 2>/dev/null) || exit 0
  # Model busy (reasoning or running a tool): wait — Stop sets idle when it ends.
  printf "%s" "$screen" | grep -q "esc to interrupt" && continue
  # A startup/confirmation menu is up (e.g. the --resume summary/full chooser):
  # not idle-for-input — do not initialize.
  printf "%s" "$screen" | grep -qE "Resume from summary|Resume full session|Enter to confirm|Esc to cancel" && continue
  # Require the ready input prompt to be drawn (skips the loading screen and a bare
  # shell prompt), then mark idle so the A2A bus can deliver.
  if printf "%s" "$screen" | grep -qE "for shortcuts|to show tasks|for agents"; then
    tmux set-window-option -t "$PANE" @waiting 2 2>/dev/null
    echo "$(date +%s) sessionstart-idle $PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null
    exit 0
  fi
done
'

if command -v setsid >/dev/null 2>&1; then
  setsid bash -c "$WATCH" _ "$PANE" >/dev/null 2>&1 &
else
  ( bash -c "$WATCH" _ "$PANE" >/dev/null 2>&1 & )   # macOS: orphan via subshell
fi

exit 0
