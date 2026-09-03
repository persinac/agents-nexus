#!/usr/bin/env bash
# Re-read agents-nexus plists after editing them; launchd caches at load time.
set -uo pipefail

DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
ok=0
fail=0

for plist in "$AGENTS"/com.agents-nexus.*.plist; do
  label="$(basename "$plist" .plist)"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null
  if launchctl bootstrap "$DOMAIN" "$plist" 2>/dev/null; then
    printf '  reloaded  %s\n' "$label"
    ok=$((ok + 1))
  else
    printf '  FAILED    %s\n' "$label"
    fail=$((fail + 1))
  fi
done

printf '\nreloaded %d, failed %d\n' "$ok" "$fail"
