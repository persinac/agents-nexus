#!/usr/bin/env bash
# Fuzzy repo picker with git worktree support.
# If the selected repo already has an agent, offers to create a worktree.

# Load machine-specific overrides (set by installer)
[ -f "$HOME/.tmux/env.sh" ] && source "$HOME/.tmux/env.sh"
REPO_DIR="${REPO_DIR:-$HOME/repos}"
WT_DIR="$REPO_DIR/.worktrees"

# Build combined list: repos + extra dirs + existing worktrees
repos=$(
  find "$REPO_DIR" -maxdepth 4 \( -name '.git' -type d -o -name '.git' -type f \) 2>/dev/null \
    | sed "s|${REPO_DIR}/||; s|/\.git$||" \
    | grep -v '^\.worktrees' \
    | sort
)

# Extra repo directories (colon-separated, e.g. ~/projects:/work/repos)
# Each repo is prefixed with [dirname] so it's visually distinct in fzf
extra_repos=""
IFS=: read -ra _extra_dirs <<< "${EXTRA_REPO_DIRS:-}"
for _dir in "${_extra_dirs[@]}"; do
  [ -d "$_dir" ] || continue
  _label=$(basename "$_dir")
  _found=$(
    find "$_dir" -maxdepth 4 \( -name '.git' -type d -o -name '.git' -type f \) 2>/dev/null \
      | sed "s|${_dir}/||; s|/\.git$||" \
      | grep -v '^\.worktrees' \
      | sed "s|^|[${_label}] |" \
      | sort
  )
  [ -n "$_found" ] && extra_repos+="$_found"$'\n'
done

worktrees=""
if [ -d "$WT_DIR" ]; then
  worktrees=$(ls -1 "$WT_DIR" 2>/dev/null | sed 's/^/[wt] /')
fi

# Merge into fzf
selected=$(
  { echo "[general]"; echo "$repos"; [ -n "$extra_repos" ] && printf '%s' "$extra_repos"; [ -n "$worktrees" ] && echo "$worktrees"; } \
    | grep -v '^$' \
    | fzf --prompt='repo> ' \
        --height=100% \
        --border=rounded \
        --preview='if [ "{}" = "[general]" ]; then echo "General session — cross-project memory, no repo context"; else ls '"${REPO_DIR}"'/{} 2>/dev/null; fi' \
        --preview-window=right:40%
)

[ -z "$selected" ] && exit 0

# Handle general session
if [[ "$selected" == "[general]" ]]; then
  tmux new-window -d -n "general" -c "$HOME" "env PROJECT_SLUG=general $HOME/.tmux/open-claude.sh"
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

# Handle extra-dir selection — tagged as [dirname] reponame
if [[ "$selected" =~ ^\[([^]]+)\]\ (.+)$ ]]; then
  _label="${BASH_REMATCH[1]}"
  _rel="${BASH_REMATCH[2]}"
  # Resolve which extra dir this label belongs to
  _repo_path=""
  IFS=: read -ra _extra_dirs <<< "${EXTRA_REPO_DIRS:-}"
  for _d in "${_extra_dirs[@]}"; do
    [ "$(basename "$_d")" = "$_label" ] || continue
    _repo_path="$_d/$_rel"
    break
  done
  if [ -n "$_repo_path" ]; then
    window_name=$(basename "$_rel")
    tmux new-window -d -n "$window_name" -c "$_repo_path" "$HOME/.tmux/open-claude.sh"
    exit 0
  fi
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
  tmux display-message "Failed to create worktree. Branch '$branch' may be checked out elsewhere."
  exit 1
fi

window_name="${selected//\//_}/$branch"
tmux new-window -d -n "$window_name" -c "$wt_path" "$HOME/.tmux/open-claude.sh"
