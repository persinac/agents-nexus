#!/usr/bin/env bash
# herdr plugin pane: live memory-system health (wraps the fleet's memory-status.sh,
# which runs memory-status.py — a refreshing TUI). Sources the env layer so
# AGENTS_NEXUS_DIR / DATABASE_URL resolve; the panel itself degrades to a
# "DATABASE_URL not set" view if the DB is unreachable.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
# set -a: auto-export everything the env files set (AGENTS_NEXUS_DIR, DATABASE_URL, …)
# so it propagates to memory-status.sh, which runs as a fresh process via exec. Without
# this, that script falls back to a wrong ~/repos path and the venv python is missing.
set -a
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && . "$NEXUS_TMUX_DIR/env.defaults.sh"
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && . "$NEXUS_TMUX_DIR/env.sh"
set +a
exec "$NEXUS_TMUX_DIR/memory-status.sh"
