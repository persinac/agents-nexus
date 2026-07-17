#!/usr/bin/env bash
# Emits the active API key name for the tmux status bar.
# Shows nothing when default key is in use (no visual clutter).
KEYNAME=$(tmux show-environment ANTHROPIC_KEY_NAME 2>/dev/null | sed 's/ANTHROPIC_KEY_NAME=//')
[ -n "$KEYNAME" ] && printf "#[fg=#f38ba8][key:%s] " "$KEYNAME"
