#!/usr/bin/env bash
# overseer-reap.sh — close agents idle past a threshold, checkpointing first.
#
# Scans the tmux agent registry (~/.tmux/registry/*). For each agent that is
# sitting at a finished (@waiting=2) or waiting-on-input (@waiting=1) state AND
# has been idle longer than REAP_IDLE_SECS (default 4h), it runs a final memory
# checkpoint over the agent's newest transcript, then kills the window — the
# pane-died hooks (agent-deregister + worktree-cleanup) handle the rest.
#
# Pre-reap self-checkpoint (PREREAP_ENABLED=1, default on): PREREAP_LEAD_SECS
# (default 900 = 15m) BEFORE the reap deadline, the reaper nudges the still-live
# agent to checkpoint its OWN state — richer than the post-hoc transcript scrape,
# because the agent is alive and knows its branch / uncommitted work / open items.
# The nudge is programmatic (`substrate send` into the pane), so a fully idle agent
# checkpoints itself without a human. Only @waiting=2 (finished, at a ready prompt)
# is nudged — @waiting=1 is a permission/elicitation prompt and injecting text there
# would answer the menu with garbage, so those keep the transcript-scrape-at-reap
# path. The nudge wakes the agent, which bumps @last_tool and resets the idle clock;
# to keep the reaper reaping we record the warning in a reaper-owned marker file and
# reap PREREAP_LEAD_SECS later off that DEADLINE, not the (now-reset) idle clock. An
# agent a human genuinely re-engages after the warning is spared (see the loop).
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
#   - NEVER reaps a window with a non-empty `@cohort` — a named multi-agent
#       "design cohort" (scripts/agent-cohort.sh hold <design> <agents…>).
#       Always honored, like @keep, so a coordinated design across several idle
#       agents isn't swept mid-flight. Release the whole group at once with:
#       agent-cohort.sh release <design>.
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
NEXUS_DIR="${AGENTS_NEXUS_DIR:-$HOME/repos/agents-nexus}"
REGISTRY_DIR="$HOME/.tmux/registry"
EXCLUDE_FILE="$HOME/.tmux/overseer-exclude"
LOG="$HOME/.tmux/overseer-reap.log"
APM_LOG="$HOME/.tmux/apm.log"
CHECKPOINT="$NEXUS_DIR/scripts/checkpoint-transcript.sh"
LEDGER="$NEXUS_DIR/scripts/agent-ledger.py"        # durable agent ledger (best-effort)

# ── Pre-reap self-checkpoint knobs ───────────────────────────────────────────
# PREREAP_LEAD_SECS before the hard idle deadline, nudge the live agent to
# checkpoint itself (see the header). Tunable via env; all fail-safe.
PREREAP_ENABLED="${PREREAP_ENABLED:-1}"
PREREAP_LEAD_SECS="${PREREAP_LEAD_SECS:-900}"           # warn 15m before the reap
PREREAP_WAIT_STATES="${PREREAP_WAIT_STATES:-2}"         # only nudge @waiting=2 (finished-idle); 1 = perm/elicitation prompt (unsafe to inject)
PREREAP_ACTIVITY_SLOP="${PREREAP_ACTIVITY_SLOP:-300}"   # @last_tool advancing >this past the nudge = a human came back → spare the agent
PREREAP_DIR="$HOME/.tmux/overseer-prereap"              # reaper-owned marker files (NOT a substrate opt — the herdr @-opt read lags through the substrated cache)
# The instruction injected into the pane. One line (send-keys sends Enter on any newline). Tunable.
PREREAP_MSG="${PREREAP_CHECKPOINT_MSG:-You are about to be closed for being idle (~15 min warning). Checkpoint your current state to durable memory NOW, concisely: branch, uncommitted changes, key decisions and their rationale, and open items — enough for a fresh session to resume cleanly. Do this immediately without asking, then stop; do not start any other work.}"
WARN_SECS=$(( IDLE_SECS - PREREAP_LEAD_SECS ))
# Degenerate window (lead >= threshold) would nudge on first idle — disable instead.
if [ "$WARN_SECS" -lt 1 ]; then PREREAP_ENABLED=0; fi

[ -d "$REGISTRY_DIR" ] || exit 0
# Fleet substrate up? Route through the seam. In herdr mode there is no tmux 'agents'
# session, so the old `tmux has-session` guard exited here and the reaper never ran on a
# pure-herdr / Linux box; has-session is backend-aware (herdr: `herdr status server`).
"$HOME/.tmux/substrate.sh" has-session 2>/dev/null || exit 0

now="$(date +%s)"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >> "$LOG" 2>/dev/null || true; }

# Safety net: never reap the window an attached client is currently viewing
# (you're probably sitting in it). The robust protection for detached / away
# sessions is the @orchestrator tag / exclude file — this only covers "attached".
# This is a tmux-only concept (headless herdr has no attached-active window), so
# derive it only in tmux mode — matches the guarded use in the loop below.
ATTACHED_ACTIVE=""
if [ "${NEXUS_SUBSTRATE:-herdr}" != "herdr" ] && tmux list-clients -t "$SESSION" 2>/dev/null | grep -q .; then
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

