#!/usr/bin/env bash
# overseer-reap.sh — close agents idle past a threshold, checkpointing first.
#
# Scans the tmux agent registry (~/.tmux/registry/*). For each agent that is
# sitting at a finished (@waiting=2) or waiting-on-input (@waiting=1) state AND
# has been idle longer than REAP_IDLE_SECS (default 4h), it runs a final memory
# checkpoint over the agent's newest transcript, then kills the window — the
# pane-died hooks (agent-deregister + worktree-cleanup) handle the rest.
#
# Designed as a scheduled job (launchd plist / systemd timer), every ~15m. It is
# Slack-independent by design (Slack is opt-in; the reaper must work without it).
# Idempotent and fail-open.
#
# Safety:
#   - NEVER reaps a window tagged `@keep 1` — even under REAP_ALL=1. Pin your
#       active working set so an AFK reap can't sweep it (scripts/agent-keep.sh,
#       or: tmux set-option -w @keep 1). The always-honored sibling of the
#       @orchestrator tag below, which REAP_ALL drops.
#   - NEVER reaps a window tagged `@orchestrator 1` (mark your main session:
#       tmux set-option -w @orchestrator 1) — UNLESS REAP_ALL=1.
#   - NEVER reaps a name/pane listed in $REAP_EXCLUDE (csv) or
#       ~/.tmux/overseer-exclude (one name or %pane per line).
#   - Skips actively-working agents (@waiting=0) and dead panes.
#   - REAP_DRY_RUN=1 logs decisions without closing anything.
#   - REAP_ALL=1 prunes EVERYTHING idle incl. the command post (drops the name +
#     @orchestrator exemptions; keeps the exclude list + attached-window guard).
#     For unattended "leave it for days" boxes — the Linux systemd unit sets it.
set -uo pipefail

IDLE_SECS="${REAP_IDLE_SECS:-14400}"          # 4 hours
SESSION="${TMUX_SESSION:-agents}"
DRY_RUN="${REAP_DRY_RUN:-0}"
# REAP_ALL=1 — prune EVERYTHING idle, command post included: drops the automatic
# orchestrator exemptions (the overseer/orchestrator name skip + the @orchestrator
# tag). Still honors ~/.tmux/overseer-exclude/$REAP_EXCLUDE and still won't yank a
# window an attached client is actively viewing. For "leave it for days" boxes.
REAP_ALL="${REAP_ALL:-0}"
NEXUS_DIR="${AGENTS_NEXUS_DIR:-$HOME/garner/repos/agents-nexus}"
REGISTRY_DIR="$HOME/.tmux/registry"
EXCLUDE_FILE="$HOME/.tmux/overseer-exclude"
LOG="$HOME/.tmux/overseer-reap.log"
APM_LOG="$HOME/.tmux/apm.log"
CHECKPOINT="$NEXUS_DIR/scripts/checkpoint-transcript.sh"
LEDGER="$NEXUS_DIR/scripts/agent-ledger.py"        # durable agent ledger (best-effort)

command -v tmux >/dev/null 2>&1 || exit 0
[ -d "$REGISTRY_DIR" ] || exit 0
tmux has-session -t "$SESSION" 2>/dev/null || exit 0

now="$(date +%s)"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >> "$LOG" 2>/dev/null || true; }

# Safety net: never reap the window an attached client is currently viewing
# (you're probably sitting in it). The robust protection for detached / away
# sessions is the @orchestrator tag / exclude file — this only covers "attached".
ATTACHED_ACTIVE=""
if tmux list-clients -t "$SESSION" 2>/dev/null | grep -q .; then
  ATTACHED_ACTIVE="$(tmux display-message -t "$SESSION" -p '#{window_id}' 2>/dev/null)"
fi

# Optional command timeout for the (foreground) checkpoint so a hung curator
# can't stall the sweep. macOS lacks `timeout`; fall back to gtimeout or none.
TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN="timeout 180"
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout 180"; fi

# Build the exclude set (lowercased names + raw pane ids). The command-post
# windows (`overseer`/`orchestrator`) are protected by name — unless REAP_ALL=1,
# which prunes them too.
EXCLUDES=" "
[ "$REAP_ALL" != "1" ] && EXCLUDES=" overseer orchestrator "
[ -n "${REAP_EXCLUDE:-}" ] && EXCLUDES="$EXCLUDES$(printf '%s' "$REAP_EXCLUDE" | tr ',' ' ' | tr '[:upper:]' '[:lower:]') "
if [ -f "$EXCLUDE_FILE" ]; then
  while IFS= read -r line; do
    line="$(printf '%s' "$line" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    [ -n "$line" ] && case "$line" in \#*) ;; *) EXCLUDES="$EXCLUDES$line " ;; esac
  done < "$EXCLUDE_FILE"
fi
excluded() { case "$EXCLUDES" in *" $(printf '%s' "$1" | tr '[:upper:]' '[:lower:]') "*) return 0 ;; *) return 1 ;; esac; }

