#!/usr/bin/env bash
# agent-auto.sh — flag an agent as @autoaccept so the permission gate approves its
# tool calls WITHOUT a model call. Sets the `@autoaccept` pane option that
# notify-classify.py reads (see _autoaccept_pane there for the policy).
#
# What the flag buys: notify-classify's LLM tier is skipped entirely for this pane.
# That is the tier a classifier timeout turns into a spurious denial, so a flagged
# agent keeps working through a flaky network instead of stopping on a prompt.
#
# What it does NOT do, so nobody sets it and expects the wrong thing:
#   - It does not weaken _bash_is_denied. rm / kubectl delete / DROP TABLE /
#     terraform destroy still fall through to the classifier and still ask a human.
#   - It does not touch AskUserQuestion. That is the agent asking YOU something, not
#     a permission decision; it still surfaces.
#   - It does not rescue a NATIVE Claude Code auto-mode denial — those hard-deny and
#     never raise an answerable prompt (see automode-watchdog.py). Run flagged panes
#     in Manual mode so every call routes through this gate.
#   - It does not bypass the PreToolUse hooks. block-destructive.sh and
#     block-credential-dump.sh still apply, flagged or not.
#
# ACCEPTED TRADE, stated plainly: on a flagged pane a Write to a credential-ish path
# (.env, *.pem, kubeconfig) and a mutating MCP call (Slack post, Trello write) are
# auto-approved. Only the Bash denylists hold. Turn it off when you are done.
#
# Usage:
#   agent-auto.sh                 # list currently flagged (@autoaccept) panes
#   agent-auto.sh list            # same
#   agent-auto.sh <target>        # flag <target>     (@autoaccept 1)
#   agent-auto.sh <target> on     # flag <target>
#   agent-auto.sh <target> off    # unflag <target>   (@autoaccept 0)
#
# <target> is an agent NAME, a window slot (index), a %pane id, or a herdr wN:pN
# handle. Names are resolved against the registry (~/.tmux/registry/*).
#
# Composes with @keep and @cohort: independent tags, so flagging an agent never
# changes whether the reaper will close it, and vice versa.
set -uo pipefail

SESSION="${TMUX_SESSION:-agents}"
REGISTRY_DIR="$HOME/.tmux/registry"
SUBSTRATE="$HOME/.tmux/substrate.sh"
BACKEND="${NEXUS_SUBSTRATE:-herdr}"
OPT="@autoaccept"

# Precondition routed through the substrate seam, for the reason agent-keep.sh
# documents: in herdr mode there is no tmux `agents` session, so a raw
# `tmux has-session` guard would exit before the write and silently fail the flag.
"$SUBSTRATE" has-session 2>/dev/null || { echo "agent-auto: fleet substrate ($BACKEND) is not up" >&2; exit 1; }

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

list_flagged() {
  local any=0
  if [ "$BACKEND" = herdr ]; then
    # herdr has no `tmux list-windows`; enumerate the registry (the same source of
    # truth agent-keep.sh uses) and read the option per pane through the seam.
    local name pane
    for f in "$REGISTRY_DIR"/*; do
      [ -f "$f" ] || continue
      name=""; pane=""
      while IFS='=' read -r k v; do
        case "$k" in NAME) name="$v" ;; PANE_ID) pane="$v" ;; esac
      done < "$f"
      [ -n "$pane" ] || continue
      [ "$("$SUBSTRATE" pane-opt "$pane" "$OPT" 2>/dev/null)" = "1" ] || continue
      printf '  %s  %-24s %s\n' "$OPT" "${name:-?}" "$pane"; any=1
    done
    [ "$any" = "1" ] || echo "  (none flagged)"
    return
  fi
  while IFS=$'\t' read -r wid wname flag; do
    if [ "$flag" = "1" ]; then
      printf '  %s  %-24s %s\n' "$OPT" "$wname" "$wid"; any=1
    fi
  done < <(tmux list-windows -t "$SESSION" -F "#{window_id}	#{window_name}	#{${OPT}}" 2>/dev/null)
  [ "$any" = "1" ] || echo "  (none flagged)"
}

# No target → list.
if [ "$#" -eq 0 ] || [ "${1:-}" = "list" ]; then
  echo "Auto-accept ($OPT) panes in session '$SESSION':"
  list_flagged
  exit 0
fi

TARGET="$1"
STATE="${2:-on}"
case "$STATE" in
  on|1|yes|true)   VAL=1 ;;
  off|0|no|false)  VAL=0 ;;
  *) echo "agent-auto: state must be on|off (got '$STATE')" >&2; exit 2 ;;
esac

# Resolve the target to a substrate target spec.
case "$TARGET" in
  %*)                           TGT="$TARGET" ;;       # tmux pane id
  w[A-Za-z0-9]*:p[A-Za-z0-9]*)  TGT="$TARGET" ;;       # herdr pane handle (wN:pN)
  *[!0-9]*)      pane="$(resolve_name "$TARGET")"      # has non-digits → a name
                 TGT="${pane:-$SESSION:$TARGET}" ;;    # fall back to window-name target
  *)             TGT="$SESSION:$TARGET" ;;             # all digits → slot/window index
esac

# `set-opt` is the GENERIC seam verb (substrate.sh set-opt <pane> <@name> [value]),
# which is why this flag needed no new case in substrate.sh or substrated — both
# already fall through to a generic sidecar read/write for any @name.
if ! "$SUBSTRATE" set-opt "$TGT" "$OPT" "$VAL" 2>/dev/null; then
  echo "agent-auto: could not set $OPT on '$TARGET' (resolved '$TGT') — not found?" >&2
  exit 1
fi

if [ "$VAL" = "1" ]; then
  echo "flagged '$TARGET' ($OPT 1) — the gate will auto-approve without a model call."
  echo "  still asks: rm / delete / drop / destroy (_bash_is_denied) and AskUserQuestion."
  echo "  still approves without asking: credential-path writes, mutating MCP calls."
  echo "  turn off with: $(basename "$0") '$TARGET' off"
else
  echo "unflagged '$TARGET' ($OPT 0) — back to the normal gate (allowlist → permissive → LLM)."
fi
