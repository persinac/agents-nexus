#!/usr/bin/env bash
# herdr-recover.sh — reconcile + (opt-in) respawn the fleet after a herdr restart.
#
# THE PROBLEM. A herdr SERVER restart (brew services restart / launchd KeepAlive
# respawn / crash) kills the in-pane `claude` processes and churns pane ids — but,
# unlike tmux, herdr fires NO `pane-died` hook. So `agent-deregister` never runs:
# the ~/.tmux/registry/* entries, the agent-ledger, and the substrated cache all go
# stale, pointing at panes that no longer exist, and the agents themselves are gone
# with no one to bring them back. (tmux mode is immune — the tmux `pane-died` hook
# in tmux.conf handles dereg + worktree cleanup — so this tool no-ops there.)
#
# WHAT IT DOES. This is herdr's missing pane-died analog, plus recovery:
#   RECONCILE (always, safe): downgrade every ledger `live` entry with no backing
#     window (agent-ledger reconcile) so the rosters match reality again. Pure
#     bookkeeping — no windows opened or closed.
#   RECOVER  (opt-in): respawn each casualty from its last checkpoint via
#     open-claude.sh — which re-injects that repo's recent checkpoint notes + memory
#     recall — into its ORIGINAL cwd + workspace (read from the stale registry entry,
#     which still holds them). The workspace is re-created by LABEL, so the fleet
#     comes back organized as it was. Enable with --respawn or HERDR_RECOVER_RESPAWN=1.
#
# SAFE BY CONSTRUCTION.
#   - tmux backend  → no-op (the pane-died hook already covers it).
#   - Respawn is OPT-IN and SCOPED: by default only ledger-tracked `live` agents (the
#     orchestrator/Conductor fleet) are revived; HERDR_RECOVER_ALL=1 revives every
#     non-excluded casualty (the unattended-box "bring the whole fleet back").
#   - NEVER respawns a ledger-`dormant` agent (the reaper closed it on purpose), the
#     command post (name overseer/orchestrator or @orchestrator), or an excluded
#     name/pane (~/.tmux/overseer-exclude / $REAP_EXCLUDE — shared with the reaper).
#   - HERDR_RECOVER_DRY_RUN=1 (or --dry-run) logs decisions, changes nothing.
#   - Idempotent + fail-open: a still-alive agent is skipped; a respawn re-registers a
#     fresh entry and prunes its stale one, so the next run finds nothing to do.
#   Slack-independent (writes files, never touches the bridge) — like the reaper.
#
# Usage: herdr-recover.sh [--respawn|--all|--reconcile-only] [--dry-run]
#   (no flags) reconcile + print a recovery plan (what was lost, how to bring it back)
#   --respawn  also respawn ledger-tracked live casualties
#   --all      respawn EVERY non-excluded casualty (implies --respawn)
set -uo pipefail

BACKEND="${NEXUS_SUBSTRATE:-herdr}"
DRY_RUN="${HERDR_RECOVER_DRY_RUN:-0}"
RESPAWN="${HERDR_RECOVER_RESPAWN:-0}"
RECOVER_ALL="${HERDR_RECOVER_ALL:-0}"
NEXUS_DIR="${AGENTS_NEXUS_DIR:-$HOME/repos/agents-nexus}"
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
REGISTRY_DIR="$NEXUS_TMUX_DIR/registry"
EXCLUDE_FILE="$NEXUS_TMUX_DIR/overseer-exclude"
LOG="$NEXUS_TMUX_DIR/herdr-recover.log"
SUBSTRATE="$NEXUS_TMUX_DIR/substrate.sh"
OPEN_CLAUDE="$NEXUS_TMUX_DIR/open-claude.sh"
LEDGER="$NEXUS_DIR/scripts/agent-ledger.py"
RECOVER_SEED_DEFAULT="Your previous session was interrupted by a herdr server restart. The checkpoint and memory context above is your last known state — review it and resume where you left off. If the task was already complete, summarize the final state and wait for instructions."
RECOVER_SEED="${HERDR_RECOVER_SEED:-$RECOVER_SEED_DEFAULT}"

