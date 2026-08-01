#!/usr/bin/env bash
# auto-approve.sh — watch ONE agent's pane and auto-answer its permission prompts.
#
# A stopgap for running an agent unattended when auto-accept-edits mode / a permission
# rule can't be enabled (e.g. the operator has no shell). Scoped to a single pane, time-
# bounded, and self-stopping. Sends the answer via agent-send.sh — the same path the
# Slack bridge uses to deliver a permission answer to a pane (deliverToPane).
#
# Detection: `@waiting` is not reliably set for every substrate backend (herdr agents can
# read @waiting=0 while sitting at a prompt), so we detect by CAPTURING the pane and
# matching the Claude permission-prompt frame. Dedup: after answering we wait for the
# prompt frame to clear (or its action line to change) before scanning again, so exactly
# one keystroke is sent per prompt — never a stray digit into live work.
#
# Usage: auto-approve.sh <pane> [max_seconds] [digit]
#   pane         substrate pane id (e.g. wQ:pA)
#   max_seconds  total run time before auto-stopping (default 1800)
#   digit        option to send (default 1 = "Yes, once")
#
# NOTE: this approves EVERY prompt it sees (edits AND commands) with the given digit. Only
# arm it for an agent you intend to let run unattended.
set -u

PANE="${1:?usage: auto-approve.sh <pane> [max_seconds] [digit]}"
MAX="${2:-1800}"
DIGIT="${3:-1}"
SUB="$HOME/.tmux/substrate.sh"
SEND="$HOME/.tmux/agent-send.sh"
# Prompt frame signature (case-insensitive): the "Do you want to …" question and the
# highlighted "N. Yes" option Claude Code renders while awaiting a permission decision.
SIG='do you want to|❯ *[0-9]\. *yes|[0-9]\. *yes'

start=$(date +%s)
approved=0
echo "[auto-approve] $PANE — answering prompts with '$DIGIT', cap ${MAX}s, from $(date +%H:%M:%S)"

while :; do
  (( $(date +%s) - start >= MAX )) && { echo "[auto-approve] ${MAX}s cap reached; stop"; break; }

  cap=$("$SUB" capture "$PANE" 16 2>/dev/null) || { echo "[auto-approve] pane gone; stop"; break; }

  if grep -qiE "$SIG" <<<"$cap"; then
    action=$(grep -iE "do you want to" <<<"$cap" | head -1 | sed 's/^ *//;s/ *$//')
    "$SEND" "$PANE" "$DIGIT" >/dev/null 2>&1
    approved=$((approved + 1))
    echo "[auto-approve] #$approved $(date +%H:%M:%S) — ${action:-prompt}"
    # Wait until this prompt is gone (agent working) or a DIFFERENT prompt appears, so we
    # send exactly one keystroke per prompt even if a frame element ticks.
    for _ in $(seq 1 15); do
      sleep 1
      c2=$("$SUB" capture "$PANE" 16 2>/dev/null) || break
      grep -qiE "$SIG" <<<"$c2" || break
      a2=$(grep -iE "do you want to" <<<"$c2" | head -1 | sed 's/^ *//;s/ *$//')
      [ -n "$action" ] && [ "$a2" != "$action" ] && break
    done
  else
    sleep 4
  fi
done

echo "[auto-approve] EXIT $(date +%H:%M:%S): approved $approved prompt(s) over $(( ($(date +%s) - start) / 60 ))m for $PANE"
