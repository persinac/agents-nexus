#!/usr/bin/env bash
# Fuzzy repo picker → workspace-bucket spawn (herdr front door). Linux twin of the mac one:
# forces NEXUS_SUBSTRATE=herdr, ensures node/herdr/fzf are on PATH, execs the shared launcher.
# The picker + bucket prompt live in the shared launch-claude.sh, so both platforms match.
export PATH="$HOME/.local/bin:$HOME/.local/share/fnm/aliases/default/bin:/usr/local/bin:/usr/bin:$PATH"
export NEXUS_SUBSTRATE=herdr
exec "$HOME/.tmux/launch-claude.sh"
