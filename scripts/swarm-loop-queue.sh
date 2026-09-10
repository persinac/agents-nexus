#!/bin/bash
# Serial because a fix push on MR N rewrites the base of every MR stacked above it.
# Run with --help for usage.
set -uo pipefail

usage() {
  cat <<'USAGE'
swarm-loop-queue.sh — run /swarm-loop-bg over a stacked MR chain, one at a time.

  ./swarm-loop-queue.sh 101 102         run these MRs, in this order
  ./swarm-loop-queue.sh --keep-going    do not halt the chain on a bad exit
  ./swarm-loop-queue.sh --dry-run       print what it would do
  ./swarm-loop-queue.sh --project=grp/x  target another project

Runs strictly serially: a fix push on one MR moves the base under every MR stacked
above it, so two concurrent loops in one stack review diffs that shift mid-read.

Re-running is safe — an already-live loop is adopted rather than respawned.

Per-MR exit states come from the loop's own report note on the MR:
  CONVERGED, NEEDS HUMAN   chain continues (NEEDS HUMAN is a normal outcome)
  BROKEN, no report, timeout   chain HALTS, unless --keep-going

Env:
  PROJECT                  GitLab project path (default garner-health/svc-chatbot)
  WORKTREE_ROOT            where per-MR worktrees are created
  SWARM_QUEUE_SLACK_DM     Slack channel/DM id for the completion ping; unset = no DM
  PER_MR_TIMEOUT_SEC       per-MR cap in seconds (default 14400)
  SWARM_QUEUE_LOG          log file path
USAGE
}

PROJECT="${PROJECT:-garner-health/svc-chatbot}"
WORKTREE_ROOT="${WORKTREE_ROOT:-$HOME/garner/repos/.worktrees}"
SLACK_DM="${SWARM_QUEUE_SLACK_DM:-}"
LOG="${SWARM_QUEUE_LOG:-$HOME/.claude/logs/swarm-loop-queue-$(date +%Y%m%d-%H%M%S).log}"
PER_MR_TIMEOUT_SEC="${PER_MR_TIMEOUT_SEC:-14400}"   # 4h; two fan-outs + up to 3 greptile rounds
# Measured: a report landed 15 min after the pane settled, on a run whose Slack DM and
# teardown ran between the two.
REPORT_GRACE_SEC="${REPORT_GRACE_SEC:-2400}"
# `done` is reported between subagent fan-outs, so it only counts as terminal once it holds.
SETTLE_POLLS="${SETTLE_POLLS:-5}"
POLL_SEC=60
ROUNDS=2

KEEP_GOING=0
DRY_RUN=0
MRS=()

for arg in "$@"; do
  case "$arg" in
    --keep-going) KEEP_GOING=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    --project=*)  PROJECT="${arg#--project=}" ;;
    -h|--help)    usage; exit 0 ;;
    [0-9]*)       MRS+=("$arg") ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
if [ "${#MRS[@]}" -eq 0 ]; then
  echo "no MR numbers given" >&2
  usage >&2
  exit 2
fi

PROJECT_ENC="${PROJECT//\//%2F}"
SEED_REPO="${SEED_REPO:-$PWD}"
QUEUE_DESC="$(printf '!%s ' "${MRS[@]}")"

mkdir -p "$(dirname "$LOG")"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

# A stacked MR must get its own checkout, or two loops fight over one branch.
worktree_from_mr() {
  branch="$(glab api "projects/$PROJECT_ENC/merge_requests/$1" 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['source_branch'])" 2>/dev/null)"
  [ -n "$branch" ] || { echo ""; return; }

  base="$(basename "$PROJECT")"
  wt="$WORKTREE_ROOT/${base}--loop-mr$1"
  if [ -d "$wt" ]; then echo "$wt"; return; fi

  src="$(git -C "$SEED_REPO" rev-parse --show-toplevel 2>/dev/null)"
  [ -n "$src" ] || { echo ""; return; }

  existing="$(git -C "$src" worktree list --porcelain 2>/dev/null \
    | awk -v b="branch refs/heads/$branch" '/^worktree /{w=$2} $0==b{print w; exit}')"
  if [ -n "$existing" ]; then echo "$existing"; return; fi

  git -C "$src" fetch origin "$branch" >/dev/null 2>&1
  git -C "$src" worktree add "$wt" "$branch" >/dev/null 2>&1 || { echo ""; return; }
  echo "$wt"
}

