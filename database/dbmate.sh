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

# Construct DATABASE_URL from Doppler secrets
DB_USER="${NX_DB_USER:-$(doppler secrets get NX_DB_USER --plain --project nexus --config prd)}"
DB_PASSWORD="${NX_DB_PASSWORD:-$(doppler secrets get NX_DB_PASSWORD --plain --project nexus --config prd)}"
DB_HOST="${NX_DB_HOST:-$(doppler secrets get NX_DB_HOST --plain --project nexus --config prd)}"
DB_PORT="${NX_DB_PORT:-$(doppler secrets get NX_DB_PORT --plain --project nexus --config prd)}"
DB_NAME="${NX_DB_NAME:-$(doppler secrets get NX_DB_NAME --plain --project nexus --config prd)}"

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
