#!/usr/bin/env bash
# Record pane -> transcript path, so automode-watchdog.py can resolve which
# session belongs to a pane EXACTLY instead of guessing.
#
# Why (2026-08-17): the watchdog originally derived a pane's transcript from its
# cwd alone — slug the cwd, take the newest .jsonl in that project dir. Two panes
# with the SAME cwd (routinely true: several agents cwd'd to $HOME all slug to
# `-Users-<user>`) therefore resolved to one shared transcript. Observed live:
# panes w4J:p2 and w4M:p2 held an identical transcript_path AND offset. Denials
# from one pane were being attributed to both, so the watchdog could cycle the
# permission mode of a pane that was never stuck.
#
# Claude Code hands every hook the real `transcript_path`, so recording it here
# removes the guess. Called from hook-sessionstart (authoritative, at session
# start/resume), hook-pretooluse (backfills a pane already running before this
# existed — and guarantees the entry exists BEFORE any classifier denial can
# land, since a denial is itself a tool call), and hook-stop (cheap refresh).
#
# One file per pane rather than a shared JSON map: concurrent hooks from
# different panes then never race on a single file. Same convention as
# ~/.tmux/registry/<pane> and substrate.sh's herdr sidecar files.
#
# Best-effort by construction: every failure path exits 0 silently and nothing
# is written to stdout. A hook must never fail, and this hook adds no value
# worth risking a turn over.

PANE="${TMUX_PANE:-${HERDR_PANE_ID:-}}"
[ -n "$PANE" ] || exit 0

INPUT=$(cat 2>/dev/null)
# Paths cannot contain a literal `"`, so this is safe without a JSON parser —
# same sed-extraction approach hook-notification.sh uses, and avoids making
# every tool call pay for a jq/python spawn.
TP=$(printf '%s' "$INPUT" | sed -n 's/.*"transcript_path" *: *"\([^"]*\)".*/\1/p' | head -1)
[ -n "$TP" ] || exit 0

DIR="$HOME/.tmux/transcript-map"
mkdir -p "$DIR" 2>/dev/null || exit 0
F="$DIR/${PANE//[:\/]/_}"

# PreToolUse fires on every tool call but this value only changes at session
# start/resume — skip the rewrite when it already matches.
[ "$(cat "$F" 2>/dev/null)" = "$TP" ] && exit 0

printf '%s\n' "$TP" > "$F" 2>/dev/null
exit 0
