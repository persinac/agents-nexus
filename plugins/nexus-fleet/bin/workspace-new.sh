#!/usr/bin/env bash
# Create an EMPTY herdr workspace bucket (for manual tiling / pre-staging a mission).
# Bound to prefix+shift+b in the plugin manifest. The picker (prefix+shift+n) is the path
# that spawns an agent INTO a bucket; this one just makes the bucket.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_SUBSTRATE=herdr
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && . "$NEXUS_TMUX_DIR/env.sh"
[ -f "$NEXUS_TMUX_DIR/agent-resolve.sh" ] && . "$NEXUS_TMUX_DIR/agent-resolve.sh"

cat=$(grep -vE '^[[:space:]]*(#|$)' "$NEXUS_TMUX_DIR/workspace-categories.txt" 2>/dev/null \
      | fzf --prompt='category> ' --height=40% --border=rounded --print-query --query=interactive | tail -1)
cat="${cat:-interactive}"
slug=$(printf '' | fzf --print-query --prompt="slug (blank = bucket '$cat')> " --height=20% --border=rounded | head -1)
if [ -n "$slug" ]; then label="$cat/$slug"; else label="$cat"; fi

# Don't let a bucket slug shadow a known host name (the addressing grammar peels a known
# host off the front — a bucket named like a host would be unreachable by its slug).
if command -v nx_known_host >/dev/null 2>&1 && nx_known_host "${label##*/}"; then
  echo "Bucket slug '${label##*/}' collides with a known host name — pick another." >&2; sleep 2; exit 1
fi

# --focus: this is the interactive creator (prefix+shift+b) — switch the client to the new
# bucket. Without it the bucket is created but the view never moves, so it looks like a no-op.
id=$("$NEXUS_TMUX_DIR/substrate.sh" workspace-create "$label" --focus)
echo "created bucket '$label' (${id:-?})"; sleep 1
