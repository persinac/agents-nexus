#!/usr/bin/env bash
# herdr plugin pane: interactive keyword search over the agent-memory notes store.
# Ports the dashboard's "search notes" feature to a terminal pane. Wraps the fleet's
# memory-search.py (keyword, embedding-free — queries agents.memory_nodes via
# DATABASE_URL, parameterized), which prints a JSON array; this loops a prompt and
# renders the hits readably. Semantic search (embeddings) stays in the dashboard /
# `agent_memory.cli search`; this is the fast keyword path.
#
# Sources the env layer so AGENTS_NEXUS_DIR / DATABASE_URL resolve; degrades to a
# clear "DATABASE_URL not set" message if the DB is unreachable (never crashes the pane).
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"

# set -a so the env layer's vars (AGENTS_NEXUS_DIR, …) export to the python subprocess.
# NOTE: DATABASE_URL is NOT set by env.sh/env.defaults.sh — memory-search.py self-loads it
# from the repo .env (os.environ.setdefault). We fold that .env in here too so DATABASE_URL is
# genuinely present in the pane env (and any peer script that reads it), matching how the MCP
# server + memory-search.py resolve it.
set -a
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && . "$NEXUS_TMUX_DIR/env.defaults.sh"
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && . "$NEXUS_TMUX_DIR/env.sh"
set +a

NEXUS_DIR="${AGENTS_NEXUS_DIR:-$HOME/repos/agents-nexus}"

# Fold repo .env (fill-gaps: never clobber an already-set var) so DATABASE_URL resolves the same
# way memory-search.py does. Read KEY=value literally — no shell eval of values.
if [ -f "$NEXUS_DIR/.env" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in ''|\#*) continue ;; esac
    [ -z "$(eval "printf '%s' \"\${$_k:-}\"")" ] && export "$_k=$_v"
  done < "$NEXUS_DIR/.env"
fi

# Resolve memory-search.py: prefer the ~/.tmux symlink (installed from tmux-scripts),
# fall back to the in-repo source so the panel works even where the symlink is absent.
SEARCH_PY=""
for cand in "$NEXUS_TMUX_DIR/memory-search.py" "$NEXUS_DIR/tmux/mac/tmux-scripts/memory-search.py"; do
  [ -f "$cand" ] && { SEARCH_PY="$cand"; break; }
done

# The search script needs psycopg — use the mnemon venv python (same as memory-status.sh),
# falling back to system python3.
PYTHON="$NEXUS_DIR/mnemon/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="python3"

# Query scope: default to the current repo's project (basename of the launch cwd) but
# allow "all". Overridable per-search with a `p:<name>` or `all:` prefix (see prompt).
default_project="all"

RENDER_PY="$(dirname "$0")/memory-search-render.py"

hr() { printf '%s\n' "────────────────────────────────────────────────────────────"; }

# render <query>: reads memory-search.py's JSON on stdin (via the pipe below) and prints it.
# Uses a SEPARATE python file (not an inline heredoc) so the piped JSON reaches stdin — a
# `python - <<'PY'` heredoc would consume stdin as the program and lose the pipe.
render() {
  "$PYTHON" "$RENDER_PY" "$1"
}

clear 2>/dev/null || true
echo "  Agent-memory notes search (keyword)"
if [ -z "$SEARCH_PY" ]; then
  echo "  !! memory-search.py not found (looked in ~/.tmux and the repo). Run the base install."
  echo "  Press Enter to close…"; read -r _; exit 0
fi
if [ -z "${DATABASE_URL:-}" ]; then
  echo "  !! DATABASE_URL not found (repo .env has no DATABASE_URL) — searches will return nothing"
  echo "     until the memory DB is configured. (The memory-health panel notes the same.)"
fi
echo "  Scope: $default_project   ·   prefix a query with 'p:<project>' or 'all:' to change it,"
echo "  ':q' or empty-then-Ctrl-D to quit."
hr

while true; do
  printf 'search> '
  IFS= read -r line || break            # EOF (Ctrl-D) exits
  line="${line#"${line%%[![:space:]]*}"}"   # ltrim
  [ -z "$line" ] && continue
  case "$line" in
    :q|:quit|quit|exit) break ;;
  esac

  project="$default_project"
  q="$line"
  case "$line" in
    all:*)  project="all"; q="${line#all:}" ;;
    p:*)    rest="${line#p:}"; project="${rest%% *}"; q="${rest#* }"
            [ "$project" = "$rest" ] && q="" ;;   # "p:foo" with no query
  esac
  q="${q#"${q%%[![:space:]]*}"}"
  [ -z "$q" ] && { echo "  (enter a search term)"; continue; }

  hr
  echo "  '$q'  (project: $project)"
  echo
  "$PYTHON" "$SEARCH_PY" --query "$q" --project "$project" --limit 10 --format json 2>/dev/null \
    | render "$q"
  hr
done
