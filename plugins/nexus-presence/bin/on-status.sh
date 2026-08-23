#!/usr/bin/env bash
# nexus.presence — herdr event-hook for `pane.agent_status_changed`.
#
# Pops a DESKTOP notification the instant a herdr agent transitions to `blocked`
# (needs input: a permission prompt, an elicitation dialog, an end-of-turn
# question). Zero-infra & non-duplicative by design:
#   - Pure herdr event-hook -> OS notifier. No daemon, no Slack, no keybinding.
#   - The full nexus stack (substrated + slack-bridge) already pushes `blocked`
#     to Slack via its own events.subscribe. Presence uses the DESKTOP channel,
#     which that stack does not touch -> no double-notify. It is also the path
#     for a teammate running herdr + this plugin WITHOUT the daemon/bridge stack.
#   - Override the channel with NEXUS_PRESENCE_NOTIFY_CMD (message exported as
#     NEXUS_PRESENCE_MSG) to route anywhere — e.g. the Slack bus on a headless box.
#
# herdr injects: HERDR_PLUGIN_EVENT_JSON (the {event,data} payload), HERDR_PLUGIN_ROOT,
# HERDR_PLUGIN_STATE_DIR, HERDR_PANE_ID, HERDR_WORKSPACE_ID.

json="${HERDR_PLUGIN_EVENT_JSON:-}"
[ -n "$json" ] || exit 0

# Fast gate: act only on a transition TO `blocked`. Every other status change
# (idle/working/done/unknown) is a no-op with NO subprocess — the event fires a
# lot and herdr rate-limits plugin commands, so the common path must stay cheap.
# herdr serializes compact JSON, so the literal is `"agent_status":"blocked"`.
case "$json" in
  *'"agent_status":"blocked"'*) ;;
  *) exit 0 ;;
esac

# Build a human message from the payload. python3 is the stack's lingua franca and
# runs only on the (infrequent) blocked path, never on the hot no-op gate above.
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

# Optional breadcrumb (herdr's per-plugin state dir if it gave us one, else tmp).
# Every line is TAB-separated and starts with a verb, so the log answers "did the
# settle window help?" by counting: `edge` = blocked transitions seen, `suppressed`
# = resolved before the window expired (the win), `fired` = actually alerted.
log_dir="${HERDR_PLUGIN_STATE_DIR:-${TMPDIR:-/tmp}}"

