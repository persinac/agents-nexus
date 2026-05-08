#!/usr/bin/env bash
# Auto-cache the current conversation's tail to ~/.tmux/cache/<project>.md.
# Called from hook-stop.sh after every assistant turn.
# Fire-and-forget — always exits 0, never blocks Claude.
#
# Usage:  hook-autocache.sh /path/to/project/dir

CWD="${1:-$PWD}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Resolve Python — prefer the mnemon venv (same as open-claude.sh)
_VENV="${AGENTS_NEXUS_DIR:-$HOME/repos/agents-nexus}/mnemon/.venv"
if [ -x "$_VENV/bin/python3" ]; then
  PY="$_VENV/bin/python3"
elif [ -x "$_VENV/Scripts/python3.exe" ]; then
  PY="$_VENV/Scripts/python3.exe"
else
  PY="python3"
fi

"$PY" "$SCRIPT_DIR/autocache.py" "$CWD" &

exit 0
