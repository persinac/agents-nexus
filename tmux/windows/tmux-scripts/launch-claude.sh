#!/usr/bin/env bash
# Fuzzy repo picker with git worktree support.
# If the selected repo already has an agent, offers to create a worktree.

REPO_DIR="${REPO_DIR:-/c/projects}"
WT_DIR="$REPO_DIR/.worktrees"

# Build combined list: repos + existing worktrees
repos=$(
  find "$REPO_DIR" -maxdepth 4 \( -name '.git' -type d -o -name '.git' -type f \) 2>/dev/null \
    | sed "s|${REPO_DIR}/||; s|/\.git$||" \
    | grep -v '^\.worktrees' \
    | sort
)

worktrees=""
if [ -d "$WT_DIR" ]; then
  worktrees=$(ls -1 "$WT_DIR" 2>/dev/null | sed 's/^/[wt] /')
fi

# Merge into fzf
selected=$(
  { echo "[general]"; echo "$repos"; [ -n "$worktrees" ] && echo "$worktrees"; } \
    | grep -v '^$' \
    | fzf --prompt='repo> ' \
        --border=rounded
)

[ -z "$selected" ] && exit 0

# Handle general session
if [[ "$selected" == "[general]" ]]; then
  tmux new-window -d -n "general" -c "${REPO_DIR}" "env PROJECT_SLUG=general $HOME/.tmux/open-claude.sh"
  exit 0
fi

# Handle worktree selection
if [[ "$selected" == "[wt] "* ]]; then
  wt_name="${selected#\[wt\] }"
  working_dir="$WT_DIR/$wt_name"
  window_name="${wt_name/--//}"  # repo--branch → repo/branch
  tmux new-window -d -n "$window_name" -c "$working_dir" "$HOME/.tmux/open-claude.sh"
  exit 0
fi

# Check if any tmux pane is already in this repo
repo_path="$REPO_DIR/$selected"
conflict=$(tmux list-panes -a -F '#{pane_current_path}' 2>/dev/null \
  | grep -F "$repo_path" | head -1)

if [ -z "$conflict" ]; then
  # No conflict — launch normally
  tmux new-window -d -n "$selected" -c "$repo_path" "$HOME/.tmux/open-claude.sh"
  exit 0
fi

# Conflict detected — offer worktree creation
# Get current branch of the repo for context
current_branch=$(git -C "$repo_path" branch --show-current 2>/dev/null || echo "unknown")

# Prompt for branch name
branch=$(
  echo "" | fzf --prompt="Agent already on '$current_branch'. New branch name: " \
    --print-query --border=rounded --header="Worktree for: $selected" \
    | head -1
)

[ -z "$branch" ] && exit 0

# Create worktree
mkdir -p "$WT_DIR"
wt_path="$WT_DIR/${selected//\//_}--${branch}"

if git -C "$repo_path" worktree add -b "$branch" "$wt_path" 2>/dev/null; then
  : # new branch created
elif git -C "$repo_path" worktree add "$wt_path" "$branch" 2>/dev/null; then
  : # existing branch
else
  # Show error in a popup
  tmux display-message "Failed to create worktree. Branch '$branch' may be checked out elsewhere."
  exit 1
fi

window_name="${selected//\//_}/$branch"
tmux new-window -d -n "$window_name" -c "$wt_path" "$HOME/.tmux/open-claude.sh"