_presence_log() {
  { printf '%s\t%s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$1" >> "$log_dir/presence.log"; } 2>/dev/null || true
}

_presence_log "edge	$msg	pane=${HERDR_PANE_ID:-?} ws=${HERDR_WORKSPACE_ID:-?}"

# Load the env layer — portable defaults, then per-machine env.sh on top (the same
# order as open-claude.sh). herdr runs plugin hooks with a STRIPPED environment (no
# NEXUS_* reaches us), so without this NEXUS_PRESENCE_NOTIFY_CMD / _SOUND set in
# env.sh are invisible here and the documented override silently does nothing.
# Sourced AFTER the fast gate above: only a real `blocked` transition pays for it,
# so the high-frequency no-op path still spawns nothing.
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
# shellcheck source=/dev/null
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && source "$NEXUS_TMUX_DIR/env.defaults.sh"
# shellcheck source=/dev/null
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && source "$NEXUS_TMUX_DIR/env.sh"

# Dispatch. Precedence: explicit override -> macOS toast -> Linux toast -> bell.
# A channel must SUCCEED to count as delivered; a channel that EXISTS but fails
# falls through to the next. The old form selected a branch on `command -v` alone
# and swallowed the result with `|| true`, so a present-but-broken notifier ate
# the alert and left the bell unreachable — e.g. notify-send on WSL/headless,
# where the binary installs fine but no org.freedesktop.Notifications daemon is
# registered on the session bus, so every toast fails with ServiceUnknown.
_presence_notify() {
  local msg="$1"
  local notified=0

  if [ -n "${NEXUS_PRESENCE_NOTIFY_CMD:-}" ]; then
    NEXUS_PRESENCE_MSG="$msg" sh -c "$NEXUS_PRESENCE_NOTIFY_CMD" >/dev/null 2>&1 && notified=1
  fi

  if [ "$notified" = 0 ] && [ "$(uname)" = "Darwin" ] && command -v osascript >/dev/null 2>&1; then
    local safe="${msg//\"/}"               # AppleScript string can't hold a raw double-quote
    local sound="${NEXUS_PRESENCE_SOUND:-Submarine}"
    osascript -e "display notification \"$safe\" with title \"herdr · agent blocked\" sound name \"$sound\"" >/dev/null 2>&1 && notified=1
  fi

  if [ "$notified" = 0 ] && command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "herdr · agent blocked" "$msg" >/dev/null 2>&1 && notified=1
  fi

  if [ "$notified" = 0 ]; then
    printf '\a' >/dev/tty 2>/dev/null || true # last resort: terminal bell
  fi
}

# --- Settle window (added 2026-08-21) --------------------------------------
# Alex's permission classifier (agents-nexus tmux/mac/tmux-scripts/notify-classify.py)
# auto-answers most prompts in 5-10s, because prompts it cannot decide from its
# deterministic allowlists cost a real API call. Firing "the instant" a pane goes
# blocked therefore alerted on prompts that were about to answer themselves:
# MEASURED 333 desktop toasts in one day, the same pane recurring every ~7s, which
# is exactly that round-trip. Every one was read after it had already resolved.
#
# So: defer, then RE-CHECK before alerting. herdr's own [ui.toast].delay_seconds
# does exactly this for its native toasts, but cannot reach us — this plugin owns
# the desktop channel and calls osascript directly.
#
# Deliberately NOT a daemon: the waiter is a detached child of this hook, so the
# plugin keeps its zero-infra property. The `blocked` edge is infrequent enough to
# afford one short-lived process; the hot no-op gate at the top of this file still
# spawns nothing at all.
_presence_still_blocked() {
  local pane="$1" hb="" c
  hb="$(command -v herdr 2>/dev/null)"
  if [ -z "$hb" ]; then                    # herdr strips our env, so PATH may not have it
    for c in /opt/homebrew/bin/herdr /usr/local/bin/herdr "$HOME/.local/bin/herdr"; do
      [ -x "$c" ] && hb="$c" && break
    done
  fi
  # No reachable binary -> fail OPEN and alert. A missed "needs input" leaves an
  # agent stuck indefinitely; a surplus toast costs a glance.
  [ -n "$hb" ] || return 0
  # `python3 -c` NOT `python3 - <<HEREDOC`: with a heredoc, python reads its PROGRAM
  # from stdin, so the piped snapshot is unreadable and json.load always throws —
  # which lands in the fail-open branch below and alerts every single time. That
  # exact bug shipped in the first draft of this function and presented as "the
  # settle window does nothing", because failing open is indistinguishable from
  # "still blocked". Keep stdin free for the pipe.
  "$hb" api snapshot 2>/dev/null | NEXUS_PANE="$pane" python3 -c '
import json, os, sys
pane = os.environ.get("NEXUS_PANE", "")
try:
    agents = json.load(sys.stdin)["result"]["snapshot"]["agents"]
except Exception:
    sys.exit(0)          # unreadable snapshot -> fail OPEN, alert
for a in agents:
    if a.get("pane_id") == pane:
        # agent_status is the live field. state_labels can retain a stale
        # {"blocked": "permission_prompt"} entry on a pane that is working
        # again, so reading that instead would defeat the whole check.
        sys.exit(0 if a.get("agent_status") == "blocked" else 1)
sys.exit(1)              # pane closed while we waited -> nothing to alert about
'
}

delay="${NEXUS_PRESENCE_DELAY_SECS:-15}"
case "$delay" in ''|*[!0-9]*) delay=15 ;; esac   # non-numeric override -> default

if [ "$delay" -gt 0 ] && [ -n "${HERDR_PANE_ID:-}" ]; then
  # One waiter per pane. mkdir is atomic, so a pane that flaps blocked -> clear ->
  # blocked inside the window cannot stack two waiters and double-alert. A stale
  # lock from a killed waiter is cleared by the age check below rather than living
  # forever and silencing the pane.
  lock="$log_dir/wait-$(printf '%s' "$HERDR_PANE_ID" | tr -c 'A-Za-z0-9_.-' '_').lock"
  if ! mkdir "$lock" 2>/dev/null; then
    if [ -n "$(find "$lock" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then
      rmdir "$lock" 2>/dev/null && mkdir "$lock" 2>/dev/null || exit 0
    else
      exit 0                               # a live waiter already owns this pane
    fi
  fi
  _presence_log "deferred ${delay}s	pane=${HERDR_PANE_ID:-?} ws=${HERDR_WORKSPACE_ID:-?}"
  (
    trap 'rmdir "$lock" 2>/dev/null' EXIT
    sleep "$delay"
    if _presence_still_blocked "$HERDR_PANE_ID"; then
      _presence_log "fired	$msg	pane=${HERDR_PANE_ID:-?} ws=${HERDR_WORKSPACE_ID:-?}"
      _presence_notify "$msg"
    else
      _presence_log "suppressed (resolved in <${delay}s)	pane=${HERDR_PANE_ID:-?} ws=${HERDR_WORKSPACE_ID:-?}"
    fi
  ) </dev/null >/dev/null 2>&1 &
  disown 2>/dev/null || true               # survive this hook's exit
  exit 0
fi

_presence_notify "$msg"
exit 0
