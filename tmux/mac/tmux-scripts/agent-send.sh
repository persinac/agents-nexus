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
#       * SLACK_A2A_SAMEHOST=channel + bus on -> route through #nexus-agents so the
#         exchange is buffered + idle-gated + audited. A slot/%pane target is
#         reverse-resolved to its agent NAME first (the bus keys on name), so ALL
#         addressing of a registered agent goes through Slack. Falls back to local
#         send-keys if the bus is unreachable. Two things still stay local: a bare
#         control digit (idle-gating a permission-menu input would deadlock it) and
#         a window with no registered agent (no name to route by).
#   - --via-slack forces the bus path (for a name); --local forces send-keys.
#   - A namespaced NAME `host/name` is inherently cross-host (names a specific
#     bridge) → always routes through the bus; errors if the bus is off.
#   - --relay posts <message> to #nexus-agents for a HUMAN to read (no target,
#     no delivery) — share your output instead of copy-pasting it into Slack.
#
# Set SLACK_A2A_SAMEHOST in the AGENT shell env (~/.tmux/env.sh) — NOT in the
# bridge's env — so the bridge's own deliveries stay local and never loop.
#
# Usage: agent-send.sh [--via-slack|--local] <slot_or_name_or_%pane|host/name> <message>
#        agent-send.sh --relay <message>
# Accepts a pane id (%NN, exact), a slot number (window index), an agent name, or
# a namespaced `host/name` for a specific remote bridge.

VIA_SLACK=0
FORCE_LOCAL=0
RELAY=0
KIND=""       # typed-envelope kind (Phase B): request | reply | event ; empty = msg (unchanged)
CORR=""       # correlation id for --reply (the request's id)
REPLY_TO=""   # override where a reply should be addressed
# Parse leading flags. The typed verbs (--request/--reply/--event) build a Phase-B envelope
# via the bridge and therefore force the bus path; a bare `agent-send.sh <to> <msg>` is a
# plain `msg`, unchanged. --reply takes the correlation id; --reply-to takes an address.
while true; do
  case "$1" in
    --via-slack) VIA_SLACK=1; shift ;;
    --local)     FORCE_LOCAL=1; shift ;;
    --relay)     RELAY=1; shift ;;
    --request)   KIND="request"; shift ;;
    --reply)     KIND="reply"; CORR="${2:?"--reply needs a correlation id: agent-send.sh --reply <id> <to> <msg>"}"; shift 2 ;;
    --event)     KIND="event"; shift ;;
    --reply-to)  REPLY_TO="${2:?"--reply-to needs an address"}"; shift 2 ;;
    *) break ;;
  esac
done

# --relay has NO target: the whole remainder is text posted to #nexus-agents for
# a HUMAN to read (share your output instead of copy-pasting it into a Slack DM).
if [ "$RELAY" = "1" ]; then
  TARGET=""
  MSG="$*"
else
  TARGET="${1:?"Usage: agent-send.sh [--via-slack|--local|--relay] <slot_or_name> <message>"}"
  shift
  MSG="$*"
fi
[ -z "$MSG" ] && { echo "No message provided"; exit 1; }

SESSION="${TMUX_AGENT_SESSION:-agents}"
REGISTRY_DIR="$HOME/.tmux/registry"
BRIDGE_PORT="${SLACK_BRIDGE_PORT:-8788}"
BUS_ENABLED="${SLACK_BUS_ENABLED:-0}"
# nx-resolve: the shared address grammar (workspace/host parsing + workspace scoping).
[ -f "$HOME/.tmux/agent-resolve.sh" ] && . "$HOME/.tmux/agent-resolve.sh"
# Same-host routing: 'local' (fast send-keys, default) or 'channel' (route NAME
# targets through #nexus-agents for visibility, with a local fallback).
SAMEHOST_MODE="${SLACK_A2A_SAMEHOST:-local}"
# Emit a one-line stderr nudge when a message to a real agent goes LOCAL only
# because same-host channel routing is off (the launch-caveat trap). The bridge
# sets SLACK_A2A_NUDGE=0 on its own deliveries (which are intentionally local).
NUDGE="${SLACK_A2A_NUDGE:-1}"

