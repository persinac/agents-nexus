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

# Dispatch. Precedence: explicit override -> macOS toast -> Linux toast -> bell.
if [ -n "${NEXUS_PRESENCE_NOTIFY_CMD:-}" ]; then
  NEXUS_PRESENCE_MSG="$msg" sh -c "$NEXUS_PRESENCE_NOTIFY_CMD" >/dev/null 2>&1 || true
elif [ "$(uname)" = "Darwin" ] && command -v osascript >/dev/null 2>&1; then
  safe="${msg//\"/}"                       # AppleScript string can't hold a raw double-quote
  sound="${NEXUS_PRESENCE_SOUND:-Submarine}"
  osascript -e "display notification \"$safe\" with title \"herdr · agent blocked\" sound name \"$sound\"" >/dev/null 2>&1 || true
elif command -v notify-send >/dev/null 2>&1; then
  notify-send -u critical "herdr · agent blocked" "$msg" >/dev/null 2>&1 || true
else
  printf '\a' >/dev/tty 2>/dev/null || true # last resort: terminal bell
fi
exit 0
