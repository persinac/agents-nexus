#!/usr/bin/env bash
# auto-checkpoint.sh — Stop hook (async): background, selective memory checkpoint.
#
# On session stop, if this session hasn't been checkpointed recently, detach a
# small headless Claude (haiku) that reads the recent transcript and ONLY calls
# mcp__agent-memory__create_note when there is durable, reusable knowledge worth
# remembering. Fails open everywhere (never blocks or errors the session).
set -uo pipefail

# ── Recursion guard 1: set when we spawn the headless judge below. ───────────
[ -n "${CLAUDE_AUTO_CHECKPOINT:-}" ] && exit 0

INPUT="$(cat 2>/dev/null)" || exit 0
command -v jq >/dev/null 2>&1 || exit 0

# ── Recursion guard 2: Claude-native flag (true when already continuing). ────
[ "$(printf '%s' "$INPUT" | jq -r '.stop_hook_active // false')" = "true" ] && exit 0

SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // "unknown"')"
TRANSCRIPT="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty')"
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty')"
[ -n "$TRANSCRIPT" ] || exit 0
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"
[ -f "$TRANSCRIPT" ] || exit 0

NEXUS_DIR="${AGENTS_NEXUS_DIR:-$HOME/garner/repos/agents-nexus}"
STATE_DIR="$HOME/.claude/auto-checkpoint"
MCP_CONFIG="$STATE_DIR/mcp.json"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
[ -x "$CLAUDE_BIN" ] || CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
[ -n "$CLAUDE_BIN" ] || exit 0
[ -f "$MCP_CONFIG" ] || exit 0
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

# ── Throttle: at most one checkpoint per session per THROTTLE_SECS. ──────────
THROTTLE_SECS="${AUTO_CHECKPOINT_THROTTLE_SECS:-1200}"   # 20 minutes
MARK="$STATE_DIR/${SESSION_ID}.last"
now="$(date +%s)"
if [ -f "$MARK" ]; then
  last="$(cat "$MARK" 2>/dev/null || echo 0)"
  [ $(( now - last )) -lt "$THROTTLE_SECS" ] && exit 0
fi
printf '%s' "$now" > "$MARK" 2>/dev/null || true

# ── Detach the curator (shared core) so the session is never blocked. ────────
# The core derives the project, pulls .env creds, extracts the transcript tail,
# and runs the headless haiku judge. Stays here as a thin throttled wrapper.
CORE="$NEXUS_DIR/scripts/checkpoint-transcript.sh"
[ -x "$CORE" ] || exit 0
nohup "$CORE" --transcript "$TRANSCRIPT" --cwd "$CWD" --label "stop-hook" \
  >/dev/null 2>&1 &
disown 2>/dev/null || true

exit 0
