#!/usr/bin/env bash
# ctrl+a shift+C target. A file, not an inline binding: the goal would otherwise need
# re-escaping through TOML, herdr's sh -c, and the pane shell.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
read -r -p "conductor goal: " g
[ -n "$g" ] || { echo "no goal given — aborting." >&2; exit 1; }
exec "$NEXUS_TMUX_DIR/conductor-run.sh" "$g"