# Flatten to single line — newlines break both send-keys and the /send JSON
# payload. Skipped for --relay: a relay is human-facing prose posted to Slack
# (not injected via send-keys), so its multi-line shape is preserved as-is.
if [ "$RELAY" != "1" ]; then
  MSG=$(printf '%s' "$MSG" | tr '\n' ' ' | sed 's/  */ /g; s/^ *//; s/ *$//')
fi

# Resolve our own identity for the bus `from` tag: an explicit AGENT_FROM, else the
# per-agent PROJECT_SLUG, else our pane's registry NAME. Fold in HERDR_PANE_ID — herdr
# agents set THAT (not TMUX_PANE), so without the fold the registry lookup below never ran
# in herdr mode and any pane-backed sender lacking PROJECT_SLUG fell straight to "unknown"
# (the F4HFKXH56W/unknown on the bus). Same fold the hooks already do.
SELF_PANE="${TMUX_PANE:-${HERDR_PANE_ID:-}}"
FROM="${AGENT_FROM:-${PROJECT_SLUG:-}}"
if [ -z "$FROM" ] && [ -n "$SELF_PANE" ]; then
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    if [ "$(grep '^PANE_ID=' "$f" | cut -d= -f2)" = "$SELF_PANE" ]; then
      FROM="$(grep '^NAME=' "$f" | cut -d= -f2)"; break
    fi
  done
fi
# Still unnamed but we DO have a live pane → register it now. Everything in the fleet gets a
# registry entry, ephemeral or not: an agent that can talk on the bus must be nameable +
# addressable back. Name = herdr's own agent label, else the cwd basename. The entry is
# self-cleaning — dead panes are lazy-pruned by the name resolver below + the reaper.
if [ -z "$FROM" ] && [ -n "$SELF_PANE" ] && "$HOME/.tmux/substrate.sh" pane-alive "$SELF_PANE" 2>/dev/null; then
  _ecwd="$("$HOME/.tmux/substrate.sh" pane-field "$SELF_PANE" '#{pane_current_path}' 2>/dev/null)"
  _ename="$("$HOME/.tmux/substrate.sh" pane-field "$SELF_PANE" '#W' 2>/dev/null)"
  case "$_ename" in ''|claude|-zsh|zsh|bash|-bash) _ename="$(basename "${_ecwd:-ephemeral}")" ;; esac
  if [ -n "$_ename" ]; then
    "$HOME/.tmux/substrate.sh" register "$SELF_PANE" "$_ename" "${_ecwd:-$PWD}" 2>/dev/null && FROM="$_ename"
  fi
fi
FROM="${FROM:-unknown}"