# Flags override env.
while [ $# -gt 0 ]; do
  case "$1" in
    --respawn)        RESPAWN=1 ;;
    --all)            RECOVER_ALL=1; RESPAWN=1 ;;
    --reconcile-only) RESPAWN=0; RECOVER_ALL=0 ;;
    --dry-run)        DRY_RUN=1 ;;
    -h|--help)        sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) ;;
  esac
  shift
done

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >> "$LOG" 2>/dev/null || true; }
have_ledger() { [ -x "$LEDGER" ] && command -v python3 >/dev/null 2>&1; }

# This problem is herdr-only; in tmux the pane-died hook already dereg's + cleans up.
[ "$BACKEND" = herdr ] || { log "skip: backend=$BACKEND (herdr-only tool)"; exit 0; }
[ -d "$REGISTRY_DIR" ] || exit 0
# Server up? Nothing to recover onto otherwise — and we must not act on a transient blip.
"$SUBSTRATE" has-session 2>/dev/null || { log "skip: herdr server down — nothing to recover onto"; exit 0; }

# Exclude set (command post + user excludes), shared with the reaper's convention.
EXCLUDES=" overseer orchestrator "
[ -n "${REAP_EXCLUDE:-}" ] && EXCLUDES="$EXCLUDES$(printf '%s' "$REAP_EXCLUDE" | tr ',' ' ' | tr '[:upper:]' '[:lower:]') "
if [ -f "$EXCLUDE_FILE" ]; then
  while IFS= read -r line; do
    line="$(printf '%s' "$line" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    [ -n "$line" ] && case "$line" in \#*) ;; *) EXCLUDES="$EXCLUDES$line " ;; esac
  done < "$EXCLUDE_FILE"
fi
excluded() { case "$EXCLUDES" in *" $(printf '%s' "$1" | tr '[:upper:]' '[:lower:]') "*) return 0 ;; *) return 1 ;; esac; }

# Ledger state for a repo/name key → live|dormant|"" (empty = untracked / no ledger).
# Fleet agents spawn with repo==name (the reaper reaps --name N --repo N), so the name
# is a valid ledger key; the record's .state is what we gate respawn on.
ledger_state() {
  have_ledger || { echo ""; return; }
  python3 "$LEDGER" get --repo "$1" --json 2>/dev/null | sed -n 's/.*"state":"\([a-z]*\)".*/\1/p' | head -1
}

# Single-quote a string safely for embedding in the sh -c command line.
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

now="$(date +%s)"
recovered=0; reconciled=0; planned=0
PLAN=""

