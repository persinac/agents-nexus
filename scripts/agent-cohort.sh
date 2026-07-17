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
SUBSTRATE="$HOME/.tmux/substrate.sh"
BACKEND="${NEXUS_SUBSTRATE:-herdr}"

# Precondition via the substrate seam (backend-aware). In herdr mode there is no
# tmux `agents` session, so the old `tmux has-session` guard exited before any
# `substrate cohort` hold/release could run — leaving a cohort the user thinks is
# protected but that was never tagged, exposed to the reaper.
"$SUBSTRATE" has-session 2>/dev/null || { echo "agent-cohort: fleet substrate ($BACKEND) is not up" >&2; exit 1; }

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
    %*)                           printf '%s' "$1" ;;   # tmux pane id
    w[A-Za-z0-9]*:p[A-Za-z0-9]*)  printf '%s' "$1" ;;   # herdr pane handle (wN:pN)
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
  if [ "$BACKEND" = herdr ]; then
    # Enumerate the registry; read @cohort/@cohort_since per pane through the seam.
    local name pane
    for f in "$REGISTRY_DIR"/*; do
      [ -f "$f" ] || continue
      name=""; pane=""
      while IFS='=' read -r k v; do
        case "$k" in NAME) name="$v" ;; PANE_ID) pane="$v" ;; esac
      done < "$f"
      [ -n "$pane" ] || continue
      coh="$("$SUBSTRATE" pane-opt "$pane" @cohort 2>/dev/null)"
      [ -n "$coh" ] || continue
      since="$("$SUBSTRATE" pane-opt "$pane" @cohort_since 2>/dev/null)"
      case "$since" in ''|*[!0-9]*) age="?" ;; *) age="$(fmt_age $(( now - since )))" ;; esac
      printf '  %-18s %-24s %s  (held %s)\n' "$coh" "${name:-?}" "$pane" "$age"; any=1
    done
    [ "$any" = "1" ] || echo "  (no active cohorts)"
    return
  fi
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
        # Liveness through the seam: a herdr handle fails a tmux display-message
        # probe, so the old check collected ZERO targets on herdr → `hold … all`
        # protected nothing. substrate pane-alive works for both backends.
        [ -n "$p" ] && "$SUBSTRATE" pane-alive "$p" 2>/dev/null && TARGETS+=("$p")
      done
    fi
    n=0
    for t in "${TARGETS[@]}"; do
      tgt="$(to_target "$t")"
      if "$HOME/.tmux/substrate.sh" cohort "$tgt" "$LABEL" 2>/dev/null; then
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
        cur="$("$HOME/.tmux/substrate.sh" pane-opt "$tgt" @cohort 2>/dev/null)"
        if [ "$cur" = "$LABEL" ]; then
          "$HOME/.tmux/substrate.sh" cohort "$tgt" --release 2>/dev/null
          echo "released '$t' from cohort '$LABEL'"
        else
          echo "skip '$t' — not in cohort '$LABEL' (@cohort='${cur:-}')" >&2
        fi
      done
    else                                     # release the whole cohort
      n=0
      if [ "$BACKEND" = herdr ]; then
        # herdr: the tmux list-windows scan returns empty, so nothing would be
        # released; enumerate the registry and release each matching pane.
        for f in "$REGISTRY_DIR"/*; do
          [ -f "$f" ] || continue
          p=""; while IFS='=' read -r k v; do [ "$k" = "PANE_ID" ] && p="$v"; done < "$f"
          [ -n "$p" ] || continue
          [ "$("$SUBSTRATE" pane-opt "$p" @cohort 2>/dev/null)" = "$LABEL" ] || continue
          "$SUBSTRATE" cohort "$p" --release 2>/dev/null
          n=$(( n + 1 ))
        done
      else
        while IFS=$'\t' read -r wid coh; do
          [ "$coh" = "$LABEL" ] || continue
          "$SUBSTRATE" cohort "$wid" --release 2>/dev/null
          n=$(( n + 1 ))
        done < <(tmux list-windows -t "$SESSION" -F '#{window_id}\t#{@cohort}' 2>/dev/null)
      fi
      echo "released cohort '$LABEL' — $n agent(s) back to normal reaping"
    fi
    ;;

  *)
    echo "agent-cohort: unknown action '$ACTION' (use: list | hold | release)" >&2
    exit 2
    ;;
esac
