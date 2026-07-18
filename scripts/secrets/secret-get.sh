#!/usr/bin/env bash
# secret-get.sh — resolve ONE secret through an ordered backend chain.
#
# Tries each backend in $NEXUS_SECRETS_BACKENDS (or --backends) left→right and prints the
# FIRST non-empty value. Every backend fail-softs (missing CLI / absent key → empty, exit 0),
# so an unavailable backend just advances the chain. This generalizes the repo's old
# `${VAR:-$(doppler …)}` idiom into one seam: `env` is the universal default, doppler/aws-sm
# (and overlay-shipped backends like vault) layer after it.
#
# Usage:
#   secret-get.sh [--project P] [--config C] [--backends a,b,c] NAME
#     --project/--config   Doppler scope for THIS call (exported to the doppler backend);
#                          lets each call site pick its own scope without a global.
#     --backends a,b,c     override the chain for this call (else $NEXUS_SECRETS_BACKENDS, else "env")
#     NAME                 the secret name (env-var style: ^[A-Za-z_][A-Za-z0-9_]*$)
#
# Output: the value on stdout (no trailing newline), or nothing if no backend has it.
# Exit:   0 on hit OR miss (miss is not an error — the caller checks for empty).
#         2 only on usage error (missing / malformed NAME).
#
# Backends live beside this script as backend-<name>.sh implementing `get NAME`
# (see README.md for the contract). first non-empty stdout wins; exit codes are ignored.

# NOT `set -e`: fail-soft backends legitimately return non-zero, and `val=$(...)` under -e
# would abort the whole chain. We capture with `|| true` and decide on stdout only.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

proj=""; conf=""; backends=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --project)  proj="${2:-}"; shift 2 ;;
    --config)   conf="${2:-}"; shift 2 ;;
    --backends) backends="${2:-}"; shift 2 ;;
    --)         shift; break ;;
    -*)         echo "secret-get: unknown flag: $1" >&2; exit 2 ;;
    *)          break ;;
  esac
done

NAME="${1:-}"
[ -n "$NAME" ] || { echo "secret-get: need a secret NAME" >&2; exit 2; }
# Guard the name: it becomes an argument to child adapters (and ${!NAME} in backend-env.sh).
case "$NAME" in
  [A-Za-z_]*) ;;
  *) echo "secret-get: invalid NAME '$NAME' (must match ^[A-Za-z_][A-Za-z0-9_]*)" >&2; exit 2 ;;
esac
case "$NAME" in
  *[!A-Za-z0-9_]*) echo "secret-get: invalid NAME '$NAME' (must match ^[A-Za-z_][A-Za-z0-9_]*)" >&2; exit 2 ;;
esac

# Per-call Doppler scope travels to the doppler backend via the environment.
[ -n "$proj" ] && export DOPPLER_PROJECT="$proj"
[ -n "$conf" ] && export DOPPLER_CONFIG="$conf"

# Chain precedence: --backends > $NEXUS_SECRETS_BACKENDS > "env".
chain="${backends:-${NEXUS_SECRETS_BACKENDS:-env}}"

# Split on commas (bash 3.2: read -ra, NOT mapfile).
IFS=',' read -ra _bs <<< "$chain"
for b in "${_bs[@]}"; do
  b="${b// /}"                       # tolerate "env, doppler"
  [ -n "$b" ] || continue
  adapter="$DIR/backend-$b.sh"
  [ -f "$adapter" ] || continue      # unknown backend → skip (an overlay may not be applied)
  val="$(bash "$adapter" get "$NAME" 2>/dev/null || true)"
  if [ -n "$val" ]; then
    printf '%s' "$val"
    exit 0
  fi
done

exit 0   # miss: nothing found in any backend (not an error)
