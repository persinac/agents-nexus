#!/usr/bin/env bash
# herdr-close-agent.sh — herdr's missing pane-died analog, fired at close TIME.
#
# THE GAP. herdr emits no pane-died / pane-closed event (its only subscribable
# events are pane.output_matched | pane.agent_status_changed | pane.scroll_changed),
# so a deliberately-closed agent never runs agent-deregister — unlike tmux, whose
# `pane-died` hook in tmux.conf deregisters + cleans the worktree on close. The stale
# ~/.tmux/registry/<pane> entry is then indistinguishable from a crash casualty, so
# herdr-recover treats it as one and RESPAWNS the agent you just closed.
#
# THE FIX. Bind this to the close chord (it overrides herdr's native close_pane):
# it resolves the target pane, DEREGISTERS it (+ worktree cleanup) exactly like the
# tmux pane-died hook, THEN closes the herdr pane. A deliberate close therefore leaves
# no casualty and herdr-recover never revives it — independent of HERDR_RECOVER_ALL.
# A herdr SERVER crash (no close keypress) still leaves entries for recover to bring
# the fleet back, so the deliberate-close vs. crash cases stay cleanly separated.
#
# Only wired in herdr mode (this shim lives in the herdr config.toml); tmux keeps its
# native pane-died hook, so this is never invoked there.
#
# Usage: herdr-close-agent.sh [<pane_id>]
#   no arg   → the focused pane (`herdr pane current --current`); how the keybinding calls it
#   <pane_id>→ close a specific pane (used by tests / scripted closes)
#
# Env: HERDR_BIN (absolute herdr path — the herdr server runs with a stripped PATH, so
#      a bare `herdr` may not resolve), NEXUS_TMUX_DIR (default ~/.tmux).
set -uo pipefail

HERDR_BIN="${HERDR_BIN:-herdr}"
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"

pane="${1:-}"
cwd=""

# Resolve the target pane's id + cwd BEFORE closing it — once closed it is ungettable.
if [ -n "$pane" ]; then
  info="$("$HERDR_BIN" pane get "$pane" 2>/dev/null)" || info=""
else
  info="$("$HERDR_BIN" pane current --current 2>/dev/null)" || info=""
  pane="$(printf '%s' "$info" | jq -r '.result.pane.pane_id // empty' 2>/dev/null)"
fi
cwd="$(printf '%s' "$info" | jq -r '.result.pane.cwd // empty' 2>/dev/null)"

# No pane resolved → do nothing (fail-safe: never blind-close on a bad lookup).
[ -n "$pane" ] || { echo "herdr-close-agent: no target pane resolved" >&2; exit 0; }

# Deregister (removes the ~/.tmux/registry/<pane> entry + logs session_end) and clean
# the worktree — the exact scripts the tmux pane-died hook runs. Best-effort: a failure
# here must not block the close.
if [ -x "$NEXUS_TMUX_DIR/agent-deregister.sh" ]; then
  "$NEXUS_TMUX_DIR/agent-deregister.sh" "$pane" 2>/dev/null || true
fi
if [ -n "$cwd" ] && [ -x "$NEXUS_TMUX_DIR/worktree-cleanup.sh" ]; then
  "$NEXUS_TMUX_DIR/worktree-cleanup.sh" "$cwd" 2>/dev/null || true
fi

# Now close the pane in herdr.
exec "$HERDR_BIN" pane close "$pane"
