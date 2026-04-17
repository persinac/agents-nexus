#!/usr/bin/env bash
# Called by tmux pane-died hook. If the dead pane was in a worktree, clean it up.
# Usage: worktree-cleanup.sh <pane_current_path>

PANE_PATH="$1"
REPO_DIR="${REPO_DIR:-/c/projects}"
WT_DIR="$REPO_DIR/.worktrees"

[ -z "$PANE_PATH" ] && exit 0

# Check if the path is inside our worktree directory
case "$PANE_PATH" in
  "$WT_DIR"/*|"$WT_DIR"\\*) ;;
  *) exit 0 ;;
esac

# Extract repo name from worktree dirname (format: repo--branch)
wt_name=$(basename "$PANE_PATH")
repo_name="${wt_name%%--*}"

# Find the main repo — handle nested repos (e.g., org/repo → org_repo)
# The worktree name uses _ for /, so reverse it
repo_path=""
for candidate in "${repo_name//_//}" "$repo_name"; do
  if [ -d "$REPO_DIR/$candidate/.git" ] || [ -f "$REPO_DIR/$candidate/.git" ]; then
    repo_path="$REPO_DIR/$candidate"
    break
  fi
done

[ -z "$repo_path" ] && exit 0

# Remove the worktree (safe — git refuses if uncommitted changes)
if ! git -C "$repo_path" worktree remove "$PANE_PATH" 2>/dev/null; then
  echo "$(date '+%H:%M:%S') worktree-cleanup: could not remove $wt_name (uncommitted changes?)" \
    >> "$HOME/.tmux/hook-debug.log" 2>/dev/null
fi
