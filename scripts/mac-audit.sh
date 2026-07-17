#!/usr/bin/env bash
#
# Mac setup audit — confirms the live state of the agents-nexus stack on macOS
# matches what's supposed to be running: launchd jobs loaded, the Slack bridge
# healthy, the auto-approve classifier venv present, and smart-routing deps
# installed.
#
# Read-only. Exits non-zero if any REQUIRED check fails (optional jobs never
# fail the run). Run from anywhere:  bash scripts/mac-audit.sh   (or: task mac:audit)
#
set -u

NEXUS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"

# --- colors (off when not a tty) ---
if [ -t 1 ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[34m'; DIM=$'\e[2m'; X=$'\e[0m'
else
  R=; G=; Y=; B=; DIM=; X=
fi

ok=0; warn=0; fail=0
pass() { printf '  %s[ OK ]%s %-38s %s\n' "$G" "$X" "$1" "${2:-}"; ok=$((ok+1)); }
note() { printf '  %s[warn]%s %-38s %s\n' "$Y" "$X" "$1" "${2:-}"; warn=$((warn+1)); }
bad()  { printf '  %s[FAIL]%s %-38s %s\n' "$R" "$X" "$1" "${2:-}"; fail=$((fail+1)); }
opt()  { printf '  %s[ -- ]%s %-38s %s\n' "$DIM" "$X" "$1" "${DIM}${2:-}${X}"; }
hdr()  { printf '\n%s%s%s\n' "$B" "$1" "$X"; }

if [ "$(uname -s)" != "Darwin" ]; then
  echo "mac-audit: this audit targets macOS (launchd). Host is $(uname -s) — skipping." >&2
  exit 0
fi

printf '%sagents-nexus — Mac setup audit%s   %s%s%s\n' "$B" "$X" "$DIM" "$(date '+%Y-%m-%d %H:%M')" "$X"

# Extract the <key>Label</key> value from a plist (robust to filename drift).
plist_label() {
  awk '/<key>Label<\/key>/{getline; gsub(/^[[:space:]]*<string>|<\/string>[[:space:]]*$/,""); print; exit}' "$1"
}

# Is a launchd label currently loaded? Echo its LastExitStatus if so.
launchd_status() {
  local label="$1" line
  line=$(launchctl list 2>/dev/null | awk -v l="$label" '$3==l {print; found=1} END{exit !found}') || return 1
  # "LastExitStatus" = N;  (only present once it has run at least once)
  local code
  code=$(launchctl list "$label" 2>/dev/null | sed -n 's/.*"LastExitStatus" = \([0-9-]*\);.*/\1/p')
  echo "${code:-?}"
  return 0
}

audit_plist_dir() {  # $1 = dir, $2 = "required" | "optional"
  local dir="$1" kind="$2" f label basename code
  [ -d "$dir" ] || return 0
  for f in "$dir"/*.plist; do
    [ -e "$f" ] || continue
    basename="$(basename "$f")"
    label="$(plist_label "$f")"
    [ -n "$label" ] || label="${basename%.plist}"
    if code=$(launchd_status "$label"); then
      if [ "$code" = "0" ] || [ "$code" = "?" ]; then
        pass "$label" "loaded${code:+ (exit $code)}"
      else
        bad "$label" "loaded but LastExitStatus=$code — check logs"
      fi
    elif [ -f "$LA_DIR/$basename" ]; then
      note "$label" "installed, NOT loaded (launchctl bootout/disabled?)"
    else
      if [ "$kind" = optional ]; then
        opt "$label" "not installed (optional)"
      else
        note "$label" "not installed"
      fi
    fi
  done
}

hdr "launchd jobs — standard (launchd/)"
audit_plist_dir "$NEXUS_DIR/launchd" required

hdr "launchd jobs — optional / personal (opt-in, never fails the audit)"
audit_plist_dir "$NEXUS_DIR/launchd/optional" optional
audit_plist_dir "$NEXUS_DIR/launchd/personal" optional

# --- HTTP health probes ---
hdr "services"
BRIDGE_PORT="${SLACK_BRIDGE_PORT:-8788}"
health=$(curl -m 2 -s "http://127.0.0.1:${BRIDGE_PORT}/health" 2>/dev/null || true)
if [ -n "$health" ]; then
  threads=$(printf '%s' "$health" | sed -n 's/.*"threads":\([0-9]*\).*/\1/p')
  if printf '%s' "$health" | grep -q '"connected":true'; then
    pass "slack-bridge :$BRIDGE_PORT /health" "connected, threads=${threads:-0}"
  else
    note "slack-bridge :$BRIDGE_PORT /health" "up but Socket Mode NOT connected (token/socket issue)"
  fi
else
  bad "slack-bridge :$BRIDGE_PORT /health" "no response — bridge down (or tokens unset → exited 0)"
fi

probe() {  # $1 label, $2 url, $3 required|optional
  local code
  # curl prints the real code via -w even when it exits non-zero (e.g. an SSE
  # stream killed by -m); a true connection failure yields "000". So don't append
  # a fallback — just default an empty capture.
  code=$(curl -m 2 -s -o /dev/null -w '%{http_code}' "$2" 2>/dev/null)
  code=${code:-000}
  if [ "$code" != "000" ]; then
    pass "$1" "responding (HTTP $code)"
  elif [ "$3" = optional ]; then
    opt "$1" "no response (start the stack if you want it)"
  else
    note "$1" "no response"
  fi
}
probe "arbiter :8420"        "http://127.0.0.1:8420/"        optional
probe "dashboard :8421"      "http://127.0.0.1:8421/"        optional
probe "spark :8343"          "http://127.0.0.1:8343/sse"     optional
probe "ollama :11434"        "http://127.0.0.1:11434/"       optional

# --- dependencies the bridge/classifier quietly need ---
hdr "dependencies"
CLASSIFY_PY="$HOME/.tmux/.classify-venv/bin/python"
if [ -x "$CLASSIFY_PY" ]; then
  pass "auto-approve classifier venv" "~/.tmux/.classify-venv"
else
  bad "auto-approve classifier venv" "missing → EVERY permission prompt goes red + to Slack"
fi

if [ -d "$NEXUS_DIR/slack-bridge/node_modules/@anthropic-ai/sdk" ]; then
  pass "slack-bridge smart-routing dep" "@anthropic-ai/sdk installed"
else
  bad "slack-bridge smart-routing dep" "@anthropic-ai/sdk missing → unaddressed msgs only get a usage hint (run: task slack:bridge:install)"
fi

if [ -d "$NEXUS_DIR/slack-bridge/node_modules" ]; then
  pass "slack-bridge node_modules" "present"
else
  bad "slack-bridge node_modules" "missing → run: task slack:bridge:install"
fi

# --- bridge env wiring (does .env carry the Slack creds?) ---
ENV_FILE="$NEXUS_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  miss=""
  for v in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_NEXUS_CHANNEL; do
    grep -qE "^${v}=.+" "$ENV_FILE" || miss="$miss $v"
  done
  if [ -z "$miss" ]; then
    pass ".env Slack creds" "SLACK_BOT_TOKEN / SLACK_APP_TOKEN / SLACK_NEXUS_CHANNEL set"
  else
    note ".env Slack creds" "missing:$miss (bridge will no-op / only handle DMs)"
  fi
else
  note ".env" "not found at $ENV_FILE"
fi

# --- summary ---
printf '\n%s──────── summary ────────%s\n' "$B" "$X"
printf '  %s%d ok%s   %s%d warn%s   %s%d fail%s\n' \
  "$G" "$ok" "$X" "$Y" "$warn" "$X" "$R" "$fail" "$X"
if [ "$fail" -gt 0 ]; then
  printf '  %sfailures above are actionable — see the hint on each line.%s\n' "$R" "$X"
  exit 1
fi
exit 0
