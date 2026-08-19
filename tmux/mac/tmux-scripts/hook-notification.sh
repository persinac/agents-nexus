#!/usr/bin/env bash
# Notification hook: fires on permission prompts, questions, etc.
# Sets @waiting=1 (red) when Claude needs user input.

INPUT=$(cat)

# Chain memory event early (works even outside tmux)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$INPUT" | "$SCRIPT_DIR/hook-memory.sh" permission_wait 2>/dev/null

# tmux sets TMUX_PANE; herdr sets HERDR_PANE_ID. Fold herdr's in so the guard, the
# classifier PANE=, the registry lookup, and the substrate calls all work in both.
TMUX_PANE="${TMUX_PANE:-${HERDR_PANE_ID:-}}"
[ -n "$TMUX_PANE" ] || exit 0

NTYPE=$(echo "$INPUT" | sed -n 's/.*"notification_type" *: *"\([^"]*\)".*/\1/p' | head -1)

# DEBUG (2026-08-14, widened 2026-08-18, temporary): log every notification
# unconditionally, before the type filter below, to settle whether the
# too-complex AST-parser gate emits a Notification at all.
#
# Why it matters: a too-complex denial lands in the transcript as
# toolDenialKind=user-rejected — the same value a human answering "no" to an
# ordinary prompt produces — so automode-watchdog.py must never react to it and
# could not tell the two apart if it tried. That leaves the Notification as the
# only signal that could mark a gated pane as needing input. If it fires, the
# rest of this hook already handles the pane (needs-input + classifier + Slack)
# and an unattended agent recovers. If it does not, an unattended agent that
# trips the gate blocks mid-turn with no Stop hook and no needs-input flag —
# indistinguishable from a long-running task, i.e. silently stuck until a human
# happens to look at that pane.
#
# The first cut capped the payload at `cut -c1-300` and so could never answer
# the question: the payload opens with session_id + transcript_path + cwd, and
# this box's long home-dir path appears twice in that prefix (once in cwd, once
# inside transcript_path), pushing every remaining field past the cut. Four days and 245
# logged notifications yielded no field beyond notification_type.
#
# What the widened capture established (2026-08-18), so the next person does not
# re-derive it: the payload is ENTIRELY these seven keys, ~430 bytes —
#   session_id, transcript_path, cwd, prompt_id, hook_event_name, message,
#   notification_type
# Consequences, all confirmed against real captures rather than assumed:
#   - `message` is the constant "Claude needs your permission" for every
#     permission_prompt (7/7 identical). It names no tool, command, or reason.
#   - So the payload alone can NEVER distinguish a too-complex gate from an
#     ordinary permission prompt. Logging more of it does not help; that avenue
#     is closed, not merely unfinished.
#   - `prompt_id` is the id of the USER TURN, not of the permission prompt, despite
#     the name. Every notification raised while working on one user message carries
#     the same value: 43 logged notifications spread over ~13 ids, one id used 13
#     times, and a single id (10c5ad6f) observed across three DIFFERENT commands at
#     15:00, 15:05 and 15:11. So it is a turn correlation key and must NOT be used to
#     dedupe prompts — doing so would collapse every prompt in a turn into one.
#     (An earlier version of this comment claimed it was per-prompt. It is not.)
#     It also appears NOWHERE in the session transcript, so it cannot bridge a
#     notification to the denial it belongs to either.
#
# ANSWER (2026-08-18): the gate DOES emit a `permission_prompt` Notification.
# Therefore this hook runs for a gated pane and the report-state needs-input
# below fires — a pane stuck on the gate is VISIBLE to a supervisor, and the
# "silently stuck, looks like a long-running task" failure feared above does not
# happen. What does NOT happen is auto-approval: notify-classify.py did not clear
# it, so the prompt still reaches a human.
#
# Evidence, from live capture rather than reasoning: the Bash tool_use for
# `for i in 1 2; do echo "loop-$i"; done` was written to the transcript at
# 20:37:57Z, a notification landed for this pane 7s later at 20:38:04Z, and the
# tool_result did not arrive until 20:38:53Z — a 56-second wait on a human
# keypress. auto-approve.log holds entries at 14:18:51 and 14:34:44 local but
# NOT 14:38:04, confirming the hook did not answer that one.
#
# CORRECTION: an earlier version of this comment guessed notify-classify.py had
# failed because the gate fires at PARSE time, before the tool_use its
# _last_tool_use() looks for exists in the transcript. The ordering above
# disproves that outright — the tool_use was already on disk 7 seconds before
# the notification. The two real causes were both inside notify-classify.py and
# are now fixed: it had no concept of shell control flow, so `for`/`do`/`done`
# segments failed on an unrecognized command head and fell through to the LLM;
# and that LLM tier was itself dead, from an inherited ANTHROPIC_API_BASE
# naming a container-only host that cannot resolve here. A read-only loop now
# auto-approves deterministically, with no model call at all.
#
# Also worth knowing, from the same session: a for/case/function_definition/
# command_substitution command is NOT reliably gated on 2.1.234 — most ran clean
# with no prompt at all, including inside a headless
# --dangerously-skip-permissions run (which logged no notification either). The
# trigger set is narrower than `~/.claude/CLAUDE.md` currently claims; do not
# assume a given compound shape will gate.
LOG="$HOME/.tmux/notification-debug.log"
# Bounded: this sits on the hot path of every notification and has already
# outlived its "temporary" label once.
if [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  mv -f "$LOG" "$LOG.1" 2>/dev/null
fi
printf '%s [%s] type=%s prompt=%s raw=%s\n' "$(date '+%F %T')" "$TMUX_PANE" \
  "${NTYPE:-<empty>}" \
  "$(echo "$INPUT" | sed -n 's/.*"prompt_id" *: *"\([^"]*\)".*/\1/p' | head -1)" \
  "$(printf '%s' "$INPUT" | tr -d '\n' | cut -c1-2000)" >> "$LOG" 2>/dev/null

# Only go red for genuine approval/input requests.
# idle_prompt fires when Claude finishes a turn — Stop hook already handles that (→ @waiting=2).
case "$NTYPE" in
  permission_prompt|elicitation_dialog) ;;
  *) exit 0 ;;
esac

NOW=$(date +%s)
WNAME=$("$HOME/.tmux/substrate.sh" pane-field "$TMUX_PANE" '#W' 2>/dev/null)

# Agent name — used by both the classifier and the Slack post.
AGENT_NAME=$(grep '^NAME=' "$HOME/.tmux/registry/$TMUX_PANE" 2>/dev/null | cut -d= -f2)
[ -z "$AGENT_NAME" ] && AGENT_NAME="${WNAME:-agent}"

# Auto-approve gate + middle-man summary. The brain categorizes the pending tool:
#   exit 0  -> read-only: answer "1. Yes" locally, no human, no Slack
#   exit 10 -> needs a human: prints the /notify body ([category] + summary) on stdout
# Fails safe to "modify" (ask) on any error.
CLASSIFY_PY="$HOME/.tmux/.classify-venv/bin/python"
BODY=""
if [ "$NTYPE" = "permission_prompt" ] && [ -x "$CLASSIFY_PY" ]; then
  # Optional external timeout (macOS lacks `timeout`); litellm bounds the API call itself.
  TIMEOUT_BIN=""
  if command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN="timeout 20"
  elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout 20"; fi
  BODY=$(printf '%s' "$INPUT" | AN="$AGENT_NAME" PANE="$TMUX_PANE" KIND="$NTYPE" WAIT_SINCE="$NOW" FB="needs input ($NTYPE)" \
    $TIMEOUT_BIN "$CLASSIFY_PY" "$SCRIPT_DIR/notify-classify.py" 2>/dev/null)
  RC=$?
  if [ "$RC" -eq 0 ]; then
    ( sleep 0.4; "$HOME/.tmux/substrate.sh" send "$TMUX_PANE" 1 2>/dev/null ) &
    "$HOME/.tmux/substrate.sh" report-state "$TMUX_PANE" working "$NOW" 2>/dev/null
    echo "$NOW auto-approve $TMUX_PANE" >> "$HOME/.tmux/auto-approve.log" 2>/dev/null
    exit 0
  fi
fi

# --- needs a human: flag + desktop notify + surface to Slack ---
"$HOME/.tmux/substrate.sh" report-state needs-input "$TMUX_PANE" "$NTYPE" "$NOW" 2>/dev/null

# Desktop notification — OS-guarded so this hook is the ONE shared copy (no per-OS override):
#   macOS  → osascript bubble + sound
#   Linux  → notify-send bubble whenever that binary is installed (the test below is
#            `command -v`, NOT whether a console session is attached — an earlier version of
#            this comment claimed the latter), plus a bell (\a) that iTerm2 / Windows
#            Terminal turn into a system notification for SSH clients.
case "$OSTYPE" in
  darwin*)
    osascript -e "display notification \"Agent ${WNAME:-?} needs input ($NTYPE)\" with title \"Claude Code\" sound name \"Glass\"" 2>/dev/null & ;;
  *)
    command -v notify-send >/dev/null 2>&1 && \
      notify-send "Claude Code" "Agent ${WNAME:-?} needs input ($NTYPE)" 2>/dev/null &
    printf '\a' ;;
esac

# Slack round-trip — backgrounded; curl no-ops if the bridge is down.
(
  # The brain set BODY for a modify decision; otherwise (elicitation, or no classifier
  # venv) fall back to the deterministic payload helper.
  if [ -z "$BODY" ]; then
    BODY=$(printf '%s' "$INPUT" | AN="$AGENT_NAME" PANE="$TMUX_PANE" FB="needs input ($NTYPE)" KIND="$NTYPE" WAIT_SINCE="$NOW" \
      python3 "$SCRIPT_DIR/notify-payload.py" 2>/dev/null)
  fi
  printf '%s' "$BODY" \
    | curl -m 2 -s -o /dev/null -X POST "http://127.0.0.1:${SLACK_BRIDGE_PORT:-8788}/notify" -H 'Content-Type: application/json' --data @- 2>/dev/null
) &

echo "$NOW wait $TMUX_PANE" >> "$HOME/.tmux/apm.log" 2>/dev/null

exit 0