# Newest Claude Code transcript (.jsonl) for a working directory, or empty.
# Claude encodes the project dir as the abs path with non-alnum chars -> '-'.
latest_transcript() {
  local cwd="$1" enc proj
  [ -n "$cwd" ] || return 0
  enc="$(printf '%s' "$cwd" | sed 's/[^a-zA-Z0-9]/-/g')"
  proj="$HOME/.claude/projects/$enc"
  [ -d "$proj" ] || return 0
  ls -t "$proj"/*.jsonl 2>/dev/null | head -1
}

reaped=0
for f in "$REGISTRY_DIR"/*; do
  [ -f "$f" ] || continue
  NAME=""; PANE_ID=""; CWD=""; AT=""
  while IFS='=' read -r k v; do
    case "$k" in NAME) NAME="$v" ;; PANE_ID) PANE_ID="$v" ;; CWD) CWD="$v" ;; AT) AT="$v" ;; esac
  done < "$f"
  [ -n "$PANE_ID" ] || continue

  # Dead pane? Leave it — agent-deregister cleans the registry on pane-died.
  tmux display-message -t "$PANE_ID" -p '#{pane_id}' >/dev/null 2>&1 || continue

  # Hard exclusions: @keep pin (ALWAYS), orchestrator tag (unless REAP_ALL),
  # attached-and-viewed window, or an explicitly excluded name/pane.
  # @keep is the always-honored sibling of @orchestrator: REAP_ALL drops the
  # orchestrator exemption but NEVER the keep pin, so a user-pinned working-set
  # window survives an unattended REAP_ALL sweep (the AFK-reap problem).
  if [ "$(tmux show-options -wqv -t "$PANE_ID" @keep 2>/dev/null)" = "1" ]; then continue; fi
  if [ "$REAP_ALL" != "1" ] && [ "$(tmux show-options -wqv -t "$PANE_ID" @orchestrator 2>/dev/null)" = "1" ]; then continue; fi
  if excluded "$NAME" || excluded "$PANE_ID"; then continue; fi
  PANE_WINDOW="$(tmux display-message -t "$PANE_ID" -p '#{window_id}' 2>/dev/null)"
  if [ -n "$ATTACHED_ACTIVE" ] && [ "$PANE_WINDOW" = "$ATTACHED_ACTIVE" ]; then continue; fi

  WAITING="$(tmux show-options -wqv -t "$PANE_ID" @waiting 2>/dev/null)"
  case "$WAITING" in 1|2) ;; *) continue ;; esac     # 0/unset = actively working

  LAST_TOOL="$(tmux show-options -wqv -t "$PANE_ID" @last_tool 2>/dev/null)"
  ref="${LAST_TOOL:-$AT}"
  case "$ref" in ''|*[!0-9]*) continue ;; esac        # need a numeric timestamp
  idle=$(( now - ref ))
  [ "$idle" -ge "$IDLE_SECS" ] || continue

  log "candidate name=$NAME pane=$PANE_ID waiting=$WAITING idle=${idle}s cwd=$CWD"
  if [ "$DRY_RUN" = "1" ]; then continue; fi

  # Final checkpoint over the newest transcript (best-effort, time-bounded).
  TRANSCRIPT="$(latest_transcript "$CWD")"
  if [ -n "$TRANSCRIPT" ] && [ -x "$CHECKPOINT" ]; then
    log "checkpoint name=$NAME transcript=$TRANSCRIPT"
    # shellcheck disable=SC2086
    $TIMEOUT_BIN "$CHECKPOINT" --transcript "$TRANSCRIPT" --cwd "$CWD" --label "reap:$NAME" >/dev/null 2>&1 || \
      log "checkpoint failed/timed out name=$NAME (closing anyway)"
  else
    log "no transcript for name=$NAME cwd=$CWD — closing without checkpoint"
  fi

  # Close the window. kill-window fires pane-died -> deregister + worktree-cleanup.
  WINDOW="${PANE_WINDOW:-$(tmux display-message -t "$PANE_ID" -p '#{window_id}' 2>/dev/null)}"
  if [ -n "$WINDOW" ] && tmux kill-window -t "$WINDOW" 2>/dev/null; then
    log "reaped name=$NAME window=$WINDOW idle=${idle}s"
    printf '%s reap %s\n' "$now" "$PANE_ID" >> "$APM_LOG" 2>/dev/null || true
    # Mark the agent dormant in the durable ledger (best-effort, fail-open). This
    # is a no-op for agents the orchestrator never spawned — the ledger only
    # records a reap when a matching live entry exists. Keeps the reaper
    # Slack-independent: it writes a file, never touches the bridge.
    if [ -x "$LEDGER" ] && command -v python3 >/dev/null 2>&1; then
      python3 "$LEDGER" reap --name "$NAME" --repo "$NAME" \
        --checkpoint "reap:$NAME" --transcript "${TRANSCRIPT:-}" >/dev/null 2>&1 || \
        log "ledger reap-mark failed name=$NAME (non-fatal)"
    fi
    reaped=$(( reaped + 1 ))
  else
    log "kill-window failed name=$NAME pane=$PANE_ID"
  fi
done

[ "$reaped" -gt 0 ] && log "sweep done — reaped $reaped agent(s)"
exit 0
