#!/usr/bin/env bash
# nexus.fleet: spawn a no-repo "general" scratch agent (cwd=$HOME, PROJECT_SLUG=general,
# cross-project memory) through the substrate seam — the herdr analog of the old tmux
# `ctrl+a G`. Headless action, fire-and-forget; --focus switches to the new agent.
# Mirrors launch-claude.sh's general spawn. Name collisions auto-suffix at the seam.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_SUBSTRATE="${NEXUS_SUBSTRATE:-herdr}"
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
exec "$NEXUS_TMUX_DIR/substrate.sh" spawn general "$HOME" \
  "env PROJECT_SLUG=general $NEXUS_TMUX_DIR/open-claude.sh" --focus
