#!/usr/bin/env bash
# nx-kv — read the fleet agent registry straight out of the NATS JetStream KV bucket.
#
#   nx-kv.sh              # or: keys — every live agent, as a paste-ready FQDN
#   nx-kv.sh hosts        # just the distinct hosts
#   nx-kv.sh raw          # the dot-encoded KV keys, unconverted
#   nx-kv.sh get <fqdn>   # one entry's value (see the note on scoped creds below)
#
# WHY THIS EXISTS. `curl -s localhost:8788/agents` is the everyday answer and is
# usually enough — but it is the BRIDGE's read of the registry. When the question is
# "is the bridge telling me the truth?", you need the bucket itself, with nothing of
# ours in between. This is that second opinion. It is also the answer when the local
# bridge is down: the registry is in the cloud, so it is still readable.
#
# CREDENTIALS. NATS_URL embeds user:pass. Neither this wrapper nor nx-kv.mjs ever
# prints it — output identifies the broker as `protocol//hostname:port` only. Agent
# transcripts are durable and cannot be un-written; keep it that way.
#
# `get` will often FAIL, and that is correct. The bridge's credentials are scoped to
# allow kv.keys() but deny kv.get(). Least privilege working as designed, not an
# outage. Presence and liveness are answerable from the key alone.

set -euo pipefail

SELF="${BASH_SOURCE[0]}"
while [ -L "$SELF" ]; do
  _t="$(readlink "$SELF")"
  case "$_t" in /*) SELF="$_t" ;; *) SELF="$(dirname "$SELF")/$_t" ;; esac
done
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"

# tmux/mac/tmux-scripts -> repo root
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
BRIDGE_DIR="$REPO_ROOT/slack-bridge"

if [ ! -d "$BRIDGE_DIR/node_modules/@nats-io" ]; then
  echo "nx-kv: NATS client not installed at $BRIDGE_DIR/node_modules/@nats-io" >&2
  echo "       run: ( cd '$BRIDGE_DIR' && npm install )" >&2
  exit 1
fi

# The broker credentials live in the bridge's environment and nowhere else readable.
# If this shell doesn't have them, hand the .mjs the bridge's pid so it can pull the
# NATS_* names out of /proc/<pid>/environ. That is a targeted lookup of three
# variables, not a dump — and the values are used, never printed.
if [ -z "${NATS_URL:-}" ]; then
  pid="$(systemctl --user show slack-bridge -p MainPID --value 2>/dev/null || true)"
  [ "${pid:-0}" = "0" ] && pid=""
  [ -z "$pid" ] && pid="$(pgrep -f 'slack-bridge/index.js' 2>/dev/null | head -1 || true)"
  export NX_BRIDGE_PID="${pid:-}"
fi

# The .mjs lives in slack-bridge/, NOT next to this wrapper, and that is deliberate.
# Node resolves a bare ESM import ("@nats-io/kv") by walking up from the IMPORTING FILE,
# never from cwd — and it ignores NODE_PATH entirely, which is a CommonJS-only mechanism.
# A copy in tmux-scripts/ therefore searches tmux/*/node_modules and dies with
# ERR_MODULE_NOT_FOUND no matter what the environment says. Putting it in the tree that
# owns the dependency is the fix; the wrapper stays here with its siblings.
#
# NATS 4222 client of mqtt.flashbackfleet.com -- trust the internal Flashback CA the
# same way slack-bridge.service's unit file does (true append to Node's default CA
# store, never a replacement). Unlike the bridge, this is a one-shot process invoked
# from an operator's own shell, which never has this set on its own -- without it,
# every run fails "unable to get local issuer certificate" regardless of what the
# bridge's trust looks like. Only set if the caller hasn't already provided one.
: "${NODE_EXTRA_CA_CERTS:=$BRIDGE_DIR/certs/flashback-root.crt}"
export NODE_EXTRA_CA_CERTS
exec node "$BRIDGE_DIR/nx-kv.mjs" "$@"
