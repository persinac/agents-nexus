#!/bin/sh
# launchd boot bring-up. Resolves NEXUS_COMPOSE_FILE the way Taskfile's `dotenv:` does —
# launchd loads no .env, so bare `docker compose` picks the wrong project and collides with
# the running stack's container names, failing every boot.
NX="$(cd "$(dirname "$0")/.." && pwd)"
cd "$NX" || exit 1
for f in .env.local .env; do
  [ -f "$f" ] && { set -a; . "./$f"; set +a; }
done
exec docker compose -f "${NEXUS_COMPOSE_FILE:-docker-compose.yml}" up --no-recreate -d
