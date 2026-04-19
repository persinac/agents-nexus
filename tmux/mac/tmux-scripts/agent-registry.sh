#!/usr/bin/env bash
# Agent registry — live discovery and communication between Claude Code agents.
#
# Commands:
#   agent-registry.sh peers [--exclude PANE_ID]     List all active agents
#   agent-registry.sh whoami                        Show this agent's slot, name, and directory
#   agent-registry.sh broadcast [--exclude PANE_ID] <msg>  Send a message to all OTHER agents

set -euo pipefail

COMMAND="${1:-help}"
shift 2>/dev/null || true

# Parse --exclude flag (used to exclude self when $TMUX_PANE isn't available)
EXCLUDE_PANE="${TMUX_PANE:-}"
if [ "${1:-}" = "--exclude" ] && [ -n "${2:-}" ]; then
  EXCLUDE_PANE="$2"
  shift 2
fi

REGISTRY_DIR="$HOME/.tmux/registry"
SESSION="${TMUX_AGENT_SESSION:-agents}"

case "$COMMAND" in
  peers)
    mkdir -p "$REGISTRY_DIR"
    count=0
    printf "%-6s %-24s %s\n" "SLOT" "NAME" "DIRECTORY"
    printf "%-6s %-24s %s\n" "----" "----" "---------"
    for f in "$REGISTRY_DIR"/*; do
      [ -f "$f" ] || continue
      pane_id=$(grep '^PANE_ID=' "$f" | cut -d= -f2)
      [ -n "$EXCLUDE_PANE" ] && [ "$pane_id" = "$EXCLUDE_PANE" ] && continue
      name=$(grep '^NAME=' "$f" | cut -d= -f2)
      cwd=$(grep '^CWD=' "$f" | cut -d= -f2)
      slot=$(tmux display-message -t "$pane_id" -p '#{window_index}' 2>/dev/null)
      if [ -z "$slot" ]; then
        rm -f "$f"
        continue
      fi
      printf "%-6s %-24s %s\n" "$slot" "$name" "$cwd"
      count=$((count + 1))
    done
    if [ "$count" -eq 0 ]; then
      echo "No other agents active"
    fi
    ;;

  whoami)
    pane_id="${TMUX_PANE:-}"
    if [ -z "$pane_id" ] && [ -n "$EXCLUDE_PANE" ]; then
      pane_id="$EXCLUDE_PANE"
    fi
    if [ -z "$pane_id" ]; then
      echo "Not running inside tmux"
      exit 1
    fi
    reg_file="$REGISTRY_DIR/$pane_id"
    if [ -f "$reg_file" ]; then
      name=$(grep '^NAME=' "$reg_file" | cut -d= -f2)
      cwd=$(grep '^CWD=' "$reg_file" | cut -d= -f2)
      slot=$(tmux display-message -t "$pane_id" -p '#{window_index}' 2>/dev/null)
      printf "slot: %s\nname: %s\ndirectory: %s\npane: %s\n" "$slot" "$name" "$cwd" "$pane_id"
    else
      echo "Not registered"
      exit 1
    fi
    ;;

  broadcast)
    MSG="$*"
    [ -z "$MSG" ] && { echo "Usage: agent-registry.sh broadcast [--exclude PANE_ID] <message>"; exit 1; }
    mkdir -p "$REGISTRY_DIR"
    sent=0
    for f in "$REGISTRY_DIR"/*; do
      [ -f "$f" ] || continue
      pane_id=$(grep '^PANE_ID=' "$f" | cut -d= -f2)
      [ -n "$EXCLUDE_PANE" ] && [ "$pane_id" = "$EXCLUDE_PANE" ] && continue
      slot=$(tmux display-message -t "$pane_id" -p '#{window_index}' 2>/dev/null)
      [ -z "$slot" ] && { rm -f "$f"; continue; }
      if [[ "$MSG" =~ ^[0-9]$ ]]; then
        tmux send-keys -t "${SESSION}:${slot}" "$MSG"
      else
        tmux send-keys -t "${SESSION}:${slot}" "$MSG" Enter
      fi
      sent=$((sent + 1))
    done
    echo "Broadcast to $sent agent(s)"
    ;;

  help|*)
    echo "Usage: agent-registry.sh <command> [args]"
    echo ""
    echo "Commands:"
    echo "  peers [--exclude PANE_ID]              List all active agents"
    echo "  whoami                                 Show this agent's info"
    echo "  broadcast [--exclude PANE_ID] <msg>    Send a message to all other agents"
    ;;
esac
