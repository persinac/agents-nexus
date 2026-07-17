#!/usr/bin/env bash
# herdr plugin pane: live APM/fleet dashboard. stats.sh prints one snapshot per call
# (herdr-aware active-agent count via the substrate seam), so we loop it for a live
# panel. Close the pane with your herdr pane-close key.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
# set -a so AGENTS_NEXUS_DIR/DATABASE_URL/etc. reach stats.sh (a fresh process each loop).
set -a
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && . "$NEXUS_TMUX_DIR/env.defaults.sh"
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && . "$NEXUS_TMUX_DIR/env.sh"
set +a
export NEXUS_SUBSTRATE="${NEXUS_SUBSTRATE:-herdr}"
trap 'exit 0' INT TERM
while :; do
  "$NEXUS_TMUX_DIR/stats.sh"
  sleep 2
done
