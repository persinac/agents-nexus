#!/usr/bin/env bash
# nexus.presence — herdr event-hook for `pane.agent_status_changed`.
#
# Pops a DESKTOP notification when a herdr agent transitions to `blocked`
# (needs input: a permission prompt, an elicitation dialog, an end-of-turn
# question) AND is still blocked once the settle window expires. Zero-infra &
# non-duplicative by design:
#   - Pure herdr event-hook -> OS notifier. No daemon, no Slack, no keybinding.
#   - The full nexus stack (substrated + slack-bridge) already pushes `blocked`
#     to Slack via its own events.subscribe. Presence uses the DESKTOP channel,
#     which that stack does not touch -> no double-notify. It is also the path
#     for a teammate running herdr + this plugin WITHOUT the daemon/bridge stack.
#   - Override the channel with NEXUS_PRESENCE_NOTIFY_CMD (message exported as
#     NEXUS_PRESENCE_MSG) to route anywhere — e.g. the Slack bus on a headless box.
#
# THE SETTLE WINDOW (added 2026-08-26). On a box running the nexus Notification
# hook (~/.tmux/hook-notification.sh -> notify-classify.py), most permission
# prompts are auto-answered by the classifier within ~0.4-3s and never reach a
# human. herdr's status machine still sees a real blocked edge for each one, so
# notifying on the edge itself pinged the human for prompts that were already
# handled. Measured before this change, from this hook's own presence.log joined
# against ~/.tmux/auto-approve.log: 1,596 toasts over three days, 82% of them for
# prompts the classifier took, 5.5% genuine. On 2026-08-26 alone, 71 of 74.
#
# So: defer, re-read the pane's CURRENT status, and notify only if it is still
# blocked — the same semantics herdr documents for its own [ui.toast]
# delay_seconds ("notifies only if the pane is still in the same state when the
# delay expires"). Two knobs, both optional:
#   NEXUS_PRESENCE_DELAY_SECS     settle window, default 15. 0 disables both the
#                                 wait and the re-check, restoring the
#                                 pre-2026-08-26 "every blocked edge notifies"
#                                 behavior. Note 0 means "no settle", NOT
#                                 "synchronous": the worker is always detached,
#                                 so even at 0 the toast lands a beat later
#                                 (dominated by the env-layer source, ~1s here).
#                                 Imperceptible for a desktop toast, and it is
#                                 what keeps the hook itself cheap.
#   NEXUS_PRESENCE_COOLDOWN_SECS  min seconds between toasts for one pane,
#                                 default = the settle window.
#
# Why deferred in a DETACHED child rather than sleeping in the hook: herdr
# supervises plugin commands — it waits for exit and records start/finish (see
# `herdr plugin log list --plugin nexus.presence`). Blocked-path runs measured
# ~1s before this change; sleeping 15s in the hook would hold a supervised child
# open that long on every prompt. Survival of the detached child past the
# parent's exit and reaping was probe-verified before shipping.
#
# The hook (parent) therefore does the minimum: gate, spawn, exit — measured
# ~100ms, down from ~1s. EVERYTHING else runs in the child, including sourcing
# the env layer. That last part is not incidental: profiling put env.defaults.sh
# + env.sh at ~945ms of the old ~1s, far more than the python3 build or the
# osascript call. Sourcing it in the hook would have left the parent as
# expensive as before and made the detach pointless, so the parent reads no
# NEXUS_* knobs at all and always defers; the child resolves the real delay
# (0 included, which it honors by skipping both the sleep and the re-check).
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

# --- Stage 1: the hook itself. Spawn the worker and get out. ---
# Deliberately does NOT source the env layer and reads no NEXUS_* knob — see the
# header. Gate, spawn, exit. NEXUS_PRESENCE_SETTLED marks the re-exec so the
# child skips this block; herdr's injected vars do not survive the detach, so
# each one the child needs is passed explicitly.
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

# ============================ detached worker ============================
# Everything below runs off herdr's critical path.

# Load the env layer — portable defaults, then per-machine env.sh on top (the same
# order as open-claude.sh). herdr runs plugin hooks with a STRIPPED environment (no
# NEXUS_* reaches us), so without this NEXUS_PRESENCE_NOTIFY_CMD / _SOUND /
# _DELAY_SECS set in env.sh are invisible here and the documented overrides
# silently do nothing.
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
# shellcheck source=/dev/null
[ -f "$NEXUS_TMUX_DIR/env.defaults.sh" ] && source "$NEXUS_TMUX_DIR/env.defaults.sh"
# shellcheck source=/dev/null
[ -f "$NEXUS_TMUX_DIR/env.sh" ] && source "$NEXUS_TMUX_DIR/env.sh"

delay="${NEXUS_PRESENCE_DELAY_SECS:-15}"
case "$delay" in ''|*[!0-9]*) delay=15 ;; esac   # non-numeric -> default, never error

log_dir="${HERDR_PLUGIN_STATE_DIR:-${TMPDIR:-/tmp}}"
pane="${HERDR_PANE_ID:-?}"

# --- Stage 2: settle, then re-read the pane's CURRENT status. ---
# delay=0 is the documented pre-2026-08-26 behavior: no wait, and no re-check
# either — there is nothing to settle, so the edge itself is the verdict.
if [ "$delay" -gt 0 ]; then
  sleep "$delay"

  # `herdr agent get <pane>` answers with the same `"agent_status":"..."` literal
  # the fast gate matches on, so the check is the identical pattern.
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

  # Fail OPEN, deliberately: if the status cannot be read (no herdr binary, no
  # pane id, API error) we notify rather than stay silent. A spurious ping is a
  # nuisance; a swallowed one strands an agent waiting on a human.
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

# --- Stage 3: coalesce bursts. ---
# A pane that flaps blocked->working->blocked (one edge per auto-approved tool
# call) spawns one worker per edge, and they can all find it blocked again by the
# time they wake. One toast per pane per cooldown is enough to say "this pane
# needs you". Independent of the settle window on purpose: delay=0 + an explicit
# cooldown is a valid combination (a box with no classifier that still does not
# want burst spam). Default cooldown = delay, so delay=0 alone stays a pure
# passthrough to the pre-2026-08-26 behavior.
#
# Stamp file only, no lock — a benign race costs a duplicate toast, whereas a
# stale lock would mute a pane indefinitely, which is the worse failure.
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

# Build a human message from the payload. python3 is the stack's lingua franca and
# runs only on the (infrequent) path that actually notifies — after the settle
# window and the cooldown have both cleared, never on the hot no-op gate.
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

# Breadcrumb (herdr's per-plugin state dir if it gave us one, else tmp).
{ printf '%s\t%s\tpane=%s ws=%s outcome=notified\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S')" "$msg" \
    "$pane" "${HERDR_WORKSPACE_ID:-?}" >> "$log_dir/presence.log"; } 2>/dev/null || true

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
