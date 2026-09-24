#!/usr/bin/env bash
# herdr's native close_workspace SIGKILLs every pane and fires no pane-died event, so agents
# would survive as fake crash casualties in ~/.tmux/registry (six did on 2026-09-11).
set -uo pipefail
HERDR_BIN="${HERDR_BIN:-herdr}"
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
ws="${1:-}"
if [ -z "$ws" ]; then
  ws="$("$HERDR_BIN" pane current --current 2>/dev/null | jq -r '.result.pane.workspace_id // empty' 2>/dev/null)"
fi
[ -n "$ws" ] || { echo "herdr-close-workspace: no workspace resolved" >&2; exit 0; }

panes="$("$HERDR_BIN" pane list --workspace "$ws" 2>/dev/null | jq -r '.result.panes[]? | "\(.pane_id)\t\(.cwd // "")"' 2>/dev/null)"
while IFS="$(printf '\t')" read -r pane cwd; do
  [ -n "$pane" ] || continue
  if [ -x "$NEXUS_TMUX_DIR/agent-deregister.sh" ]; then
    "$NEXUS_TMUX_DIR/agent-deregister.sh" "$pane" 2>/dev/null || true
  fi
  if [ -n "$cwd" ] && [ -x "$NEXUS_TMUX_DIR/worktree-cleanup.sh" ]; then
    "$NEXUS_TMUX_DIR/worktree-cleanup.sh" "$cwd" 2>/dev/null || true
  fi
done <<EOF
$panes
EOF

exec "$HERDR_BIN" workspace close "$ws"