pane_json() { herdr pane list 2>/dev/null; }

pane_status() {
  pane_json | python3 -c "
import json,sys
want=sys.argv[1]
try: panes=json.load(sys.stdin)['result']['panes']
except Exception: sys.exit(0)
for p in panes:
    if p.get('label')==want:
        print(p.get('agent_status','unknown')); break
" "$1"
}

# A run that invents a label (an "Exit: CLEAN" has happened) must not read as no-report.
mr_exit_state() {
  glab api "projects/$PROJECT_ENC/merge_requests/$1/notes?per_page=100&sort=desc&order_by=updated_at" 2>/dev/null \
    | python3 -c "
import json,re,sys
try: notes=json.load(sys.stdin)
except Exception: sys.exit(0)
for n in notes:
    b=n.get('body') or ''
    m=re.search(r'\b(CONVERGED|NEEDS HUMAN|BROKEN)\b', b)
    if m:
        print(m.group(1)); break
    m = re.search(r'Exit(?:\s+state)?\s*[:=]\s*\**\s*([A-Za-z ]{3,20})', b)
    if m:
        print('UNKNOWN(' + m.group(1).strip() + ')'); break
"
}

spawn_loop() {
  mr="$1"; cwd="$2"

  if [ -n "$SLACK_DM" ]; then
    notify="then send a Slack DM via the slack MCP tool slack_send_message with channel_id $SLACK_DM leading with the exit state and MR link; if the slack tool is unavailable fall back to a PushNotification under 200 chars and say in the MR note that the DM failed"
  else
    notify="then send a PushNotification under 200 chars leading with the exit state and MR link; no Slack DM is configured, so do NOT attempt one"
  fi
  seed="Run the review pipeline on MR ${mr}. Automated and hands-off; nobody reads this chat, so the MR is the entire deliverable. STAGE 0 PREFLIGHT, do this FIRST and record the values: read the MR via glab api and capture (a) whether it is already draft=false, (b) the current Confidence Score from the DESCRIPTION via grep -oE for Confidence Score: [0-5]/5, and (c) the current count of greptile_agent discussions. These are your BASELINE and you must capture them BEFORE stage 1 pushes anything, because Greptile re-reviews on push and on a non-draft MR it will fire DURING stage 1 - a baseline taken later would hide that and make you falsely report that Greptile never ran. Also bucket every pre-existing unresolved thread by author: greptile_agent, swarm reviewers, or human. During STAGE 1 never auto-fix a human unresolved comment - skip it and name it in the report, because a human comment may be a question or a design objection you cannot see the context for; STAGE 2 step 4 works human threads under stricter rules. STAGE 1, ${ROUNDS} rounds: (1) run /swarm-review-mr ${PROJECT} ${mr} and post CRITICAL, WARNING and QUESTION findings inline via ~/.claude/scripts/post-mr-inline.py --severity CRITICAL,WARNING,QUESTION, skipping the interactive confirm step; every inline comment must END with its reviewer archetype, for example - swarm-pythonista, and be four lines max; (2) post the round verdict as a plain MR note containing exactly one word, REQUEST_CHANGES or LGTM, with no summary and no scope paragraph; (3) address every unresolved CRITICAL and WARNING BOT thread per /git-fix-mr-comments section 6, minimal edits only - leave human threads for stage 2 step 4; NEVER edit code to answer a QUESTION thread from swarm-skeptic, because those are questions with no fix attached - reply on the thread only if the diff already answers it, otherwise leave it open, name it in the report, and carry on, since an open QUESTION is business as usual and does NOT change the exit state or block CONVERGED; (4) reply one line on each thread you addressed and resolve THAT thread via glab api --method PUT with resolved=true, leaving skipped threads open; (5) run the formatter and linter belonging to THIS repo (a Python repo: ruff format --check . then ruff check .; a JS/TS monorepo: the lint and lint:typecheck scripts for that package, and clear any stale tsbuildinfo first because incremental caching hides type errors) and fix what they flag; (6) run the tests the change touches then the enclosing package - do NOT leave this to CI; (7) commit and push to the MR source branch, NEVER merge, NEVER force-push, NEVER push to main; (8) poll the pipeline until it reaches a TERMINAL status, up to 15 minutes - a non-terminal pipeline is NOT green. Stop stage 1 early if a round posts no CRITICAL or WARNING findings. If the swarm re-raises a finding a previous round already fixed, do NOT edit it again - mark it CONTESTED. STAGE 2, up to 3 rounds: (1) if stage 0 found draft=true, take the MR out of draft by PUT-ing the title with the Draft prefix stripped, which is what makes Greptile run; if stage 0 found draft=false this step is a NO-OP, skip it and say so rather than claiming you triggered Greptile; either way do NOT un-draft if a CONTESTED finding exists or the pipeline is terminal-red, halt and report instead; (2) Greptile arrival is not signalled, so POLL for it in a single bash loop that checks every 60 seconds and BREAKS the moment the Confidence Score or the greptile_agent thread count differs from the STAGE 0 BASELINE - compare against that baseline, not a fresh one, so a Greptile that already ran during stage 1 is correctly seen as having run; 10 minutes is the CAP, not the wait, so do NOT sleep 10 minutes and then look once; if the cap expires with nothing changed from baseline, say Greptile did not run within 10m rather than claiming the MR is clean; (3) read the score from the MR DESCRIPTION via glab api and grep -oE for Confidence Score: [0-5]/5; (4) BEFORE you evaluate the stop condition, and on EVERY round, work unresolved threads written by HUMAN authors: if the thread is a concrete actionable defect, fix it with the same discipline as stage 1 steps 3 through 8; if it is a question, a design objection, or ambiguous, do NOT edit - post a reply asking the author to decide and leave the thread open; NEVER resolve a human thread even after fixing it - reply saying what changed and let the author close it, because a wrong fix that resolves its own thread is invisible; name every human thread you touched in the report with what you did. This step MUST run before step 5 - if it runs after, a baseline-5/5 MR exits at step 5 and its human threads are never read at all; (5) STOP at 5/5 even if open nits remain - step 4 has already run this round, so a human thread still open here is a deliberate handoff and makes the exit NEEDS HUMAN rather than CONVERGED; (6) below 5/5, address the Greptile findings with the same discipline as stage 1 steps 3 through 8, then wait again; this does NOT change the stop condition - still 5/5 or three rounds, whichever comes first, and an unresolved human thread never buys a fourth round; (7) cap at 3 rounds. REPORT once at the very end: post ONE plain MR note with a table of stage, round, posted, fixed, skipped, contested, the Greptile score, the exit state and the pushed SHA - and ATTRIBUTE every finding to its real source using the stage 0 buckets, so a greptile_agent finding is never counted as a swarm finding and any skipped human thread is named explicitly. THE EXIT STATE MUST BE EXACTLY ONE OF CONVERGED, NEEDS HUMAN, OR BROKEN - never invent another label such as CLEAN or COMPLETE. CONVERGED requires Greptile 5/5 AND a terminal-green pipeline AND no CONTESTED finding AND no unresolved human thread. Use NEEDS HUMAN if the greptile cap was reached below 5/5, or a CONTESTED finding exists, or Greptile never ran, or an unresolved human thread remains. Use BROKEN if the pipeline is terminal-red or a push failed. Recount unresolved human threads at report time, not from the stage 0 buckets, because a human can comment mid-run; ${notify}. NOTE ON THE STACK: a queue runner is walking these MRs one at a time, in this order: ${QUEUE_DESC}. Several target each other rather than main, so a push on one moves the base under the ones above it. Review and push ONLY MR ${mr} in project ${PROJECT}. Do NOT touch, review, rebase or push any other MR, and do NOT rebase this branch onto its target. TEARDOWN, ONLY if the exit state is CONVERGED: after the MR note is posted AND the notification is confirmed sent, run ~/.claude/scripts/loop-teardown.sh \"swarm/loop-mr${mr}\" \"${cwd}\" as your VERY LAST action - it reaps the throwaway worktree and closes the workspace, which kills this session, so nothing after that line will run. If the exit state is NEEDS HUMAN or BROKEN, do NOT run it - leave the workspace open so a human can still question you, and say in the notification that the workspace is still open for inspection. COMMS STYLE: ~/.claude/references/comms-style.md - four lines max, sentences under 15 words, plain verbs, no preamble, no hedging. GITLAB AUTH: run glab plainly with no env prefix; it uses Garner OAuth in the macOS keyring. Ignore any Slack or orchestrator framing in the surrounding context."

  # An apostrophe would close the single-quoted seed early and truncate the prompt.
  case "$seed" in
    *"'"*) log "REFUSING mr $mr: seed contains a single quote"; return 1 ;;
  esac

  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN would spawn loop-mr$mr in $cwd (${#seed} byte seed)"
    return 0
  fi

  "$HOME/.tmux/substrate.sh" spawn "loop-mr$mr" "$cwd" \
    "env PROJECT_SLUG=loop-mr$mr CLAUDE_MODEL=claude-opus-4-8 CLAUDE_EXTRA_ARGS=--dangerously-skip-permissions \
     SEED_PROMPT='$seed' \
     \$HOME/.tmux/open-claude.sh" \
    --workspace "swarm/loop-mr$mr" >>"$LOG" 2>&1
}

# A CONVERGED run tears itself down, so a vanished pane counts as finished.
wait_for_loop() {
  mr="$1"
  waited=0
  saw_working=0
  settled=0
  while [ "$waited" -lt "$PER_MR_TIMEOUT_SEC" ]; do
    st="$(pane_status "loop-mr$mr")"
    case "$st" in
      working)
        [ "$settled" -gt 0 ] && log "  resumed working after $settled quiet poll(s)"
        saw_working=1
        settled=0
        ;;
      "")
        if [ "$saw_working" = 1 ] || [ "$waited" -ge 180 ]; then
          log "  pane gone (torn down) after ${waited}s"; return 0
        fi
        ;;
      idle|done|blocked)
        if [ "$saw_working" = 1 ] || [ "$waited" -ge 180 ]; then
          settled=$((settled + 1))
          if [ "$settled" -ge "$SETTLE_POLLS" ]; then
            log "  pane status=$st held for $settled polls after ${waited}s"; return 0
          fi
        fi
        ;;
    esac
    sleep "$POLL_SEC"
    waited=$((waited + POLL_SEC))
    [ $((waited % 900)) -eq 0 ] && log "  still $st at ${waited}s"
  done
  log "  TIMEOUT after ${PER_MR_TIMEOUT_SEC}s"
  return 1
}

