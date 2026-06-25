#!/usr/bin/env bash
# agent-cohort.sh — protect a GROUP of agents as a named "design cohort" so the
# overseer reaper never closes them mid-flight, then release the whole group in
# one shot when the work ships. Sets the `@cohort` window option that
# overseer-reap.sh treats as an always-honored exclusion (even under REAP_ALL=1).
#
# For when one orchestrator drives a multi-repo design across several agents that
# sit idle between bus round-trips — a normal idle-reap would sweep them.
#
# Usage:
#   agent-cohort.sh                          # list active cohorts + members
#   agent-cohort.sh list                     # same
#   agent-cohort.sh hold <design> <t…|all>   # tag each target with @cohort=<design>
#   agent-cohort.sh release <design>         # clear @cohort on every member of <design>
#   agent-cohort.sh release <design> <t…>    # drop specific members from <design>
#
# <target> is an agent NAME (resolved via ~/.tmux/registry/*), a window slot, or a
# %pane id. `all` (hold) tags every live registered agent in the session.
#
# Composes with @keep: independent tags, so `release <design>` never disturbs a
# window pinned manually via agent-keep.sh.
set -uo pipefail

SESSION="${TMUX_SESSION:-agents}"
REGISTRY_DIR="$HOME/.tmux/registry"

command -v tmux >/dev/null 2>&1 || { echo "agent-cohort: tmux not found" >&2; exit 1; }
tmux has-session -t "$SESSION" 2>/dev/null || { echo "agent-cohort: no tmux session '$SESSION'" >&2; exit 1; }

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

# name | slot | %pane -> tmux target spec
to_target() {
  case "$1" in
    %*)        printf '%s' "$1" ;;
    *[!0-9]*)  local p; p="$(resolve_name "$1")"; printf '%s' "${p:-$SESSION:$1}" ;;
    *)         printf '%s' "$SESSION:$1" ;;
  esac
}

# secs -> 2h3m / 5m / 12s
fmt_age() {
  local s="$1"
  if   [ "$s" -ge 3600 ]; then printf '%dh%dm' $(( s / 3600 )) $(( (s % 3600) / 60 ))
  elif [ "$s" -ge 60 ];   then printf '%dm' $(( s / 60 ))
  else printf '%ds' "$s"; fi
}

list_cohorts() {
  local now any=0 wid wname coh since age
  now="$(date +%s)"
  while IFS=$'\t' read -r wid wname coh since; do
    [ -n "$coh" ] || continue
    case "$since" in ''|*[!0-9]*) age="?" ;; *) age="$(fmt_age $(( now - since )))" ;; esac
    printf '  %-18s %-24s %s  (held %s)\n' "$coh" "$wname" "$wid" "$age"; any=1
  done < <(tmux list-windows -t "$SESSION" -F '#{window_id}	#{window_name}	#{@cohort}	#{@cohort_since}' 2>/dev/null | sort)
  [ "$any" = "1" ] || echo "  (no active cohorts)"
}

ACTION="${1:-list}"
if [ "$#" -ge 1 ]; then shift; fi      # remaining args: <label> [targets…]

case "$ACTION" in
  list|"")
    echo "Active design cohorts in session '$SESSION':"
    list_cohorts
    ;;

  hold)
    LABEL="${1:-}"; if [ "$#" -ge 1 ]; then shift; fi
    if [ -z "$LABEL" ] || [ "$#" -lt 1 ]; then
      echo "usage: agent-cohort.sh hold <design> <target…|all>" >&2; exit 2
    fi
    NOW="$(date +%s)"
    TARGETS=("$@")
    if [ "${1:-}" = "all" ]; then           # expand to every live registered agent
      TARGETS=()
      for f in "$REGISTRY_DIR"/*; do
        [ -f "$f" ] || continue
        p=""; while IFS='=' read -r k v; do [ "$k" = "PANE_ID" ] && p="$v"; done < "$f"
        [ -n "$p" ] && tmux display-message -t "$p" -p '' >/dev/null 2>&1 && TARGETS+=("$p")
      done
    fi
    n=0
    for t in "${TARGETS[@]}"; do
      tgt="$(to_target "$t")"
      if tmux set-window-option -t "$tgt" @cohort "$LABEL" 2>/dev/null; then
        tmux set-window-option -t "$tgt" @cohort_since "$NOW" 2>/dev/null
        echo "held '$t' -> cohort '$LABEL'"; n=$(( n + 1 ))
      else
        echo "skip '$t' — not found (resolved '$tgt')" >&2
      fi
    done
    echo "cohort '$LABEL': $n agent(s) protected from the reaper (even REAP_ALL=1). Release: agent-cohort.sh release $LABEL"
    ;;

  release)
    LABEL="${1:-}"; if [ "$#" -ge 1 ]; then shift; fi
    [ -n "$LABEL" ] || { echo "usage: agent-cohort.sh release <design> [target…]" >&2; exit 2; }
    if [ "$#" -ge 1 ]; then                  # release specific members
      for t in "$@"; do
        tgt="$(to_target "$t")"
        cur="$(tmux show-options -wqv -t "$tgt" @cohort 2>/dev/null)"
        if [ "$cur" = "$LABEL" ]; then
          tmux set-window-option -u -t "$tgt" @cohort 2>/dev/null
          tmux set-window-option -u -t "$tgt" @cohort_since 2>/dev/null
          echo "released '$t' from cohort '$LABEL'"
        else
          echo "skip '$t' — not in cohort '$LABEL' (@cohort='${cur:-}')" >&2
        fi
      done
    else                                     # release the whole cohort
      n=0
      while IFS=$'\t' read -r wid coh; do
        [ "$coh" = "$LABEL" ] || continue
        tmux set-window-option -u -t "$wid" @cohort 2>/dev/null
        tmux set-window-option -u -t "$wid" @cohort_since 2>/dev/null
        n=$(( n + 1 ))
      done < <(tmux list-windows -t "$SESSION" -F '#{window_id}	#{@cohort}' 2>/dev/null)
      echo "released cohort '$LABEL' — $n agent(s) back to normal reaping"
    fi
    ;;

  *)
    echo "agent-cohort: unknown action '$ACTION' (use: list | hold | release)" >&2
    exit 2
    ;;
esac
