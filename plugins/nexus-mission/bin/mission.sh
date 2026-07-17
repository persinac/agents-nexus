#!/usr/bin/env bash
# nexus.mission launcher: pick a Conductor mission mode, enter a goal/ticket, kick it off.
#   distribute -> python conductor.py --distribute "<goal>"  (returns immediately; the
#                 mission runs DETACHED in its own tiled mission/<slug> bucket + reports out)
#   sdlc       -> python conductor.py --sdlc "<ticket|goal>" (external SDLC pipeline -> plan.md;
#                 runs FOREGROUND in this pane; needs a context-repos workspace)
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
# set -a so AGENTS_NEXUS_DIR / DATABASE_URL / proxy vars reach the conductor (fresh process).
set -a
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && . "$NEXUS_TMUX_DIR/env.defaults.sh"
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && . "$NEXUS_TMUX_DIR/env.sh"
set +a
NEXUS_DIR="${AGENTS_NEXUS_DIR:-$HOME/repos/agents-nexus}"
PY="$NEXUS_DIR/agent-runner/.venv/bin/python"
CONDUCTOR="$NEXUS_DIR/agent-runner/conductor.py"

[ -x "$PY" ] || { echo "✗ conductor venv not found at $PY"; read -rp "enter to close… " _; exit 1; }

# 1. pick mode (fzf)
mode=$(printf 'distribute\tfan-out build/verify mission — detached, works today\nsdlc\texternal SDLC pipeline → plan.md — needs a context-repos workspace\n' \
  | fzf --delimiter='\t' --with-nth=1,2 --height=40% --prompt='mission mode> ' \
        --header='pick a Conductor mission mode (esc cancels)' | cut -f1)
[ -n "$mode" ] || { echo "cancelled"; exit 0; }

# 2. sdlc preflight — the pipeline needs cloned context repos (project-context-*) under one
#    of the CUSTOM_WORKSPACE_ROOTS (colon-separated list; default ~/code).
if [ "$mode" = "sdlc" ]; then
  WS=""
  IFS=: read -ra _roots <<< "${CUSTOM_WORKSPACE_ROOTS:-$HOME/code}"
  for _r in "${_roots[@]}"; do
    [ -n "$_r" ] && ls -d "$_r"/project-context-* >/dev/null 2>&1 && { WS="$_r"; break; }
  done
  if [ -z "$WS" ]; then
    echo "✗ sdlc workspace not found — no project-context-* under: ${CUSTOM_WORKSPACE_ROOTS:-$HOME/code}"
    echo
    echo "  Enable sdlc missions: clone the context repos (project-context-*) into a dir listed"
    echo "  in CUSTOM_WORKSPACE_ROOTS (colon-separated; default ~/code), or set"
    echo "  sdlc.workspace_root in conductor.yaml."
    echo "  (distribute missions work without this.)"
    echo
    read -rp "press enter to close… " _
    exit 0
  fi
fi

# 3. goal / ticket
prompt_label=$([ "$mode" = sdlc ] && echo "ticket/goal" || echo "goal")
read -rp "${mode} ${prompt_label}> " goal
[ -n "$goal" ] || { echo "no ${prompt_label} entered; cancelled"; exit 0; }

# 4. kick the Conductor
echo "→ launching ${mode} mission: ${goal}"
echo
if [ "$mode" = "sdlc" ]; then
  exec "$PY" "$CONDUCTOR" --sdlc "$goal"
else
  "$PY" "$CONDUCTOR" --distribute "$goal"
  echo
  read -rp "mission dispatched (detached — see the bucket above). press enter to close… " _
fi
