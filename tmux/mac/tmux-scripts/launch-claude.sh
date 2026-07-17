#!/usr/bin/env bash
# Fuzzy repo picker with git worktree support.
# If the selected repo already has an agent, offers to create a worktree.

# Fleet install root. Self-default so this resolves before env is sourced;
# env.defaults.sh re-exports the canonical value for spawned agents.
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
# Portable defaults first (committed; degrade path), then per-machine env.sh on top.
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && source "$NEXUS_TMUX_DIR/env.defaults.sh"
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && source "$NEXUS_TMUX_DIR/env.sh"
# nx-resolve: for the new-bucket slug≠host guard.
[ -f "$NEXUS_TMUX_DIR/agent-resolve.sh" ] && . "$NEXUS_TMUX_DIR/agent-resolve.sh"
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

# ── workspace bucket (herdr only; tmux has no workspaces) ────────────────────
# Ask which bucket this agent goes in: an existing live workspace, a new one from the fixed
# category vocab (interactive = one shared bucket; mission/swarm = <category>/<slug>), or flat.
# WS_LABEL threads into every spawn via nx_spawn. Gate off with NEXUS_WORKSPACES=0.
WS_LABEL=""
if [ "${NEXUS_SUBSTRATE:-herdr}" = "herdr" ] && [ "${NEXUS_WORKSPACES:-1}" = "1" ]; then
  _existing=$("$NEXUS_TMUX_DIR/substrate.sh" workspace-list 2>/dev/null | awk -F'\t' 'NF{print $2}' | grep -v '^~$')
  _pick=$(
    { echo "[flat / no bucket]"; [ -n "$_existing" ] && printf '%s\n' "$_existing"; echo "[new bucket…]"; } \
      | grep -v '^$' | fzf --prompt='bucket> ' --height=40% --border=rounded --header="workspace for $selected"
  )
  case "$_pick" in
    ""|"[flat / no bucket]") WS_LABEL="" ;;
    "[new bucket…]")
      _cat=$(grep -vE '^[[:space:]]*(#|$)' "$NEXUS_TMUX_DIR/workspace-categories.txt" 2>/dev/null \
             | fzf --prompt='category> ' --height=40% --border=rounded --print-query --query=interactive | tail -1)
      _cat="${_cat:-interactive}"
      _slug=$(printf '' | fzf --print-query --prompt="slug (blank = bucket '$_cat')> " --height=20% --border=rounded | head -1)
      if [ -n "$_slug" ]; then WS_LABEL="$_cat/$_slug"; else WS_LABEL="$_cat"; fi
      if command -v nx_known_host >/dev/null 2>&1 && nx_known_host "${WS_LABEL##*/}"; then
        echo "Bucket slug '${WS_LABEL##*/}' collides with a known host name — pick another." >&2; exit 1
      fi ;;
    *) WS_LABEL="$_pick" ;;
  esac
fi

# Spawn through the seam, into WS_LABEL when set (herdr bucket); flat otherwise. --focus:
# this is the INTERACTIVE picker — the user chose a repo to go work in, so switch the client
# to the new agent. Without it a spawn into a NON-current bucket lands invisibly (herdr's
# spawn default is no-focus, for background fan-out); tmux ignores --focus.
nx_spawn() {  # nx_spawn <name> <cwd> <cmd>
  if [ -n "${WS_LABEL:-}" ]; then
    "$NEXUS_TMUX_DIR/substrate.sh" spawn "$1" "$2" "$3" --workspace "$WS_LABEL" --focus
  else
    "$NEXUS_TMUX_DIR/substrate.sh" spawn "$1" "$2" "$3" --focus
  fi
}

# Handle general session
if [[ "$selected" == "[general]" ]]; then
  nx_spawn "general" "$HOME" "env PROJECT_SLUG=general $NEXUS_TMUX_DIR/open-claude.sh"
  exit 0
fi

# Handle worktree selection
if [[ "$selected" == "[wt] "* ]]; then
  wt_name="${selected#\[wt\] }"
  working_dir="$WT_DIR/$wt_name"
  window_name="${wt_name/--//}"  # repo--branch → repo/branch
  nx_spawn "$window_name" "$working_dir" "$NEXUS_TMUX_DIR/open-claude.sh"
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
    nx_spawn "$window_name" "$_repo_path" "$NEXUS_TMUX_DIR/open-claude.sh"
    exit 0
  fi
fi

# Decide whether to reuse the existing checkout or branch off into a worktree.
# A conflict only matters if ANOTHER LIVE AGENT is already working in this repo —
# a stray shell, or the pane you launched from, shouldn't force a worktree.
# Agents self-register in ~/.tmux/registry/ with PANE_ID + CWD, so check that
# rather than raw pane paths (which can't tell an agent from any old shell).
repo_path="$REPO_DIR/$selected"
self_pane="${TMUX_PANE:-}"
REGISTRY_DIR="$NEXUS_TMUX_DIR/registry"

agent_here=""
if [ -d "$REGISTRY_DIR" ]; then
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    pane_id=$(grep '^PANE_ID=' "$f" | cut -d= -f2)
    cwd=$(grep '^CWD=' "$f" | cut -d= -f2)
    [ -n "$self_pane" ] && [ "$pane_id" = "$self_pane" ] && continue       # skip ourselves
    "$NEXUS_TMUX_DIR/substrate.sh" pane-alive "$pane_id" || { rm -f "$f"; continue; }  # drop stale (herdr-aware)
    case "$cwd" in
      "$repo_path"|"$repo_path"/*) agent_here="$pane_id"; break ;;
    esac
  done
fi

if [ -z "$agent_here" ]; then
  # No other agent in this repo — open an agent in the existing checkout.
  nx_spawn "$selected" "$repo_path" "$NEXUS_TMUX_DIR/open-claude.sh"
  exit 0
fi

# Another agent is live here — reuse the checkout anyway, or branch off.
current_branch=$(git -C "$repo_path" branch --show-current 2>/dev/null || echo "unknown")
choice=$(
  printf '%s\n' "open here anyway (branch: $current_branch)" "create new worktree…" \
    | fzf --prompt="agent already in $selected> " --border=rounded \
        --header="An agent is already working in $selected"
)
[ -z "$choice" ] && exit 0
if [[ "$choice" == "open here anyway"* ]]; then
  nx_spawn "$selected" "$repo_path" "$NEXUS_TMUX_DIR/open-claude.sh"
  exit 0
fi
# Otherwise fall through to worktree creation below.

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
nx_spawn "$window_name" "$wt_path" "$NEXUS_TMUX_DIR/open-claude.sh"
