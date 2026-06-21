#!/usr/bin/env bash
# Send a message from one agent to another.
#
# Dual-mode, with a configurable same-host default:
#   - A NAME target that is NOT in THIS host's registry -> route through the Slack
#     bridge bus (POST :8788/send), so agents on other hosts are reachable.
#     Requires the bus enabled (SLACK_BUS_ENABLED=1); else the old "Agent not
#     found" behavior, with no network call.
#   - A LOCAL target (a %pane, a slot number, or a name in this host's registry):
#       * default (SLACK_A2A_SAMEHOST=local) -> tmux send-keys, instant, no network.
#       * SLACK_A2A_SAMEHOST=channel + bus on -> route a NAME target through
#         #nexus-agents too, so same-host A2A is visible/audited in the channel,
#         falling back to local send-keys if the bus is unreachable. %pane / slot
#         targets and bare control digits always stay local (they cannot round-trip
#         through the name-keyed bus, and a lone digit is a menu input).
#   - --via-slack forces the bus path (for a name); --local forces send-keys.
#
# Set SLACK_A2A_SAMEHOST in the AGENT shell env (~/.tmux/env.sh) — NOT in the
# bridge's env — so the bridge's own deliveries stay local and never loop.
#
# Usage: agent-send.sh [--via-slack|--local] <slot_or_name_or_%pane> <message>
# Accepts a pane id (%NN, exact), a slot number (window index), or an agent name.

VIA_SLACK=0
FORCE_LOCAL=0
case "$1" in
  --via-slack) VIA_SLACK=1; shift ;;
  --local)     FORCE_LOCAL=1; shift ;;
esac

TARGET="${1:?"Usage: agent-send.sh [--via-slack|--local] <slot_or_name> <message>"}"
shift
MSG="$*"
[ -z "$MSG" ] && { echo "No message provided"; exit 1; }

SESSION="${TMUX_AGENT_SESSION:-agents}"
REGISTRY_DIR="$HOME/.tmux/registry"
BRIDGE_PORT="${SLACK_BRIDGE_PORT:-8788}"
BUS_ENABLED="${SLACK_BUS_ENABLED:-0}"
# Same-host routing: 'local' (fast send-keys, default) or 'channel' (route NAME
# targets through #nexus-agents for visibility, with a local fallback).
SAMEHOST_MODE="${SLACK_A2A_SAMEHOST:-local}"

# Flatten to single line — newlines break both send-keys and the JSON payload.
MSG=$(printf '%s' "$MSG" | tr '\n' ' ' | sed 's/  */ /g; s/^ *//; s/ *$//')

# Resolve our own identity for the bus `from` tag (best-effort): an explicit
# AGENT_FROM, else the per-agent PROJECT_SLUG, else our pane's registry NAME.
FROM="${AGENT_FROM:-${PROJECT_SLUG:-}}"
if [ -z "$FROM" ] && [ -n "$TMUX_PANE" ]; then
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    if [ "$(grep '^PANE_ID=' "$f" | cut -d= -f2)" = "$TMUX_PANE" ]; then
      FROM="$(grep '^NAME=' "$f" | cut -d= -f2)"; break
    fi
  done
fi
FROM="${FROM:-unknown}"

# Route a message to the bridge bus. Returns 0 on a 200, non-zero otherwise.
route_via_bus() {
  local to="$1" payload http
  payload=$(TO="$to" FROM="$FROM" MSG="$MSG" python3 -c \
    'import json,os;print(json.dumps({"to":os.environ["TO"],"from":os.environ["FROM"],"msg":os.environ["MSG"]}))') \
    || { echo "bus: could not encode payload"; return 2; }
  http=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -X POST \
         -H 'content-type: application/json' -d "$payload" \
         "http://127.0.0.1:${BRIDGE_PORT}/send" 2>/dev/null) \
    || { echo "bus: bridge unreachable on :${BRIDGE_PORT}"; return 2; }
  if [ "$http" = "200" ]; then
    echo "Sent to ${to} via bus (from ${FROM}): ${MSG}"; return 0
  fi
  echo "bus: /send returned HTTP ${http} (is SLACK_BUS_ENABLED=1 on the bridge?)"; return 2
}

# Local delivery via tmux send-keys. $1 = DEST. A lone digit is sent without
# Enter (permission-menu navigation); everything else is typed literally + Enter.
deliver_local() {
  local dest="$1"
  if [[ "$MSG" =~ ^[0-9]$ ]]; then
    tmux send-keys -t "$dest" "$MSG"
  else
    tmux send-keys -l -t "$dest" "$MSG"
    tmux send-keys -t "$dest" Enter
  fi
  echo "Sent to ${TARGET} (${dest}): ${MSG}"
}

# --via-slack forces the bus regardless of locality (the owning host delivers).
if [ "$VIA_SLACK" = "1" ]; then
  route_via_bus "$TARGET"; exit $?
fi

# Resolve the target to a LOCAL send-keys destination (DEST); track whether it
# was addressed by NAME (only names can round-trip through the name-keyed bus).
DEST=""; TARGET_IS_NAME=0
if [[ "$TARGET" =~ ^%[0-9]+$ ]]; then
  # Pane id — inherently local and exact.
  if tmux list-panes -a -F '#{pane_id}' 2>/dev/null | grep -qx "$TARGET"; then
    DEST="$TARGET"
  else
    echo "Pane not found: $TARGET"; exit 1
  fi
elif [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  # Window index — inherently local.
  DEST="${SESSION}:${TARGET}"
else
  # A name — resolve against the local registry to its live window index.
  TARGET_IS_NAME=1
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    name=$(grep '^NAME=' "$f" | cut -d= -f2)
    pane_id=$(grep '^PANE_ID=' "$f" | cut -d= -f2)
    if [ "$name" = "$TARGET" ]; then
      slot=$(tmux display-message -t "$pane_id" -p '#{window_index}' 2>/dev/null)
      [ -n "$slot" ] && { DEST="${SESSION}:${slot}"; break; }
      rm -f "$f"   # stale registry entry (pane gone)
    fi
  done
fi

# --local forces the fast path (errors if the target is not local).
if [ "$FORCE_LOCAL" = "1" ]; then
  [ -n "$DEST" ] && { deliver_local "$DEST"; exit 0; }
  echo "Not a local target: $TARGET"; exit 1
fi

# Local target.
if [ -n "$DEST" ]; then
  # Channel mode: route a NAME target through the bus for visibility, with a
  # fallback to local delivery if the bus is unreachable. %pane / slot targets and
  # bare control digits stay local (they can't round-trip the name-keyed bus).
  if [ "$BUS_ENABLED" = "1" ] && [ "$SAMEHOST_MODE" = "channel" ] \
     && [ "$TARGET_IS_NAME" = "1" ] && ! [[ "$MSG" =~ ^[0-9]$ ]]; then
    route_via_bus "$TARGET" && exit 0
    echo "bus: unreachable — delivering locally instead"
  fi
  deliver_local "$DEST"; exit 0
fi

# Not local. Route through the bus if it's enabled; else preserve the old error.
if [ "$BUS_ENABLED" = "1" ]; then
  route_via_bus "$TARGET" && exit 0
  exit 1
fi
echo "Agent not found: $TARGET"
exit 1
