#!/usr/bin/env bash
# memory-status.sh — launch the memory health panel (Windows/MSYS2 version)
_AGENT_MEM_VENV="$HOME/garner/repos/agents-nexus/mnemon/.venv"
if [ -x "$_AGENT_MEM_VENV/bin/python3" ]; then
  PYTHON="$_AGENT_MEM_VENV/bin/python3"
elif [ -x "$_AGENT_MEM_VENV/Scripts/python3.exe" ]; then
  PYTHON="$_AGENT_MEM_VENV/Scripts/python3.exe"
else
  PYTHON="python3"
fi
exec "$PYTHON" "$HOME/.tmux/memory-status.py"