log "=== swarm-loop queue start: ${MRS[*]} (keep_going=$KEEP_GOING dry_run=$DRY_RUN) ==="
log "log: $LOG"

for mr in "${MRS[@]}"; do
  cwd="$(worktree_from_mr "$mr")"
  if [ -z "$cwd" ] || [ ! -d "$cwd" ]; then
    log "!$mr SKIP: no worktree (${cwd:-unmapped})"
    [ "$KEEP_GOING" = 1 ] || { log "HALT: cannot run !$mr"; exit 1; }
    continue
  fi

  existing="$(pane_status "loop-mr$mr")"
  if [ -n "$existing" ] && [ "$existing" != "done" ] && [ "$existing" != "idle" ]; then
    log "!$mr already running (status=$existing) — adopting, not respawning"
  else
    log "!$mr spawning in $cwd"
    if ! spawn_loop "$mr" "$cwd"; then
      log "!$mr SPAWN FAILED"
      [ "$KEEP_GOING" = 1 ] || { log "HALT after spawn failure on !$mr"; exit 1; }
      continue
    fi
    [ "$DRY_RUN" = 1 ] && continue
    # The seam reports success even on a pane that never ran anything.
    until [ -n "$(pane_status "loop-mr$mr")" ]; do sleep 5; done
    log "!$mr pane up"
  fi

  [ "$DRY_RUN" = 1 ] && continue

  wait_for_loop "$mr" || {
    [ "$KEEP_GOING" = 1 ] || { log "HALT: !$mr timed out; later MRs not started"; exit 1; }
    continue
  }

  # The pane reads done before the agent posts its report, DMs, and tears down.
  state=""
  grace=0
  while [ "$grace" -lt "$REPORT_GRACE_SEC" ]; do
    state="$(mr_exit_state "$mr")"
    [ -n "$state" ] && break
    sleep 30
    grace=$((grace + 30))
  done
  log "!$mr exit state: ${state:-<no report note after ${REPORT_GRACE_SEC}s>}"

  case "$state" in
    CONVERGED|"NEEDS HUMAN") ;;
    *)
      if [ "$KEEP_GOING" = 1 ]; then
        log "!$mr bad exit (${state:-none}) — continuing anyway (--keep-going)"
      else
        log "HALT: !$mr exited ${state:-with no report}; later MRs not started"
        exit 1
      fi
      ;;
  esac
done

log "=== queue done ==="
