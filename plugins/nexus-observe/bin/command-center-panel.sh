#!/usr/bin/env bash
# herdr plugin pane: fleet "command center" — a refreshing TUI that ports the dashboard's
# command-center 2x2 grid (health dots + Agents / Services / Timers) to a terminal pane.
# Composes the existing fleet primitives (no arbiter needed):
#   - health : docker info, substrate has-session, memory-stats.py (DB)
#   - agents : substrate.sh query  (index|name|@waiting|path|command|@wait_type)
#   - services: docker ps
#   - timers : launchd (~/Library/LaunchAgents/com.agents-nexus.*) or systemd --user timers
# Read-only. Refreshes every REFRESH secs (default 5). Ctrl-C / pane-close to exit.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"

set -a
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && . "$NEXUS_TMUX_DIR/env.defaults.sh"
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && . "$NEXUS_TMUX_DIR/env.sh"
set +a

NEXUS_DIR="${AGENTS_NEXUS_DIR:-$HOME/repos/agents-nexus}"
# Fold repo .env (fill-gaps) so DATABASE_URL resolves for the DB-health probe, same as
# memory-search.py / the MCP server do (env.sh does not set it).
if [ -f "$NEXUS_DIR/.env" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in ''|\#*) continue ;; esac
    [ -z "$(eval "printf '%s' \"\${$_k:-}\"")" ] && export "$_k=$_v"
  done < "$NEXUS_DIR/.env"
fi

SUBSTRATE="$NEXUS_TMUX_DIR/substrate.sh"
PYTHON="$NEXUS_DIR/mnemon/.venv/bin/python3"; [ -x "$PYTHON" ] || PYTHON="python3"
STATS_PY=""
for c in "$NEXUS_TMUX_DIR/memory-stats.py" "$NEXUS_DIR/tmux/mac/tmux-scripts/memory-stats.py"; do
  [ -f "$c" ] && { STATS_PY="$c"; break; }
done
REFRESH="${NEXUS_CC_REFRESH:-5}"

dot() { [ "$1" = "1" ] && printf '●' || printf '○'; }   # ● up / ○ down

waiting_label() {
  case "$1" in
    0) printf 'working' ;;
    1) printf 'BLOCKED' ;;
    2) printf 'idle' ;;
    *) printf '?' ;;
  esac
}

render() {
  local now; now=$(date '+%H:%M:%S')
  # ── health probes ──────────────────────────────────────────────
  local docker_up=0 ccount=0 tmux_up=0 db_up=0
  if command -v docker >/dev/null 2>&1; then
    local ci; ci=$(docker info --format '{{.ContainersRunning}}' 2>/dev/null)
    [ -n "$ci" ] && { docker_up=1; ccount="${ci:-0}"; }
  fi
  "$SUBSTRATE" has-session >/dev/null 2>&1 && tmux_up=1
  local db_json=""
  [ -n "$STATS_PY" ] && db_json=$("$PYTHON" "$STATS_PY" 2>/dev/null)
  case "$db_json" in *'"error"'*|'') db_up=0 ;; *'notes_total'*) db_up=1 ;; esac

  clear 2>/dev/null || printf '\033[2J\033[H'
  printf 'Nexus Command Center   %s   (refresh %ss · Ctrl-C to exit)\n' "$now" "$REFRESH"
  printf 'health:  docker %s (%s)   substrate %s   db %s\n' \
    "$(dot "$docker_up")" "$ccount" "$(dot "$tmux_up")" "$(dot "$db_up")"
  if [ "$db_up" = "1" ] && [ -n "$db_json" ]; then
    printf '%s' "$db_json" | "$PYTHON" -c "import sys,json;
d=json.load(sys.stdin)
print('  memory: %s notes (%s embedded) · %s events/24h' % (d.get('notes_total','?'), d.get('notes_embedded','?'), d.get('events_24h','?')))" 2>/dev/null
  fi
  printf '────────────────────────────────────────────────────────────\n'

  # ── agents (substrate query) ───────────────────────────────────
  printf 'AGENTS\n'
  local q; q=$("$SUBSTRATE" query 2>/dev/null)
  if [ -z "$q" ]; then
    printf '  (none)\n'
  else
    printf '%s\n' "$q" | while IFS='|' read -r idx name waiting path cmd wtype; do
      [ -z "$name" ] && continue
      local repo; repo=$(basename "$path" 2>/dev/null)
      printf '  %-8s %-28s %-8s %s\n' "$idx" "${name:0:28}" "$(waiting_label "$waiting")" "$repo"
    done
  fi
  printf '────────────────────────────────────────────────────────────\n'

  # ── services (docker ps) ───────────────────────────────────────
  printf 'SERVICES\n'
  if [ "$docker_up" = "1" ]; then
    local svc; svc=$(docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null | rg -a '^nexus-|^langfuse-' 2>/dev/null || docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null | grep -E '^nexus-|^langfuse-')
    if [ -z "$svc" ]; then printf '  (no nexus containers running)\n'; else
      printf '%s\n' "$svc" | while IFS=$'\t' read -r n s; do printf '  %-22s %s\n' "$n" "$s"; done
    fi
  else
    printf '  (docker down)\n'
  fi
  printf '────────────────────────────────────────────────────────────\n'

  # ── timers (launchd / systemd) ─────────────────────────────────
  printf 'TIMERS\n'
  case "$(uname -s)" in
    Darwin)
      local d="$HOME/Library/LaunchAgents"
      if [ -d "$d" ]; then
        local found=0
        for p in "$d"/com.agents-nexus.*.plist; do
          [ -f "$p" ] || continue
          found=1
          local label; label=$(basename "$p" .plist | sed 's/^com\.agents-nexus\.//')
          launchctl list 2>/dev/null | grep -q "com.agents-nexus.$label" \
            && printf '  %s %s\n' "$(dot 1)" "$label" \
            || printf '  %s %s (not loaded)\n' "$(dot 0)" "$label"
        done
        [ "$found" = "0" ] && printf '  (none installed)\n'
      else
        printf '  (no LaunchAgents dir)\n'
      fi ;;
    *)
      local t; t=$(systemctl --user list-timers --no-pager --no-legend 2>/dev/null | grep -a 'nexus\|agents-nexus' | head -10)
      [ -n "$t" ] && printf '%s\n' "$t" | sed 's/^/  /' || printf '  (no user timers)\n' ;;
  esac
}

# Refresh loop. A single render on non-TTY (piped) so tests/one-shots don't spin.
if [ -t 1 ]; then
  trap 'printf "\n"; exit 0' INT TERM
  while true; do
    render
    sleep "$REFRESH"
  done
else
  render
fi
