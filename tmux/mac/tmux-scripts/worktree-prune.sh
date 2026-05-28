#!/usr/bin/env bash
# Interactive worktree cleanup. fzf multi-select the worktrees to remove.
# Bound to ctrl+a → W. Companion to launch-claude.sh (which creates them) and
# worktree-cleanup.sh (the pane-died auto-cleanup that skips dirty trees).

[ -f "$HOME/.tmux/env.sh" ] && source "$HOME/.tmux/env.sh"
REPO_DIR="${REPO_DIR:-$HOME/repos}"
WT_DIR="$REPO_DIR/.worktrees"

if [ ! -d "$WT_DIR" ] || [ -z "$(ls -A "$WT_DIR" 2>/dev/null)" ]; then
  echo "No worktrees under $WT_DIR"
  read -rp "Press enter to close…" _
  exit 0
fi

# Build list with a dirty/clean marker so it's obvious what has unsaved work.
list=""
for wt in "$WT_DIR"/*/; do
  [ -d "$wt" ] || continue
  name=$(basename "$wt")
  n=$(git -C "$wt" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  if [ "$n" -gt 0 ]; then
    list+="✗ dirty($n)  $name"$'\n'
  else
    list+="✓ clean      $name"$'\n'
  fi
done

selected=$(
  printf '%s' "$list" | grep -v '^$' \
    | fzf --multi --prompt='remove worktrees (TAB to mark)> ' \
        --height=100% --border=rounded \
        --header='TAB: toggle · ENTER: remove selected · ESC: cancel' \
        --preview="git -C '$WT_DIR'/\$(echo {} | sed 's/^[^ ]* *[^ ]* *//') status -sb 2>/dev/null; echo; git -C '$WT_DIR'/\$(echo {} | sed 's/^[^ ]* *[^ ]* *//') log --oneline -5 2>/dev/null" \
        --preview-window=right:50%
)

[ -z "$selected" ] && exit 0

echo "Removing $(printf '%s\n' "$selected" | grep -c .) worktree(s)…"
echo
declare -A parents
while IFS= read -r line; do
  [ -z "$line" ] && continue
  name=$(echo "$line" | sed 's/^[^ ]* *[^ ]* *//')
  wt="$WT_DIR/$name"
  main=$(dirname "$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null)") || { echo "SKIP $name (no parent repo)"; continue; }
  parents["$main"]=1
  if git -C "$main" worktree remove --force "$wt" 2>/dev/null; then
    echo "removed  $name"
  else
    echo "FAILED   $name"
  fi
done <<< "$selected"

# Tidy stale admin records in each touched repo.
for r in "${!parents[@]}"; do
  git -C "$r" worktree prune 2>/dev/null
done

echo
echo "Note: branches are preserved — use 'git branch -D <name>' to drop them too."
read -rp "Press enter to close…" _
