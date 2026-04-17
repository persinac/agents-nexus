#!/usr/bin/env bash
echo "$(date +%s) $*" >> "$HOME/.tmux/apm.log"
