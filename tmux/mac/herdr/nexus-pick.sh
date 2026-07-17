#!/usr/bin/env bash
# herdr entrypoint for the fleet repo picker.
#
# Thin wrapper: set a robust PATH (the herdr server's is stripped) + force the
# herdr substrate backend, then hand off to the REAL launch-claude.sh. That gives
# herdr the same picker as tmux — worktree listing, conflict detection, and (the
# important part) spawning through open-claude.sh so checkpoint/memory/context is
# injected. launch-claude.sh's `tmux new-window` calls are routed through
# substrate.sh, which does `herdr agent start` when NEXUS_SUBSTRATE=herdr.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_SUBSTRATE=herdr
exec "$HOME/.tmux/launch-claude.sh"
