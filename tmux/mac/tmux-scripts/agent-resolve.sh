#!/usr/bin/env bash
# nx-resolve — the shared agent-address resolver (bash half; JS half is
# parseAddress()/workspaceMatches() in slack-bridge/orchestrator.js).
#
# SOURCE this file; do not exec it:  . "$NEXUS_TMUX_DIR/agent-resolve.sh"
#
# Address grammar ("thin -> FQDN -> scoped"), parsed RIGHT-TO-LEFT:
#   name                      -> just an agent name (thin)
#   ws/name                   -> workspace-scoped
#   cat/slug/name             -> workspace label is `cat/slug` (labels contain '/')
#   host/name                 -> host ONLY if `host` is a KNOWN host (else it's a workspace)
#   host/ws/name              -> explicit cross-host + workspace
# The LAST segment is always the name; the FIRST segment is a host only when
# nx_known_host says so — otherwise the whole prefix is the workspace label.
# This coexists with the legacy `host/name` cross-PC scheme (a real host is
# always known) without new sigils; single-PC users never type a host.

# Guard against double-sourcing.
[ -n "${_NX_RESOLVE_SOURCED:-}" ] && return 0 2>/dev/null || true
_NX_RESOLVE_SOURCED=1

# Fleet install root. Self-default so nx_self_ws resolves the registry even when a
# sourcing script hasn't set it (env.sh normally exports the canonical value).
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"

_nx_lc() { printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'; }

# This machine's presence host: explicit override, else the short hostname.
nx_self_host() { printf '%s' "${SLACK_PRESENCE_HOST:-$(hostname -s 2>/dev/null)}"; }

# Cached list of KNOWN remote hosts from the bridge's /agents (self + peers).
# Only consulted when presence is enabled; cached ~5s to avoid per-call curls.
_nx_known_hosts_cached() {
  local cache="${TMPDIR:-/tmp}/nx-known-hosts.${UID:-$(id -u)}"
  local port="${SLACK_BRIDGE_PORT:-8788}" age=999 mtime now
  if [ -f "$cache" ]; then
    mtime=$(stat -f %m "$cache" 2>/dev/null || stat -c %Y "$cache" 2>/dev/null || echo 0)
    now=$(date +%s); age=$(( now - mtime ))
  fi
  if [ "$age" -lt 5 ]; then cat "$cache" 2>/dev/null; return 0; fi
  local out
  out="$(curl -sf -m 1 "http://127.0.0.1:${port}/agents" 2>/dev/null)" || { : >"$cache" 2>/dev/null; return 0; }
  printf '%s' "$out" | python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
hosts = set()
if isinstance(d, dict):
    if d.get("self"): hosts.add(str(d["self"]))
    ags = d.get("agents") or []
    if isinstance(ags, list):
        for a in ags:
            if isinstance(a, dict) and a.get("host"): hosts.add(str(a["host"]))
    elif isinstance(ags, dict):
        for h in ags: hosts.add(str(h))
for h in sorted(hosts): print(h)
' >"$cache" 2>/dev/null
  cat "$cache" 2>/dev/null
}

# nx_known_host <segment> -> 0 if it names a host we know, else 1.
# SELF_HOST is always known (no network). Remote hosts exist only with presence
# on; with presence off, the only known host is self.
nx_known_host() {
  local seg="${1:-}" self
  [ -n "$seg" ] || return 1
  self="$(nx_self_host)"
  [ "$(_nx_lc "$seg")" = "$(_nx_lc "$self")" ] && return 0
  [ "${SLACK_PRESENCE_ENABLED:-0}" = "1" ] || return 1
  _nx_known_hosts_cached | grep -qix -- "$seg" && return 0
  return 1
}

# nx_parse_addr <target> -> sets globals NX_HOST, NX_WS, NX_NAME.
nx_parse_addr() {
  local t="${1:-}"
  NX_HOST=""; NX_WS=""; NX_NAME="$t"
  case "$t" in */*) : ;; *) return 0 ;; esac    # no '/': thin name, done
  NX_NAME="${t##*/}"                             # last segment = name
  local prefix="${t%/*}"                         # everything before the last '/'
  local first="${prefix%%/*}"                    # first segment of the prefix
  if nx_known_host "$first"; then
    NX_HOST="$first"
    case "$prefix" in */*) NX_WS="${prefix#*/}" ;; *) NX_WS="" ;; esac
  else
    NX_WS="$prefix"
  fi
  return 0
}

# nx_match_ws <entry_ws> <want> -> 0 if the registry WORKSPACE value satisfies
# the address's workspace token. Empty want = no filter (matches). Two-tier:
# exact full-label match, else slug (leaf after last '/') match.
nx_match_ws() {
  local ew="${1:-}" want="${2:-}"
  [ -z "$want" ] && return 0
  [ "$ew" = "$want" ] && return 0
  [ "${ew##*/}" = "${want##*/}" ] && return 0
  return 1
}

# nx_entry_ws <registry_file> -> print its WORKSPACE= value (empty if none).
nx_entry_ws() { grep '^WORKSPACE=' "${1:-/dev/null}" 2>/dev/null | head -1 | cut -d= -f2-; }

# nx_self_ws -> print THIS agent's own workspace label (from its registry entry),
# used for self-scope preference when resolving a bare name.
nx_self_ws() {
  local pane="${TMUX_PANE:-${HERDR_PANE_ID:-}}" rf
  [ -n "$pane" ] || return 0
  rf="$NEXUS_TMUX_DIR/registry/$pane"
  [ -f "$rf" ] && nx_entry_ws "$rf"
}
