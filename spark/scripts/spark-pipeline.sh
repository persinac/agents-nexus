#!/usr/bin/env bash
# Run the full Guilty Spark nightly pipeline:
#   1. spark reclaim   — full index rebuild across all repos
#   2. spark synthesize — synthesize decision records from recent MRs
#
# Usage:
#   ./scripts/spark-pipeline.sh            # run with default 2-day lookback
#   ./scripts/spark-pipeline.sh --days 7   # longer lookback for synthesize
#
# Logs to ./logs/pipeline.log when run via launchd.
# When run manually, output goes to stdout.

set -euo pipefail

SPARK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DAYS="${1:-2}"  # default 2-day overlap so nightly misses are covered

# Load environment variables from .env
set -a
[ -f "$SPARK_DIR/.env" ] && source "$SPARK_DIR/.env"
set +a

# Use the project venv directly — no PATH guessing needed in cron/launchd
SPARK_BIN="$SPARK_DIR/.venv/bin/spark"

if [ ! -x "$SPARK_BIN" ]; then
  echo "ERROR: spark not found at $SPARK_BIN — run 'uv sync' in $SPARK_DIR first" >&2
  exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ── Guilty Spark pipeline starting ──"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] SPARK_DIR: $SPARK_DIR"

# Step 1: Full index rebuild
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 1/2: reclaim (full index rebuild)..."
"$SPARK_BIN" reclaim

# Step 2: Synthesize decisions from recent MRs
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 2/2: synthesize decisions (last ${DAYS} days)..."
"$SPARK_BIN" synthesize --all --days "$DAYS"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ── Pipeline complete ──"
