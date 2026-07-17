#!/usr/bin/env bash
# herdr popup entrypoint for the fleet repo picker.
# Thin wrapper: robust PATH (herdr server env is stripped) + force the herdr
# substrate backend, then hand to the real launch-claude.sh (fzf repo/worktree
# picker → spawns via open-claude.sh so checkpoint/memory/context is injected).
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_SUBSTRATE=herdr
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
exec "$NEXUS_TMUX_DIR/launch-claude.sh"
