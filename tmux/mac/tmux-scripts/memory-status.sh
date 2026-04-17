#!/usr/bin/env bash
# memory-status.sh — launch the memory health panel
# Run directly or via: tmux split-window -h -l 52 "$HOME/.tmux/memory-status.sh"
PYTHON="$HOME/minions/minions-suite/agent-memory/.venv/bin/python3"
SCRIPT="$(dirname "$0")/memory-status.py"
exec "$PYTHON" "$SCRIPT"
