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

# Run in background so we never block the hook caller
echo "$INPUT" | "$HOME/.tmux/memory-hook.py" "$EVENT_TYPE" "${TMUX_PANE:-}" &

exit 0
