#!/usr/bin/env bash
# Append a memory event to the local buffer. Called from Claude Code hooks.
# Fire-and-forget — always exits 0, never blocks Claude.
#
# Chained from hook-stop.sh, hook-pretooluse.sh, hook-notification.sh.
# Receives hook JSON on stdin.

EVENT_TYPE="${1:-unknown}"

# Capture stdin
INPUT=$(cat 2>/dev/null)

# Resolve MSYS2 home for consistent event file location
: "${USER:=${USERNAME:=$(whoami)}}"
HOME_DIR="${HOME:-/home/$USER}"
case "$HOME_DIR" in
  /home/*) HOME_DIR="/c/msys64${HOME_DIR}" ;;
  /c/Users/*) HOME_DIR="/c/msys64/home/$USER" ;;
esac
export TMUX_HOME="$HOME_DIR/.tmux"

# Find Python (use actual binary, not pyenv shim — shim needs pyenv on PATH)
WIN_USER="${USER:-${USERNAME:-$(whoami)}}"
PYTHON=""
for p in \
  "/c/Users/$WIN_USER/.pyenv/pyenv-win/versions"/*/python.exe \
  /c/msys64/mingw64/bin/python3.exe \
  /c/msys64/usr/bin/python3.exe \
  /usr/bin/python3 \
  python3; do
  [ -x "$p" ] && { PYTHON="$p"; break; }
done
[ -z "$PYTHON" ] && exit 0

# Run synchronously — backgrounding on Windows/MSYS2 kills the child when bash exits
echo "$INPUT" | "$PYTHON" "$TMUX_HOME/memory-hook.py" "$EVENT_TYPE" "${TMUX_PANE:-}" 2>/dev/null

exit 0
