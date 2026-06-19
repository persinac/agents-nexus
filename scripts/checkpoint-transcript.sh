#!/usr/bin/env bash
# checkpoint-transcript.sh — run the memory-curator (headless haiku) over a
# Claude Code transcript and create a memory note IFF there's durable knowledge.
#
# Shared core for two callers:
#   - the auto-checkpoint Stop hook (claude-hooks/auto-checkpoint.sh) — runs it
#     detached so the session is never blocked, after its own throttle/guards;
#   - the overseer reaper (scripts/overseer-reap.sh) — runs it in the foreground
#     for a final checkpoint before closing an idle agent.
#
# This core does NOT throttle or read stdin — the caller owns those policies.
# Fails open everywhere: any missing precondition exits 0 quietly.
#
# Usage: checkpoint-transcript.sh --transcript PATH [--cwd DIR] [--label TAG]
set -uo pipefail

TRANSCRIPT="" CWD="" LABEL="checkpoint"
while [ $# -gt 0 ]; do
  case "$1" in
    --transcript) TRANSCRIPT="${2:-}"; shift 2 ;;
    --cwd)        CWD="${2:-}";        shift 2 ;;
    --label)      LABEL="${2:-}";      shift 2 ;;
    *) shift ;;
  esac
done

command -v jq >/dev/null 2>&1 || exit 0
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

# ── Run the judgment in the FOREGROUND. The caller decides whether to detach. ─
LOG="$STATE_DIR/run.log"
printf '%s [%s] checkpoint project=%s transcript=%s\n' "$(date -u +%FT%TZ)" "$LABEL" "$PROJECT" "$TRANSCRIPT" >> "$LOG" 2>/dev/null || true
env CLAUDE_AUTO_CHECKPOINT=1 ANTHROPIC_API_KEY="$API_KEY" ${DB_URL:+DATABASE_URL="$DB_URL"} \
  "$CLAUDE_BIN" -p "$PROMPT" \
    --bare \
    --model haiku \
    --allowedTools "mcp__agent-memory__create_note" \
    --mcp-config "$MCP_CONFIG" \
    --output-format text \
  >>"$LOG" 2>&1 <<< "$RECENT"

exit 0