# ── Pre-reap marker: reaper-owned, backend-agnostic. One file per pane holding
# "<nudge_epoch> <idle_ref_at_nudge>". NOT a substrate @-option: the herdr opt read
# routes through the substrated daemon cache, which lags a set/re-read within a sweep,
# so a freshly-set marker would read back empty and we'd re-nudge every run.
mkdir -p "$PREREAP_DIR" 2>/dev/null || true
_prereap_file() { printf '%s/%s' "$PREREAP_DIR" "$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '_')"; }
prereap_read()  { local f; f="$(_prereap_file "$1")"; [ -f "$f" ] && cat "$f" 2>/dev/null || true; }
prereap_set()   { printf '%s %s\n' "$2" "$3" > "$(_prereap_file "$1")" 2>/dev/null || true; }
prereap_clear() { rm -f "$(_prereap_file "$1")" 2>/dev/null || true; }

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

  # Dead pane? Leave it — agent-deregister cleans the registry on pane-died. Route through
  # the seam: `tmux display-message` on a herdr handle is unreliable (can spuriously pass),
  # so use the backend-aware pane-alive (the same check the destructive gate uses below).
  # Drop any stale pre-reap marker so the file doesn't leak after the pane is gone.
  "$HOME/.tmux/substrate.sh" pane-alive "$PANE_ID" 2>/dev/null || { prereap_clear "$PANE_ID"; continue; }

  # Hard exclusions: @keep pin (ALWAYS), orchestrator tag (unless REAP_ALL),
  # attached-and-viewed window, or an explicitly excluded name/pane.
  # @keep is the always-honored sibling of @orchestrator: REAP_ALL drops the
  # orchestrator exemption but NEVER the keep pin, so a user-pinned working-set
  # window survives an unattended REAP_ALL sweep (the AFK-reap problem).
  if [ "$("$HOME/.tmux/substrate.sh" pane-opt "$PANE_ID" @keep 2>/dev/null)" = "1" ]; then continue; fi
  # @cohort: a named design group (scripts/agent-cohort.sh). Non-empty = protected
  # like @keep — ALWAYS, even under REAP_ALL=1 — so a multi-agent design isn't
  # swept mid-flight. Logged (not reaped) once idle past COHORT_WARN_SECS so a
  # forgotten 'hold' surfaces in the log instead of becoming immortal.
  COHORT="$("$HOME/.tmux/substrate.sh" pane-opt "$PANE_ID" @cohort 2>/dev/null)"
  if [ -n "$COHORT" ]; then
    ct="$("$HOME/.tmux/substrate.sh" pane-opt "$PANE_ID" @last_tool 2>/dev/null)"; ct="${ct:-$AT}"
    case "$ct" in ''|*[!0-9]*) ct="$now" ;; esac
    [ "$(( now - ct ))" -ge "${COHORT_WARN_SECS:-86400}" ] && \
      log "cohort-held(stale) name=$NAME cohort=$COHORT idle=$(( now - ct ))s — release: scripts/agent-cohort.sh release $COHORT"
    continue
  fi
  if [ "$REAP_ALL" != "1" ] && [ "$("$HOME/.tmux/substrate.sh" pane-opt "$PANE_ID" @orchestrator 2>/dev/null)" = "1" ]; then continue; fi
  if excluded "$NAME" || excluded "$PANE_ID"; then continue; fi
  # "Skip the currently-attached active window" is a tmux-client concept (PANE_WINDOW =
  # tmux window_id). Headless herdr has no attached-active window, so only derive it in
  # tmux mode — the tmux call would just return empty on a herdr handle anyway.
  PANE_WINDOW=""
  if [ "${NEXUS_SUBSTRATE:-herdr}" != "herdr" ]; then
    PANE_WINDOW="$(tmux display-message -t "$PANE_ID" -p '#{window_id}' 2>/dev/null)"
    if [ -n "$ATTACHED_ACTIVE" ] && [ "$PANE_WINDOW" = "$ATTACHED_ACTIVE" ]; then continue; fi
  fi

  WAITING="$("$HOME/.tmux/substrate.sh" pane-opt "$PANE_ID" @waiting 2>/dev/null)"
  case "$WAITING" in
    0) prereap_clear "$PANE_ID"; continue ;;   # actively working → came back to life since any warning; drop the marker
    1|2) ;;                                     # 1 = perm/elicitation prompt, 2 = finished-idle
    *) continue ;;                              # unset/unknown → skip this sweep, keep any marker
  esac

  LAST_TOOL="$("$HOME/.tmux/substrate.sh" pane-opt "$PANE_ID" @last_tool 2>/dev/null)"
  ref="${LAST_TOOL:-$AT}"
  case "$ref" in ''|*[!0-9]*) continue ;; esac        # need a numeric timestamp
  idle=$(( now - ref ))

  # Pre-reap marker (reaper-owned file): "<nudge_epoch> <idle_ref_at_nudge>", set when we
  # warned this agent. The warning nudge wakes the agent to checkpoint, which bumps
  # @last_tool → idle looks small again; so past the warning it's this marker's DEADLINE,
  # not the (reset) idle clock, that drives the reap.
  PR="$(prereap_read "$PANE_ID")"; PR_AT="${PR%% *}"; PR_REF="${PR##* }"
  case "$PR_AT" in ''|*[!0-9]*) PR_AT="" ;; esac

  reason=""
  if [ "$idle" -ge "$IDLE_SECS" ]; then
    reason="idle>=${IDLE_SECS}s"
  elif [ -n "$PR_AT" ] && [ "$(( now - PR_AT ))" -ge "$PREREAP_LEAD_SECS" ]; then
    # Warned >= LEAD ago. Rule out genuine re-engagement first: if @last_tool advanced
    # well past the checkpoint we triggered (> nudge + SLOP), a human came back after the
    # warning → spare it and drop the marker (it re-earns a full idle lease).
    if [ -n "$PR_REF" ] && [ "$PR_REF" != "$ref" ] && [ "$ref" -gt "$(( PR_AT + PREREAP_ACTIVITY_SLOP ))" ]; then
      log "prereap-cancel name=$NAME pane=$PANE_ID — activity@${ref} after nudge@${PR_AT} (>+${PREREAP_ACTIVITY_SLOP}s); clearing marker"
      prereap_clear "$PANE_ID"; continue
    fi
    reason="prereap-deadline(${PREREAP_LEAD_SECS}s since warn)"
  fi

  if [ -z "$reason" ]; then
    # Below the reap threshold. In the warning window and not yet warned? Kick off a
    # self-checkpoint in the live agent, then let the deadline above reap it ~LEAD later.
    # Only @waiting states in PREREAP_WAIT_STATES are nudged (default: 2 = finished-idle;
    # 1 is a permission/elicitation prompt where injected text would answer the menu).
    if [ "$PREREAP_ENABLED" = "1" ] && [ -z "$PR_AT" ] && [ "$idle" -ge "$WARN_SECS" ] \
       && case " $PREREAP_WAIT_STATES " in *" $WAITING "*) true ;; *) false ;; esac; then
      # Direct liveness re-verify before poking the pane (cached candidacy; direct gate on action).
      if ! "$HOME/.tmux/substrate.sh" pane-alive "$PANE_ID" 2>/dev/null; then
        prereap_clear "$PANE_ID"; continue
      fi
      log "prereap-warn name=$NAME pane=$PANE_ID waiting=$WAITING idle=${idle}s — checkpoint nudge, reap in ~${PREREAP_LEAD_SECS}s"
      if [ "$DRY_RUN" != "1" ]; then
        prereap_set "$PANE_ID" "$now" "$ref"
        "$HOME/.tmux/substrate.sh" send "$PANE_ID" "$PREREAP_MSG" 2>/dev/null \
          || log "prereap-warn send failed name=$NAME pane=$PANE_ID (will still reap at deadline)"
      fi
    fi
    continue
  fi

  # Re-verify liveness with a DIRECT substrate call before acting. The @waiting read
  # above comes from the daemon's cache, which can briefly lag a herdr-server restart or
  # an agent exit; pane-alive hits the backend directly (herdr pane get / tmux
  # display-message). Never checkpoint or kill a phantom (stale registry/cache entry).
  if ! "$HOME/.tmux/substrate.sh" pane-alive "$PANE_ID" 2>/dev/null; then
    log "vanished name=$NAME pane=$PANE_ID — skipping (stale read)"
    prereap_clear "$PANE_ID"; continue
  fi

  log "candidate name=$NAME pane=$PANE_ID waiting=$WAITING idle=${idle}s reason=$reason cwd=$CWD"
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

  # Close the agent. kill fires pane-died -> deregister + worktree-cleanup. Kill target:
  # the tmux window_id in tmux mode (kills the whole agent window); the pane handle
  # itself in herdr mode (herdr pane close) — PANE_WINDOW is empty on the herdr path.
  if [ "${NEXUS_SUBSTRATE:-herdr}" = "herdr" ]; then
    WINDOW="$PANE_ID"
  else
    WINDOW="${PANE_WINDOW:-$(tmux display-message -t "$PANE_ID" -p '#{window_id}' 2>/dev/null)}"
  fi
  if [ -n "$WINDOW" ] && "$HOME/.tmux/substrate.sh" kill "$WINDOW" 2>/dev/null; then
    log "reaped name=$NAME window=$WINDOW idle=${idle}s reason=$reason"
    prereap_clear "$PANE_ID"
    # Drop the registry entry now. In tmux the pane-died hook (agent-deregister +
    # worktree-cleanup) does this; herdr fires NO pane-died hook, so a reaped herdr
    # agent would otherwise linger as a stale entry — showing up in `peers` and, worse,
    # looking like a restart casualty to herdr-recover.sh. Explicit + idempotent; the
    # tmux hook still runs (harmless double). worktree-cleanup stays hook-only.
    [ "${NEXUS_SUBSTRATE:-herdr}" = "herdr" ] && "$HOME/.tmux/substrate.sh" deregister "$PANE_ID" 2>/dev/null || true
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
