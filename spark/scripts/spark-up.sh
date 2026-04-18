#!/usr/bin/env bash
# Spin up all Guilty Spark services and install the nightly launchd job.
# Idempotent — safe to run on every restart or after first install.
#
# What this does:
#   1. Starts Ollama as a brew service (survives restarts automatically)
#   2. Installs + loads the nightly pipeline launchd plist (2 AM daily)
#
# Note: The MCP server (spark serve) is managed by Claude Code via stdio —
# it doesn't need to run as a persistent service.

set -euo pipefail

SPARK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_TEMPLATE="$SPARK_DIR/launchd/com.guilty-spark.nightly.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.guilty-spark.nightly.plist"
PIPELINE_SCRIPT="$SPARK_DIR/scripts/spark-pipeline.sh"

echo "==> Guilty Spark — starting up"

# ── 1. Ollama ──────────────────────────────────────────────────────────────
echo ""
echo "--> Ollama"
if brew services info ollama 2>/dev/null | grep -q "Status: started"; then
  echo "    already running (brew service)"
else
  brew services start ollama
  echo "    started (brew service — will auto-start on login)"
  # Give it a moment to bind
  sleep 3
fi

if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
  echo "    API responding at http://localhost:11434"
else
  echo "    WARNING: API not yet responding — may still be starting up"
fi

# ── 2. Nightly pipeline (launchd) ──────────────────────────────────────────
echo ""
echo "--> Nightly pipeline"

# Ensure log directory exists
mkdir -p "$SPARK_DIR/logs"
chmod +x "$PIPELINE_SCRIPT"

# Install plist — substitute placeholders with actual paths
sed -e "s|SPARK_DIR_PLACEHOLDER|$SPARK_DIR|g" \
    -e "s|HOME_PLACEHOLDER|$HOME|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
echo "    loaded: $PLIST_DEST"
echo "    scheduled: 2:00 AM daily"
echo "    log: $SPARK_DIR/logs/pipeline.log"

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "==> Done. Services:"
echo "    Ollama:           brew service (auto-starts on login)"
echo "    Nightly pipeline: launchd (2 AM daily, survives restarts)"
echo "    MCP server:       spawned on demand by Claude Code (stdio)"
echo ""
echo "Useful commands:"
echo "  spark status                        — check index health"
echo "  $PIPELINE_SCRIPT   — run pipeline now"
echo "  tail -f $SPARK_DIR/logs/pipeline.log — watch nightly log"