for f in "$REGISTRY_DIR"/*; do
  [ -f "$f" ] || continue
  NAME=""; PANE_ID=""; CWD=""; WORKSPACE=""; SUBSTRATE_F=""
  while IFS='=' read -r k v; do
    case "$k" in
      NAME) NAME="$v" ;; PANE_ID) PANE_ID="$v" ;; CWD) CWD="$v" ;;
      WORKSPACE) WORKSPACE="$v" ;; SUBSTRATE) SUBSTRATE_F="$v" ;;
    esac
  done < "$f"
  [ -n "$PANE_ID" ] || continue

  # Only herdr entries — this is the herdr pane-died analog. Prefer the recorded
  # SUBSTRATE= field; fall back to the handle shape (herdr ids are wN:pN) when absent.
  case "${SUBSTRATE_F:-}" in
    herdr) ;;
    "") case "$PANE_ID" in w[A-Za-z0-9]*:p*) ;; *) continue ;; esac ;;
    *) continue ;;
  esac

  # Alive → still running, leave it. (Direct backend check, not a cached read.)
  "$SUBSTRATE" pane-alive "$PANE_ID" 2>/dev/null && continue

  # Dead herdr entry = the agent isn't running. herdr fires no pane-died hook, so we
  # can't lean on that to tell a clean quit from a crash — but the ledger disambiguates
  # for the population we auto-revive: orchestrator/Conductor agents self-deregister on
  # a CLEAN exit (their `finally`) and the reaper deregisters what it kills (below), so a
  # lingering ledger-`live` entry with a dead pane is genuinely a restart/crash victim.
  # Untracked (interactive picker) agents have no such signal → only revived under --all.
  lstate="$(ledger_state "$NAME")"

  # Decide respawn eligibility.
  eligible=0
  if [ "$RESPAWN" = 1 ] && [ -n "$CWD" ] && ! excluded "$NAME" && ! excluded "$PANE_ID" && [ "$lstate" != dormant ]; then
    { [ "$RECOVER_ALL" = 1 ] || [ "$lstate" = live ]; } && eligible=1
  fi

  if [ "$eligible" = 1 ]; then
    log "recover name=$NAME pane=$PANE_ID cwd=$CWD ws=${WORKSPACE:-flat} ledger=${lstate:-untracked}"
    if [ "$DRY_RUN" != 1 ]; then
      wsargs=(); [ -n "$WORKSPACE" ] && wsargs=(--workspace "$WORKSPACE")
      # open-claude re-injects the repo's recent checkpoint notes + memory recall; the
      # seed frames it as a restart-restore. substrate spawn shell-interprets (sh -c),
      # so the env prefix is honored and the quoted seed survives as one value.
      spawncmd="env PROJECT_SLUG=$(sq "$NAME") SEED_PROMPT=$(sq "$RECOVER_SEED") $OPEN_CLAUDE"
      # ${arr[@]+"${arr[@]}"} expands to nothing (not an error) for an empty array —
      # bash 3.2 (macOS) + set -u aborts on a bare "${wsargs[@]}" when WORKSPACE is empty.
      if "$SUBSTRATE" spawn "$NAME" "$CWD" ${wsargs[@]+"${wsargs[@]}"} "$spawncmd" 2>/dev/null; then
        have_ledger && python3 "$LEDGER" restore --repo "$NAME" --name "$NAME" >/dev/null 2>&1 || true
        rm -f "$f"   # prune the stale entry; the respawn re-registered a fresh one
        recovered=$(( recovered + 1 ))
      else
        log "recover FAILED name=$NAME pane=$PANE_ID (leaving stale entry for retry)"
      fi
    fi
    continue
  fi

  # Not respawned — record the casualty in the recovery plan (for the operator) and,
  # when respawn is on, note WHY it was skipped.
  why=""
  if [ "$RESPAWN" = 1 ]; then
    if excluded "$NAME" || excluded "$PANE_ID"; then why=" (excluded)"
    elif [ "$lstate" = dormant ]; then why=" (reaper-dormant — left closed)"
    elif [ -z "$CWD" ]; then why=" (no cwd on record)"
    elif [ "$lstate" != live ]; then why=" (untracked — use --all to revive)"; fi
  fi
  log "casualty name=$NAME pane=$PANE_ID ws=${WORKSPACE:-flat} ledger=${lstate:-untracked}$why"
  PLAN="${PLAN}  ${NAME}  cwd=${CWD:-?}  ws=${WORKSPACE:-flat}${why}"$'\n'
  planned=$(( planned + 1 ))
done

# Reconcile the ledger last: downgrade any `live` entry no longer backed by a registry
# window (respawns re-registered fresh entries and are kept; stale ones become `gone`).
if [ "$DRY_RUN" != 1 ] && have_ledger; then
  python3 "$LEDGER" reconcile --registry-dir "$REGISTRY_DIR" >/dev/null 2>&1 || true
fi

if [ "$recovered" -gt 0 ]; then log "recovered $recovered agent(s)"; fi
if [ "$recovered" -eq 0 ] && [ "$planned" -eq 0 ]; then
  log "nothing to recover — all herdr agents alive (or cleanly gone)"
fi

# Operator-facing summary (stdout → the interactive run or the unit log).
if [ "$planned" -gt 0 ]; then
  if [ "$RESPAWN" = 1 ]; then
    printf 'herdr-recover: respawned %d, %d casualt(y/ies) left closed:\n%s' "$recovered" "$planned" "$PLAN"
  else
    printf 'herdr-recover: %d agent(s) lost to a restart (reconciled the rosters). Bring them back with: nexus-recover --respawn\n%s' "$planned" "$PLAN"
  fi
elif [ "$recovered" -gt 0 ]; then
  printf 'herdr-recover: respawned %d agent(s) from their last checkpoint.\n' "$recovered"
fi
exit 0
