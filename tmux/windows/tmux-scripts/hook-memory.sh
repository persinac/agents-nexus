#!/usr/bin/env bash
# Append a memory event to the local buffer. Called from Claude Code hooks.
# Fire-and-forget — always exits 0, never blocks Claude.
#
# Usage (from claude-settings.json hooks):
#   $HOME/.tmux/hook-memory.sh tool_use
#   $HOME/.tmux/hook-memory.sh session_idle
#   $HOME/.tmux/hook-memory.sh permission_wait

EVENT_TYPE="${1:-unknown}"

# Capture stdin before backgrounding (Claude Code pipes hook JSON here)
INPUT=$(cat 2>/dev/null)

# Resolve MSYS2 home for consistent event file location
HOME_DIR="${HOME:-/home/$USER}"
case "$HOME_DIR" in
  /home/*) HOME_DIR="/c/msys64${HOME_DIR}" ;;
esac
export TMUX_HOME="$HOME_DIR/.tmux"

# Find Python — mingw64 bin isn't always on PATH when called from Claude Code
PYTHON=""
for p in /c/msys64/mingw64/bin/python3.exe /c/msys64/usr/bin/python3.exe /usr/bin/python3 python3; do
  [ -x "$p" ] && { PYTHON="$p"; break; }
done
[ -z "$PYTHON" ] && exit 0

# Run in background so we never block the hook caller
echo "$INPUT" | "$PYTHON" "$HOME/.tmux/memory-hook.py" "$EVENT_TYPE" "${TMUX_PANE:-}" &

exit 0
