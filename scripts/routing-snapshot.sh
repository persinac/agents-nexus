#!/usr/bin/env bash
# Nightly snapshot of the nexus-proxy routing calibration report.
# Writes $STATE_DIR/YYYY-MM-DD.md (+ latest.md), pruning past 90 days, so routing
# calibration has a history that outlives Langfuse's trace TTL. Driven by the
# routing-report-snapshot.timer systemd unit; also runnable by hand.
set -euo pipefail

REPO="${AGENTS_NEXUS_DIR:-$HOME/repos/agents-nexus}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agents-nexus/routing"
WINDOW="${ROUTING_REPORT_WINDOW:-24 HOUR}"

mkdir -p "$STATE_DIR"
day="$(date -u +%F)"
out="$STATE_DIR/$day.md"

{
  echo "# nexus-proxy routing snapshot — $(date -u +'%Y-%m-%d %H:%M UTC') (window: $WINDOW)"
  echo '```'
  python3 "$REPO/scripts/routing-report.py" "$WINDOW" 2>&1
  echo '```'
} > "$out"

cp -f "$out" "$STATE_DIR/latest.md"
echo "wrote $out"

# keep ~3 months of daily snapshots
find "$STATE_DIR" -maxdepth 1 -name '20*.md' -mtime +90 -delete 2>/dev/null || true
