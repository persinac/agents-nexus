#!/usr/bin/env bash

# nexus.presence hook: toast when an agent is blocked AND still blocked after
# the settle window. Knobs, injected env vars, measurements: ../README.md

json="${HERDR_PLUGIN_EVENT_JSON:-}"
[ -n "$json" ] || exit 0

# Every other status fires here too, so this gate must stay subprocess-free.
case "$json" in
  *'"agent_status":"blocked"'*) ;;
  *) exit 0 ;;
esac

# herdr waits for plugin commands, so this must not sleep: detach and return.
# Injected vars don't survive the detach — pass each one the child needs.
if [ -z "${NEXUS_PRESENCE_SETTLED:-}" ]; then
  NEXUS_PRESENCE_SETTLED=1 \
  HERDR_PLUGIN_EVENT_JSON="$json" \
  HERDR_PLUGIN_STATE_DIR="${HERDR_PLUGIN_STATE_DIR:-}" \
  HERDR_PANE_ID="${HERDR_PANE_ID:-}" \
  HERDR_WORKSPACE_ID="${HERDR_WORKSPACE_ID:-}" \
  HERDR_BIN_PATH="${HERDR_BIN_PATH:-}" \
  NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-}" \
    nohup "$0" </dev/null >/dev/null 2>&1 &
  disown 2>/dev/null || true
  exit 0
fi

# --- detached worker: everything below is off herdr's critical path ---

# herdr strips the env, so the NEXUS_PRESENCE_* knobs are invisible without this.
# Sourced here and not in the hook: it costs ~945ms, which was most of the old ~1s.
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
# shellcheck source=/dev/null
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && source "$NEXUS_TMUX_DIR/env.defaults.sh"
# shellcheck source=/dev/null
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && source "$NEXUS_TMUX_DIR/env.sh"

delay="${NEXUS_PRESENCE_DELAY_SECS:-15}"
case "$delay" in ''|*[!0-9]*) delay=15 ;; esac   # non-numeric -> default, never error

log_dir="${HERDR_PLUGIN_STATE_DIR:-${TMPDIR:-/tmp}}"
pane="${HERDR_PANE_ID:-?}"

if [ "$delay" -gt 0 ]; then
  sleep "$delay"

  herdr_bin="${HERDR_BIN_PATH:-}"
  if [ -z "$herdr_bin" ] || [ ! -x "$herdr_bin" ]; then
    herdr_bin="$(command -v herdr 2>/dev/null)"
  fi
  if [ -z "$herdr_bin" ] || [ ! -x "$herdr_bin" ]; then
    [ -x /opt/homebrew/bin/herdr ] && herdr_bin=/opt/homebrew/bin/herdr
  fi
  if [ -z "$herdr_bin" ] || [ ! -x "$herdr_bin" ]; then
    [ -x /usr/local/bin/herdr ] && herdr_bin=/usr/local/bin/herdr
  fi

  # Fails OPEN: an unreadable status notifies. A swallowed ping strands an agent.
  if [ -n "$herdr_bin" ] && [ -n "${HERDR_PANE_ID:-}" ]; then
    cur="$("$herdr_bin" agent get "$HERDR_PANE_ID" 2>/dev/null)"
    if [ -n "$cur" ]; then
      case "$cur" in
        *'"agent_status":"blocked"'*) ;;   # still waiting on a human -> notify
        *)
          { printf '%s\t%s\tpane=%s ws=%s outcome=suppressed(settled)\n' \
              "$(date '+%Y-%m-%dT%H:%M:%S')" "recovered within ${delay}s" \
              "$pane" "${HERDR_WORKSPACE_ID:-?}" >> "$log_dir/presence.log"; } 2>/dev/null || true
          exit 0 ;;
      esac
    fi
  fi
fi

# Stamp, not a lock: a race costs a duplicate toast, a stale lock mutes forever.
cooldown="${NEXUS_PRESENCE_COOLDOWN_SECS:-$delay}"
case "$cooldown" in ''|*[!0-9]*) cooldown="$delay" ;; esac
if [ "$cooldown" -gt 0 ]; then
  stamp="$log_dir/last-notify.$(printf '%s' "$pane" | tr -c 'A-Za-z0-9._-' '_')"
  now="$(date +%s)"
  last="$(cat "$stamp" 2>/dev/null)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ "$((now - last))" -lt "$cooldown" ]; then
    { printf '%s\t%s\tpane=%s ws=%s outcome=suppressed(cooldown)\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S')" "toast $((now - last))s ago" \
        "$pane" "${HERDR_WORKSPACE_ID:-?}" >> "$log_dir/presence.log"; } 2>/dev/null || true
    exit 0
  fi
  { printf '%s\n' "$now" > "$stamp"; } 2>/dev/null || true
fi

msg="$(python3 - <<'PY' 2>/dev/null
import json, os
try:
    d = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")).get("data", {})
except Exception:
    d = {}
who = d.get("title") or d.get("display_agent") or d.get("agent") or "an agent"
agent = d.get("agent")
extra = d.get("custom_status")
tag = f" [{extra}]" if extra else ""
suffix = f" ({agent})" if agent and agent != who else ""
print(f"{who}{suffix} is blocked — needs input{tag}")
PY
)"
[ -n "$msg" ] || msg="an agent is blocked — needs input"

{ printf '%s\t%s\tpane=%s ws=%s outcome=notified\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S')" "$msg" \
    "$pane" "${HERDR_WORKSPACE_ID:-?}" >> "$log_dir/presence.log"; } 2>/dev/null || true

# A channel must SUCCEED to count as delivered, else fall through to the next:
# notify-send installs fine on WSL/headless with no daemon, and once ate the alert.
notified=0

if [ -n "${NEXUS_PRESENCE_NOTIFY_CMD:-}" ]; then
  NEXUS_PRESENCE_MSG="$msg" sh -c "$NEXUS_PRESENCE_NOTIFY_CMD" >/dev/null 2>&1 && notified=1
fi

if [ "$notified" = 0 ] && [ "$(uname)" = "Darwin" ] && command -v osascript >/dev/null 2>&1; then
  safe="${msg//\"/}"                       # AppleScript string can't hold a raw double-quote
  sound="${NEXUS_PRESENCE_SOUND:-Submarine}"
  osascript -e "display notification \"$safe\" with title \"herdr · agent blocked\" sound name \"$sound\"" >/dev/null 2>&1 && notified=1
fi

if [ "$notified" = 0 ] && command -v notify-send >/dev/null 2>&1; then
  notify-send -u critical "herdr · agent blocked" "$msg" >/dev/null 2>&1 && notified=1
fi

if [ "$notified" = 0 ]; then
  printf '\a' >/dev/tty 2>/dev/null || true # last resort: terminal bell
fi
exit 0
