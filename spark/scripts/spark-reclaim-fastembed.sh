#!/usr/bin/env bash
# Switch Spark to the FastEmbed embedder and rebuild the full index.
#
# Recreates the spark container (picking up SPARK_EMBEDDER=fastembed from
# docker-compose.work.yml) so the live MCP query path uses FastEmbed, then runs
# a full `spark reclaim` to re-embed every chunk consistently. This is the
# one-time migration after switching embedders (idea #23); it also backfills the
# idea #20 `services` tags across all installations.
#
# Reusable: run manually any time a full FastEmbed re-embed is wanted. The image
# must already be built with the fastembed dependency (docker compose build spark).
set -uo pipefail

# launchd runs with a minimal PATH — make sure docker/compose are findable.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.docker/bin:/usr/bin:/bin:$PATH"

NEXUS_DIR="${AGENTS_NEXUS_DIR:-$HOME/garner/repos/agents-nexus}"
COMPOSE="$NEXUS_DIR/docker-compose.work.yml"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ── spark FastEmbed reclaim starting ──"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] compose: $COMPOSE"

# 1. Recreate spark on FastEmbed (single writer for the reclaim below).
docker compose -f "$COMPOSE" up -d spark
sleep 5

# 2. Full re-embed of the whole index with FastEmbed.
start=$(date +%s)
docker exec nexus-spark uv run spark reclaim
rc=$?
dur=$(( $(date +%s) - start ))
echo "[$(date '+%Y-%m-%d %H:%M:%S')] reclaim exit=$rc duration=${dur}s"

# 3. Quick post-check.
docker exec nexus-spark uv run spark status 2>/dev/null | grep -iE "installations|chunks" || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ── done (exit $rc) ──"
exit $rc
