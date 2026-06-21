#!/usr/bin/env bash
# agent-keep.sh — pin/unpin an agent window so the overseer reaper never closes
# it, even under REAP_ALL=1. Sets the `@keep` window option that overseer-reap.sh
# treats as an always-honored exclusion.
#
# Usage:
#   agent-keep.sh                 # list currently pinned (@keep) windows
#   agent-keep.sh list            # same
#   agent-keep.sh <target>        # pin <target>      (@keep 1)
#   agent-keep.sh <target> on     # pin <target>
#   agent-keep.sh <target> off    # unpin <target>    (@keep 0)
#
# <target> is an agent NAME, a window slot (index), or a %pane id. Names are
# resolved against the tmux registry (~/.tmux/registry/*); a %pane or slot is
# passed through to tmux directly.
set -uo pipefail

SESSION="${TMUX_SESSION:-agents}"
REGISTRY_DIR="$HOME/.tmux/registry"

command -v tmux >/dev/null 2>&1 || { echo "agent-keep: tmux not found" >&2; exit 1; }
tmux has-session -t "$SESSION" 2>/dev/null || { echo "agent-keep: no tmux session '$SESSION'" >&2; exit 1; }

# Resolve a NAME to its PANE_ID via the registry; echo the pane or empty.
resolve_name() {
  local want="$1" name pane
  [ -d "$REGISTRY_DIR" ] || return 0
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    name=""; pane=""
    while IFS='=' read -r k v; do
      case "$k" in NAME) name="$v" ;; PANE_ID) pane="$v" ;; esac
    done < "$f"
    if [ "$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$want" | tr '[:upper:]' '[:lower:]')" ]; then
      printf '%s' "$pane"; return 0
    fi
  done
}

list_kept() {
  local any=0
  while IFS=$'\t' read -r wid wname keep; do
    if [ "$keep" = "1" ]; then
      printf '  @keep  %-24s %s\n' "$wname" "$wid"; any=1
    fi
  done < <(tmux list-windows -t "$SESSION" -F '#{window_id}	#{window_name}	#{@keep}' 2>/dev/null)
  [ "$any" = "1" ] || echo "  (no pinned windows)"
}

# No target → list.
if [ "$#" -eq 0 ] || [ "${1:-}" = "list" ]; then
  echo "Pinned (@keep) windows in session '$SESSION':"
  list_kept
  exit 0
fi

TARGET="$1"
STATE="${2:-on}"
case "$STATE" in
  on|1|yes|true)   VAL=1 ;;
  off|0|no|false)  VAL=0 ;;
  *) echo "agent-keep: state must be on|off (got '$STATE')" >&2; exit 2 ;;
esac

# Resolve the target to a tmux target spec.
case "$TARGET" in
  %*)            TGT="$TARGET" ;;                      # pane id
  *[!0-9]*)      pane="$(resolve_name "$TARGET")"      # has non-digits → a name
                 TGT="${pane:-$SESSION:$TARGET}" ;;    # fall back to window-name target
  *)             TGT="$SESSION:$TARGET" ;;             # all digits → slot/window index
esac

if ! tmux set-window-option -t "$TGT" @keep "$VAL" 2>/dev/null; then
  echo "agent-keep: could not set @keep on '$TARGET' (resolved '$TGT') — not found?" >&2
  exit 1
fi

if [ "$VAL" = "1" ]; then
  echo "pinned '$TARGET' (@keep 1) — the reaper will not close it, even under REAP_ALL=1"
else
  echo "unpinned '$TARGET' (@keep 0) — now subject to normal reaping"
fi
