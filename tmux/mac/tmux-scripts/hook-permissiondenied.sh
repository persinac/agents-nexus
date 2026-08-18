#!/usr/bin/env bash
# PermissionDenied hook: fires the instant Claude Code's auto mode denies a
# tool call, INCLUDING a denial with no classifier verdict (classifier
# unreachable/unparseable) — confirmed against Anthropic's own docs
# (code.claude.com/docs/en/hooks#permissiondenied), not assumed. That's
# exactly automode-watchdog.py's target failure mode, and this fires with zero
# poll-interval lag, synchronously, scoped to the exact pane that hit it (no
# cwd-based transcript-matching needed at all for this path — see the
# script's own docstring, "Detection is now dual").
#
# All logic lives in automode-watchdog.py --hook (one source of truth for the
# escalate/revert mechanics, shared with the poll-loop backstop) — this script
# is a thin pass-through of stdin. Exit code and stderr are ignored by Claude
# Code for PermissionDenied ("the denial already occurred" — it can't gate
# anything at this point), so there's nothing to report back either way.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# tmux sets TMUX_PANE; herdr sets HERDR_PANE_ID. Same fold every other hook in
# this repo does; passed through explicitly below rather than relied on as an
# inherited export, matching this repo's existing VAR=value-prefix convention
# (see hook-stop.sh's PANE="$TMUX_PANE" ... stop-classify.py call).
TMUX_PANE="${TMUX_PANE:-${HERDR_PANE_ID:-}}"

cat | TMUX_PANE="$TMUX_PANE" python3 "$SCRIPT_DIR/automode-watchdog.py" --hook >>"$HOME/.tmux/automode-hook.log" 2>&1
exit 0
