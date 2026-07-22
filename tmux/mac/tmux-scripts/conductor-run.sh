#!/usr/bin/env bash
# conductor-run.sh — open a tmux window and run a Conductor mission from a typed goal.
#
# Bound to <prefix>+C in tmux/mac/tmux.conf. Symlinked per host as ~/.tmux/conductor-run.sh
# (same convention as agent-send.sh / log-action.sh).
#
# SAFETY: run-mode is EXPLICIT and env-first — never encoded in the goal text. The default is
# dry-run (reporting logged, not sent) on EVERY host; a box that should run live sets
# CONDUCTOR_RUN_MODE=live in ~/.tmux/env.sh (the personal `nexus` box does). Per-run you can override
# with --live / --dry-run on the conductor.py invocation. This keeps an accidental keystroke on the
# work laptop from starting a real Jira/GitLab mission, and keeps run-mode tokens out of the goal
# (a leaked `live!` used to pollute the branch slug and split a mission across two branches/MRs).
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
# Run-mode: explicit + env-first. Default dry (safe); a live box sets CONDUCTOR_RUN_MODE=live in
# ~/.tmux/env.sh. No run-mode tokens in the goal — conductor.py gets it via the --run-mode flag.
mode="${CONDUCTOR_RUN_MODE:-dry}"
case "$mode" in live|dry) ;; *) mode="dry" ;; esac

# Distribute (default): detach the mission into its own mission/<slug> herdr workspace
# — orchestrator + workers tile together (the watchable view), reports to Slack on done.
# Foreground: run the orchestrator right here in this pane (dies if the pane closes).
# A `fg!` goal prefix forces foreground and skips the prompt (scripted/quick runs). No TTY
# (piped/non-interactive) → read hits EOF → default distribute, no hang.
dist_flag="--distribute"
if [ "${goal%% *}" = "fg!" ]; then
  dist_flag=""; goal="${goal#fg! }"
else
  printf 'Distribute into its own mission workspace? [Y/n] '
  IFS= read -r _d || true
  case "$_d" in [Nn]*) dist_flag="" ;; esac
fi

cd "$REPO/agent-runner" 2>/dev/null || { echo "cannot cd to $REPO/agent-runner"; read -r _; exit 1; }

echo "▶ Conductor mission"
echo "  host:  $host"
echo "  repo:  $REPO"
echo "  mode:  $mode"
echo "  dispatch: $([ -n "$dist_flag" ] && echo 'distributed → own mission workspace' || echo 'foreground → this pane')"
echo "  goal:  $goal"
echo "────────────────────────────────────────────────────────────"
if [ -x .venv/bin/python ]; then
  .venv/bin/python conductor.py $dist_flag --run-mode "$mode" "$goal"
else
  task conductor -- $dist_flag --run-mode "$mode" "$goal"
fi
rc=$?
echo "────────────────────────────────────────────────────────────"
echo "mission process exited ($rc). Press Enter to close this window…"
read -r _
