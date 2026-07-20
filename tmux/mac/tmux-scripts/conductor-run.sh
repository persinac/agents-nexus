#!/usr/bin/env bash
# conductor-run.sh — open a tmux window and run a Conductor mission from a typed goal.
#
# Bound to <prefix>+C in tmux/mac/tmux.conf. Symlinked per host as ~/.tmux/conductor-run.sh
# (same convention as agent-send.sh / log-action.sh).
#
# SAFETY: the tmux.conf is shared between the personal box and the work laptop, so this binding
# fires on both. A mission is LIVE only on the personal box (hostname `nexus`); on any other host
# it forces --dry-run unless the goal is prefixed with the literal token `live!`. On the box you
# can force a dry-run with a `dry!` prefix. This keeps an accidental keystroke on the work laptop
# from starting a real Jira/GitLab mission.
set -uo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
REPO="$(cd "$(dirname "$SELF")/../../.." 2>/dev/null && pwd)"

goal="$*"
goal="${goal#"${goal%%[![:space:]]*}"}"   # ltrim
if [ -z "$goal" ]; then
  echo "No goal given. Usage: <prefix>+C, then type a mission goal."
  echo "Press Enter to close…"; read -r _; exit 0
fi

host="$(hostname -s 2>/dev/null || hostname)"
mode_flag=""
if [ "$host" = "nexus" ]; then
  :                                        # personal box: LIVE by default
elif [ "${goal%% *}" = "live!" ]; then
  goal="${goal#live! }"                    # other host: explicit opt-in to live
else
  mode_flag="--dry-run"                    # other host: safe default
fi
if [ "${goal%% *}" = "dry!" ]; then mode_flag="--dry-run"; goal="${goal#dry! }"; fi
if [ "${goal%% *}" = "live!" ]; then goal="${goal#live! }"; fi   # allow live! on the box too (no-op flag)

cd "$REPO/agent-runner" 2>/dev/null || { echo "cannot cd to $REPO/agent-runner"; read -r _; exit 1; }

echo "▶ Conductor mission"
echo "  host:  $host"
echo "  repo:  $REPO"
echo "  mode:  ${mode_flag:-LIVE}"
echo "  goal:  $goal"
echo "────────────────────────────────────────────────────────────"
if [ -x .venv/bin/python ]; then
  .venv/bin/python conductor.py $mode_flag "$goal"
else
  task conductor -- $mode_flag "$goal"
fi
rc=$?
echo "────────────────────────────────────────────────────────────"
echo "mission process exited ($rc). Press Enter to close this window…"
read -r _
