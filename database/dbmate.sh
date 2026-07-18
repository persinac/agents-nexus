#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <database> <dbmate-command> [args...]"
    echo "Example: $0 pgsql up"
    exit 1
fi

DATABASE="$1"
shift

# Get the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATIONS_DIR="$SCRIPT_DIR/$DATABASE/migrations"

if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo "Error: Migrations directory not found: $MIGRATIONS_DIR"
    exit 1
fi

# Construct DATABASE_URL from the secrets chain (env first, else Doppler nexus/prd — the
# historical scope). secret-get.sh's `env` backend reads $NX_DB_* first, so an exporting box
# short-circuits before Doppler exactly as the old `${NX_DB_*:-$(doppler …)}` did. This script
# sources nothing, so it self-defaults the chain to env,doppler regardless of the fleet default.
SEC="$SCRIPT_DIR/../scripts/secrets/secret-get.sh"
_dbsec() { bash "$SEC" --project "${DOPPLER_PROJECT:-nexus}" --config "${DOPPLER_CONFIG:-prd}" \
                       --backends "${NEXUS_SECRETS_BACKENDS:-env,doppler}" "$1"; }
DB_USER="$(_dbsec NX_DB_USER)"
DB_PASSWORD="$(_dbsec NX_DB_PASSWORD)"
DB_HOST="$(_dbsec NX_DB_HOST)"
DB_PORT="$(_dbsec NX_DB_PORT)"
DB_NAME="$(_dbsec NX_DB_NAME)"

# URL-encode user and password in case they contain special characters
urlencode() {
    python3 -c "import urllib.parse; print(urllib.parse.quote('$1', safe=''))"
}

DB_USER_ENCODED=$(urlencode "$DB_USER")
DB_PASSWORD_ENCODED=$(urlencode "$DB_PASSWORD")

DATABASE_URL="postgres://${DB_USER_ENCODED}:${DB_PASSWORD_ENCODED}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=disable&search_path=agents,public"

export DATABASE_URL

# Run dbmate with migrations directory
dbmate --migrations-dir "$MIGRATIONS_DIR" --migrations-table "agents.schema_migrations" "$@"