# Route a message to the bridge bus. Returns 0 on a 200, non-zero otherwise.
route_via_bus() {
  local to="$1" payload http
  # Include the typed-envelope fields when set (Phase B). Absent → the bridge treats it as a
  # plain `msg`, so a bare send is byte-for-byte unchanged. Optional keys are omitted, not null.
  payload=$(TO="$to" FROM="$FROM" MSG="$MSG" KIND="$KIND" CORR="$CORR" REPLY_TO="$REPLY_TO" python3 -c \
    'import json,os
d={"to":os.environ["TO"],"from":os.environ["FROM"],"msg":os.environ["MSG"]}
for k in ("kind","corr","reply_to"):
    v=os.environ.get(k.upper() if k!="reply_to" else "REPLY_TO","")
    if v: d[k]=v
print(json.dumps(d))') \
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

# Relay MSG to #nexus-agents for a human to read (POST /relay). No target, no
# delivery — just posts, sender-tagged. Returns 0 on a 200, non-zero otherwise.
route_via_relay() {
  local payload http
  payload=$(FROM="$FROM" MSG="$MSG" python3 -c \
    'import json,os;print(json.dumps({"from":os.environ["FROM"],"text":os.environ["MSG"]}))') \
    || { echo "relay: could not encode payload"; return 2; }
  http=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -X POST \
         -H 'content-type: application/json' -d "$payload" \
         "http://127.0.0.1:${BRIDGE_PORT}/relay" 2>/dev/null) \
    || { echo "relay: bridge unreachable on :${BRIDGE_PORT}"; return 2; }
  if [ "$http" = "200" ]; then
    echo "Relayed to #nexus-agents (from ${FROM})"; return 0
  fi
  echo "relay: /relay returned HTTP ${http} (is SLACK_BUS_ENABLED=1 on the bridge?)"; return 2
}

# Local delivery. $1 = DEST.
#   - SDK-runner agents (registry has INBOX=<path>) receive via their inbox file:
#     append a framed JSON record; the runner consumes it at its next turn boundary
#     (idle-gated by construction — no send-keys, no @waiting, no settle delay).
#   - CLI/TUI agents: tmux send-keys. A lone digit is a permission-menu nav (no
#     Enter); everything else is typed literally, then Enter after a settle delay
#     (the TUI debounces an Enter arriving in the same render tick as the paste).
deliver_local() {
  local dest="$1" regf="" inbox=""
  regf="$(resolve_registry_file "$dest" "$TARGET")"
  [ -n "$regf" ] && inbox="$(grep '^INBOX=' "$regf" | cut -d= -f2-)"

  if [ -n "$inbox" ]; then
    # A bare control digit is TUI-menu navigation — meaningless to an SDK agent
    # (it self-gates via can_use_tool), so drop it rather than inject noise.
    if [[ "$MSG" =~ ^[0-9]$ ]]; then
      echo "Skipped control digit '$MSG' for SDK agent ${TARGET} (self-gates via can_use_tool)"; return 0
    fi
    mkdir -p "$(dirname "$inbox")"
    local rec
    rec=$(FROM="$FROM" MSG="$MSG" python3 -c \
      'import json,os,time;print(json.dumps({"from":os.environ["FROM"],"text":os.environ["MSG"],"ts":time.time()}))') \
      || { echo "inbox: could not encode record for ${TARGET}"; return 1; }
    printf '%s\n' "$rec" >> "$inbox" || { echo "inbox: could not write ${inbox}"; return 1; }
    echo "Delivered to SDK agent ${TARGET} inbox: ${MSG}"
    return 0
  fi

  # Deliver through the substrate seam: tmux send-keys today (literal paste + settle
  # delay + Enter; a bare digit = no Enter), or herdr send-text+enter under
  # NEXUS_SUBSTRATE=herdr. substrate.sh `send` replicates the exact TUI settle-delay
  # dance (the Enter-coalescing fix) that used to live here.
  "$HOME/.tmux/substrate.sh" send "$dest" "$MSG"
  echo "Sent to ${TARGET} (${dest}): ${MSG}"
}

# Reverse-resolve a local DEST (a slot's window or a %pane) to its registry agent
# NAME, so a slot/%pane-addressed message can round-trip the name-keyed bus too.
# Prints the NAME, or nothing if the pane has no registered agent (a raw window).
resolve_pane_name() {
  local dest="$1" target="$2" pane=""
  if [[ "$target" =~ ^%[0-9]+$ ]]; then pane="$target"; else pane=$(tmux display-message -t "$dest" -p '#{pane_id}' 2>/dev/null); fi
  [ -n "$pane" ] || return 0
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    if [ "$(grep '^PANE_ID=' "$f" | cut -d= -f2)" = "$pane" ]; then
      grep '^NAME=' "$f" | cut -d= -f2; return 0
    fi
  done
}

# Resolve a local DEST (a slot's window or a %pane) to the registry FILE backing it.
# Prints the file path, or nothing. Used to read the agent's runtime/inbox fields.
resolve_registry_file() {
  local dest="$1" target="$2" pane=""
  if [[ "$target" =~ ^%[0-9]+$ ]]; then pane="$target"; else pane=$(tmux display-message -t "$dest" -p '#{pane_id}' 2>/dev/null); fi
  [ -n "$pane" ] || return 0
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    if [ "$(grep '^PANE_ID=' "$f" | cut -d= -f2)" = "$pane" ]; then echo "$f"; return 0; fi
  done
}

# --relay posts to the channel for a human to read; it never delivers to an
# agent, so it short-circuits all target resolution. Bus-only (cross-machine).
if [ "$RELAY" = "1" ]; then
  if [ "$BUS_ENABLED" != "1" ]; then
    echo "Relay needs the bus (set SLACK_BUS_ENABLED=1); it posts to #nexus-agents." >&2
    exit 1
  fi
  route_via_relay; exit $?
fi

# --via-slack, OR a typed kind (--request/--reply/--event), forces the bus regardless of
# locality — a typed envelope (id + correlation) is built by the bridge, so it can't take the
# local send-keys fast path. The owning host delivers. Requires the bus enabled.
if [ "$VIA_SLACK" = "1" ] || [ -n "$KIND" ]; then
  if [ -n "$KIND" ] && [ "$BUS_ENABLED" != "1" ]; then
    echo "--${KIND} needs the bus (set SLACK_BUS_ENABLED=1); typed A2A is bus-only." >&2
    exit 1
  fi
  route_via_bus "$TARGET"; exit $?
fi

# A '/'-qualified target is either cross-host (host/name) or workspace-scoped
# (workspace/name). Parse it right-to-left with a known-host test (nx-resolve):
#   - first segment is a KNOWN host  → cross-host, bus-only (a local send-keys can't
#     cross machines); honest error if the bus is off. Bare host/name is unchanged.
#   - otherwise the prefix is a workspace label → resolve the bare name LOCALLY, scoped
#     to that bucket (WS_FILTER). This is the new workspace addressing.
# If the resolver lib is somehow unavailable, fall back to the legacy bus-only routing.
WS_FILTER=""
if [[ "$TARGET" == */* ]]; then
  _bus_only_qualified() {
    if [ "$BUS_ENABLED" != "1" ]; then
      echo "Namespaced target '$TARGET' needs the bus (set SLACK_BUS_ENABLED=1); cross-host delivery is bus-only." >&2
      exit 1
    fi
    route_via_bus "$TARGET"; exit $?
  }
  if command -v nx_parse_addr >/dev/null 2>&1; then
    nx_parse_addr "$TARGET"
    if [ -n "$NX_HOST" ]; then
      _bus_only_qualified
    else
      WS_FILTER="$NX_WS"; TARGET="$NX_NAME"   # workspace/name → local, scoped
    fi
  else
    _bus_only_qualified
  fi
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
elif [[ "$TARGET" =~ ^w[A-Za-z0-9]+:p ]]; then
  # herdr pane handle (wN:pN — real ids use letters, e.g. wB:p1) — a direct, exact target.
  DEST="$TARGET"
else
  # A NAME → resolve against the local registry ("thin → FQDN → scoped"). Collect all LIVE
  # NAME matches (honoring an explicit workspace filter): 1 match → use it; many → prefer the
  # sender's own workspace; still ambiguous → error asking to qualify as workspace/name.
  TARGET_IS_NAME=1
  cand_panes=(); cand_ws=()
  for f in "$REGISTRY_DIR"/*; do
    [ -f "$f" ] || continue
    name=$(grep '^NAME=' "$f" 2>/dev/null | head -1 | cut -d= -f2)
    [ "$name" = "$TARGET" ] || continue
    entry_ws=$(grep '^WORKSPACE=' "$f" 2>/dev/null | head -1 | cut -d= -f2-)
    if [ -n "$WS_FILTER" ]; then
      if command -v nx_match_ws >/dev/null 2>&1; then
        nx_match_ws "$entry_ws" "$WS_FILTER" || continue
      else
        [ "$entry_ws" = "$WS_FILTER" ] || [ "${entry_ws##*/}" = "${WS_FILTER##*/}" ] || continue
      fi
    fi
    pane_id=$(grep '^PANE_ID=' "$f" 2>/dev/null | head -1 | cut -d= -f2)
    # herdr-aware liveness: drop dead entries so they neither misdeliver nor inflate ambiguity.
    if ! "$HOME/.tmux/substrate.sh" pane-alive "$pane_id" 2>/dev/null; then rm -f "$f"; continue; fi
    cand_panes+=("$pane_id"); cand_ws+=("$entry_ws")
  done

  chosen=""
  if [ "${#cand_panes[@]}" -eq 1 ]; then
    chosen="${cand_panes[0]}"
  elif [ "${#cand_panes[@]}" -gt 1 ]; then
    # ambiguous bare name across buckets — prefer the sender's OWN workspace, else ask to qualify.
    self_ws=""; command -v nx_self_ws >/dev/null 2>&1 && self_ws="$(nx_self_ws)"
    if [ -n "$self_ws" ] && command -v nx_match_ws >/dev/null 2>&1; then
      i=0; hits=0; pick=""
      while [ "$i" -lt "${#cand_panes[@]}" ]; do
        nx_match_ws "${cand_ws[$i]}" "$self_ws" && { pick="${cand_panes[$i]}"; hits=$((hits+1)); }
        i=$((i+1))
      done
      [ "$hits" -eq 1 ] && chosen="$pick"
    fi
    if [ -z "$chosen" ]; then
      echo "Ambiguous agent '$TARGET' across workspaces — qualify as workspace/name:" >&2
      i=0; while [ "$i" -lt "${#cand_ws[@]}" ]; do echo "  ${cand_ws[$i]:-flat}/$TARGET" >&2; i=$((i+1)); done
      exit 1
    fi
  fi

  if [ -n "$chosen" ]; then
    # herdr: the pane handle IS the address (no session:slot); tmux: resolve the live slot.
    if [ "${NEXUS_SUBSTRATE:-herdr}" = "herdr" ]; then
      DEST="$chosen"
    else
      slot=$(tmux display-message -t "$chosen" -p '#{window_index}' 2>/dev/null)
      [ -n "$slot" ] && DEST="${SESSION}:${slot}"
    fi
  fi
fi

# --local forces the fast path (errors if the target is not local).
if [ "$FORCE_LOCAL" = "1" ]; then
  [ -n "$DEST" ] && { deliver_local "$DEST"; exit 0; }
  echo "Not a local target: $TARGET"; exit 1
fi

# Local target.
if [ -n "$DEST" ]; then
  # Channel mode: route through the bus so the exchange is buffered + audited in
  # #nexus-agents. A NAME target routes as-is; a SLOT/%pane target is reverse-
  # resolved to its agent NAME so it round-trips too (the bus keys on name) — so
  # ALL addressing of a registered agent goes through Slack. Two things stay local:
  # a bare control digit (idle-gating a permission-menu input would deadlock the
  # prompt), and a window with no registered agent (no name to route by).
  if [ "$BUS_ENABLED" = "1" ] && [ "$SAMEHOST_MODE" = "channel" ] && ! [[ "$MSG" =~ ^[0-9]$ ]]; then
    route_name="$TARGET"
    [ "$TARGET_IS_NAME" = "1" ] || route_name="$(resolve_pane_name "$DEST" "$TARGET")"
    if [ -n "$route_name" ]; then
      route_via_bus "$route_name" && exit 0
      echo "bus: unreachable — delivering locally instead"
    fi
  fi
  # Launch-caveat nudge: the bus is on and this targets a real agent, but it went
  # local because same-host routing is off — so it did NOT post to #nexus-agents.
  # (Skipped for digits/unregistered windows, which stay local by design, and for
  # the bridge's own deliveries via SLACK_A2A_NUDGE=0.)
  if [ "$NUDGE" = "1" ] && [ "$BUS_ENABLED" = "1" ] && [ "$SAMEHOST_MODE" != "channel" ] \
     && [ "$FORCE_LOCAL" != "1" ] && ! [[ "$MSG" =~ ^[0-9]$ ]]; then
    rn="$TARGET"; [ "$TARGET_IS_NAME" = "1" ] || rn="$(resolve_pane_name "$DEST" "$TARGET")"
    [ -n "$rn" ] && echo "note: delivered locally — SLACK_A2A_SAMEHOST≠channel, so this did NOT post to #nexus-agents. Set it to 'channel' (and relaunch this agent) for channel routing." >&2
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
