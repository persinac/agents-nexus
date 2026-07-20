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
log_dir="${HERDR_PLUGIN_STATE_DIR:-${TMPDIR:-/tmp}}"
{ printf '%s\t%s\tpane=%s ws=%s\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S')" "$msg" \
    "${HERDR_PANE_ID:-?}" "${HERDR_WORKSPACE_ID:-?}" >> "$log_dir/presence.log"; } 2>/dev/null || true

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
