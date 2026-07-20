#!/usr/bin/env bash
# herdr popup entrypoint for the fuzzy worktree-prune (tmux `ctrl+a W` parity).
# Thin wrapper: robust PATH (herdr server env is stripped) then hand to the shared
# ~/.tmux/worktree-prune.sh (fzf multi-select over $REPO_DIR/.worktrees → git worktree
# remove). Single source of truth — the same script the old tmux binding used.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:$PATH"
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
exec "$NEXUS_TMUX_DIR/worktree-prune.sh"
