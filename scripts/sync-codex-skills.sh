#!/usr/bin/env bash
# Sync the fleet's Claude skills → codex skills (Tier 5, codex multi-vendor).
#
# Claude and codex skills share the SKILL.md format, so we SYMLINK each compatible skill
# (~/.codex/skills/<name> → ~/.claude/skills/<name>) — edits to the Claude skill propagate,
# no drift. This makes the fleet's procedures available to ad-hoc `codex` sessions and to
# future interactive codex agents (Tier 3). Headless conductor codex workers don't need this
# — they already inline the SKILL.md body (Tier 2, gap F).
#
# SKIP-list: skills whose CORE function is a Claude-only MCP server codex can't consume
# (P3 — our agent-memory/spark/etc. are SSE; codex speaks streamable-HTTP only). Skills that
# merely have an OPTIONAL MCP step (excalidraw upload, ui-ux Skill-composition) are still
# synced — their Bash/Read/Write core works under codex, the optional step just degrades.
#
# Idempotent: safe to re-run (e.g. from install.sh). Usage: scripts/sync-codex-skills.sh
set -euo pipefail

CLAUDE_SKILLS="${CLAUDE_SKILLS:-$HOME/.claude/skills}"
CODEX_SKILLS="${CODEX_SKILLS:-$HOME/.codex/skills}"

# name → reason. These need Claude-only MCP as their primary purpose → not usable under codex.
declare -A SKIP=(
  [coordinator]="every tool is MCP (Google Calendar/Gmail/Drive/Slack); nothing works without MCP"
  [checkpoint]="core output is an agent-memory MCP note (mcp__agent-memory__*); codex has no MCP"
)

[ -d "$CLAUDE_SKILLS" ] || { echo "no $CLAUDE_SKILLS — nothing to sync"; exit 0; }
mkdir -p "$CODEX_SKILLS"

linked=0 skipped=0
for dir in "$CLAUDE_SKILLS"/*/; do
  src="${dir%/}"
  name="$(basename "$src")"
  [ -f "$src/SKILL.md" ] || continue
  if [[ -n "${SKIP[$name]:-}" ]]; then
    # tear down any stale link we made in a previous run
    [ -L "$CODEX_SKILLS/$name" ] && rm -f "$CODEX_SKILLS/$name"
    printf '  skip  %-18s — %s\n' "$name" "${SKIP[$name]}"
    skipped=$((skipped + 1))
    continue
  fi
  ln -sfn "$src" "$CODEX_SKILLS/$name"   # -f -n: replace in place, never nest inside an existing link
  printf '  link  %-18s → %s\n' "$name" "$src"
  linked=$((linked + 1))
done

echo "codex skills: ${linked} linked, ${skipped} skipped  (${CODEX_SKILLS})"
