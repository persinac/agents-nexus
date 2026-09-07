#!/usr/bin/env bash
# agent-spawn.sh — spawn an agent and PROVE it started.
#
# Why this exists (2026-09-07): `substrate.sh spawn` returned exit 0 for six
# consecutive spawns that produced six dead panes. The herdr pane and the workspace
# bucket were both created, `workspace-list` and `list-panes` showed them, and
# `capture` returned empty — which is indistinguishable from a booting agent. The
# exit code, the pane, and the bucket were all "green" while nothing was running.
#
# Root cause of that particular failure: for commands >= 900 bytes, substrate.sh
# writes a wrapper containing `exec <cmd>`. A command beginning with a bare
# assignment (`SEED_PROMPT='...' prog`) is invalid after `exec` — bash treats the
# assignment as the program name — so the shell died instantly. A real seed prompt
# is 800-1400 bytes, so this lands exactly on the threshold: the same command that
# worked at 110 bytes in testing breaks once a paragraph of seed text is added.
#
# Two guards, in order:
#   1. Refuse a bare-assignment command prefix outright, and say what to use instead.
#   2. After spawning, poll the PROCESS TABLE for a live `claude` whose environment
#      carries the expected NEXUS_WORKSPACE. Never trust the exit code.
#
# Usage:
#   agent-spawn.sh [--partner <fqdn>] [--timeout <s>] <workspace> <cwd> <command...>
#   agent-spawn.sh --timeout 90 notif /path/to/repo "env SEED_PROMPT='…' ~/.tmux/open-claude.sh"
#
# Exit 0 only when a live process is confirmed. Exit 1 with a diagnosis otherwise.
set -uo pipefail

SUBSTRATE="${NEXUS_TMUX_DIR:-$HOME/.tmux}/substrate.sh"
TIMEOUT=75
POLL=3
PARTNER=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --timeout) TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    # --partner <fqdn>: names a review peer. Injected as NEXUS_REVIEW_PARTNER so
    # open-claude.sh can render the pairing block. substrate spawn only forwards a
    # fixed env allowlist, so this has to ride inline on the command line.
    --partner) PARTNER="${2:?--partner needs an agent FQDN}"; shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done

WS="${1:?usage: agent-spawn.sh <workspace> <cwd> <command...>}"
CWD="${2:?usage: agent-spawn.sh <workspace> <cwd> <command...>}"
shift 2
CMD="$*"
[ -n "$CMD" ] || { echo "agent-spawn: no command given" >&2; exit 2; }
[ -d "$CWD" ] || { echo "agent-spawn: cwd does not exist: $CWD" >&2; exit 2; }

# ── Guard 1: bare assignment prefix ────────────────────────────────────────
# `exec FOO=bar prog` is invalid; `exec env FOO=bar prog` is fine. substrate.sh only
# wraps in `exec` past 900 bytes, so this fails ONLY for long commands — which is
# every real seed prompt, and none of the short ones you test with.
if printf '%s' "$CMD" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*='; then
  cat >&2 <<'EOF'
agent-spawn: REFUSED — the command starts with a bare VAR= assignment.

  substrate.sh wraps commands >= 900 bytes as `exec <cmd>`, and
  `exec VAR=value prog` is invalid: bash treats the assignment as the program
  name and the shell dies instantly, with exit 0 reported to the caller.

  This breaks ONLY for long commands, so a short test passes and a real
  seed prompt fails.

  Use env(1) instead:
      env SEED_PROMPT='...' /path/to/open-claude.sh
EOF
  exit 2
fi

# ── Inject the review partner, if named ────────────────────────────────────
if [ -n "$PARTNER" ]; then
  case "$CMD" in
    "env "*) CMD="env NEXUS_REVIEW_PARTNER=$PARTNER ${CMD#env }" ;;
    *)       CMD="env NEXUS_REVIEW_PARTNER=$PARTNER $CMD" ;;
  esac
fi

# ── Spawn ──────────────────────────────────────────────────────────────────
"$SUBSTRATE" spawn "$WS" "$CWD" "$CMD" --workspace "$WS" >/dev/null 2>&1
spawn_rc=$?

# ── Guard 2: prove it, from the process table ──────────────────────────────
# The ONLY trustworthy signal. Registry and NATS presence both lag while claude
# boots, so an early check there reads as failure; the pane and bucket exist even
# when the shell is dead. A process with the right NEXUS_WORKSPACE is proof.
live_pid() {
  local pid ws
  for pid in $(pgrep -u "$USER" -x claude 2>/dev/null); do
    ws=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
         | grep -m1 '^NEXUS_WORKSPACE=' | cut -d= -f2)
    [ "$ws" = "$WS" ] && { printf '%s' "$pid"; return 0; }
  done
  return 1
}

waited=0
while [ "$waited" -lt "$TIMEOUT" ]; do
  if pid=$(live_pid); then
    cwd_actual=$(readlink "/proc/$pid/cwd" 2>/dev/null)
    echo "agent-spawn: OK  ws=$WS  pid=$pid  cwd=$cwd_actual"
    exit 0
  fi
  sleep "$POLL"
  waited=$((waited + POLL))
done

# ── Failed: say which of the green signals lied ────────────────────────────
{
  echo "agent-spawn: FAILED — no live claude process for workspace '$WS' after ${TIMEOUT}s."
  echo "  substrate spawn exit code was: $spawn_rc  (this is why the exit code is not trusted)"
  echo
  echo "  What DOES exist (all of these can be green while nothing runs):"
  printf '    workspace bucket : %s\n' "$("$SUBSTRATE" workspace-list 2>/dev/null | grep -c "	${WS}	" || echo 0)"
  printf '    live panes       : %s\n' "$("$SUBSTRATE" list-panes 2>/dev/null | tr '\n' ' ')"
  echo
  echo "  Check the pane's shell directly — a dead shell here means the command"
  echo "  never ran, not that the agent is slow to boot."
} >&2
exit 1
