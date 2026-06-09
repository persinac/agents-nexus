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

# Project name: git toplevel basename, else cwd basename, else "general".
PROJECT="general"
if [ -n "$CWD" ] && cd "$CWD" 2>/dev/null; then
  PROJECT="$(basename "$(git rev-parse --show-toplevel 2>/dev/null || echo "$CWD")")"
fi

# Pull DATABASE_URL (headless mnemon MCP → Postgres) and ANTHROPIC_API_KEY
# (the detached `claude -p --bare` has no interactive login) from .env.
DB_URL=""
API_KEY=""
if [ -f "$NEXUS_DIR/.env" ]; then
  DB_URL="$(grep -E '^DATABASE_URL=' "$NEXUS_DIR/.env" | head -1 | cut -d= -f2-)"
  API_KEY="$(grep -E '^ANTHROPIC_API_KEY=' "$NEXUS_DIR/.env" | head -1 | cut -d= -f2-)"
fi
API_KEY="${ANTHROPIC_API_KEY:-$API_KEY}"
[ -n "$API_KEY" ] || exit 0

# ── Extract a compact recent slice of the conversation (text only). ──────────
RECENT="$(tail -n 250 "$TRANSCRIPT" 2>/dev/null \
  | jq -rc 'select(.message.role=="user" or .message.role=="assistant")
            | .message.role + ": " +
              (if (.message.content|type)=="string" then .message.content
               else ([.message.content[]? | select(.type=="text") | .text] | join(" ")) end)' 2>/dev/null \
  | grep -vE '^(user|assistant): *$' \
  | tail -n 40 \
  | cut -c1-2000)"
RECENT="$(printf '%s' "$RECENT" | tail -c 9000)"
[ -n "$RECENT" ] || exit 0

PROMPT="You are an autonomous memory curator. Below is the tail of a Claude Code session for project \"$PROJECT\" (lines prefixed user:/assistant:). If — and ONLY if — it contains durable, reusable knowledge worth recalling in FUTURE sessions (a decision + rationale, a non-obvious fix or gotcha, a discovered constraint, or a notable outcome), call mcp__agent-memory__create_note exactly once with: project=\"$PROJECT\", a specific title, concise content (include **Why:** and **How to apply:** lines when relevant), and 2-5 lowercase tags. Be conservative: routine Q&A, status checks, file listings, or trivial edits are NOT worth saving — in that case call no tool and reply with the single word: skip. Never call the tool more than once."

# ── Detach the judgment so the session is never blocked. ─────────────────────
LOG="$STATE_DIR/run.log"
nohup env CLAUDE_AUTO_CHECKPOINT=1 ANTHROPIC_API_KEY="$API_KEY" ${DB_URL:+DATABASE_URL="$DB_URL"} \
  "$CLAUDE_BIN" -p "$PROMPT" \
    --bare \
    --model haiku \
    --allowedTools "mcp__agent-memory__create_note" \
    --mcp-config "$MCP_CONFIG" \
    --output-format text \
  >>"$LOG" 2>&1 <<< "$RECENT" &
disown 2>/dev/null || true

exit 0
