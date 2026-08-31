#!/usr/bin/env bash
# Record pane -> the tool call about to ask for permission, for notify-classify.py.
# Payload stored verbatim: parsing it needs a JSON parser and this is the hot path of every
# tool call, so the classifier parses instead. Convention: record-transcript.sh.

umask 077

PANE="${TMUX_PANE:-${HERDR_PANE_ID:-}}"
[ -n "$PANE" ] || exit 0

DIR="$HOME/.tmux/pending"
mkdir -p "$DIR" 2>/dev/null || exit 0
chmod 700 "$DIR" 2>/dev/null
F="$DIR/${PANE//[:\/]/_}.json"

cat > "$F.tmp" 2>/dev/null || exit 0
mv -f "$F.tmp" "$F" 2>/dev/null
exit 0
