#!/usr/bin/env bash
# Create an EMPTY herdr workspace bucket (for manual tiling / pre-staging a mission). Linux twin
# of the mac one; bound to prefix+shift+b. The picker (prefix+shift+n) spawns an agent INTO a
# bucket; this just makes the bucket.
export PATH="$HOME/.local/bin:$HOME/.local/share/fnm/aliases/default/bin:/usr/local/bin:/usr/bin:$PATH"
export NEXUS_SUBSTRATE=herdr
[ -f "$HOME/.tmux/env.sh" ] && . "$HOME/.tmux/env.sh"
[ -f "$HOME/.tmux/agent-resolve.sh" ] && . "$HOME/.tmux/agent-resolve.sh"

cat=$(grep -vE '^[[:space:]]*(#|$)' "$HOME/.tmux/workspace-categories.txt" 2>/dev/null \
      | fzf --prompt='category> ' --height=40% --border=rounded --print-query --query=interactive | tail -1)
cat="${cat:-interactive}"
slug=$(printf '' | fzf --print-query --prompt="slug (blank = bucket '$cat')> " --height=20% --border=rounded | head -1)
if [ -n "$slug" ]; then label="$cat/$slug"; else label="$cat"; fi

if command -v nx_known_host >/dev/null 2>&1 && nx_known_host "${label##*/}"; then
  echo "Bucket slug '${label##*/}' collides with a known host name — pick another." >&2; sleep 2; exit 1
fi

# --focus: this is the interactive creator (prefix+shift+b) — switch the client to the new
# bucket. Without it the bucket is created but the view never moves, so it looks like a no-op.
id=$("$HOME/.tmux/substrate.sh" workspace-create "$label" --focus)
echo "created bucket '$label' (${id:-?})"; sleep 1
