#!/usr/bin/env bash
# backend-env.sh — the universal default secrets backend: read from the process environment.
#
# `get NAME` prints $NAME if set (and non-empty), else nothing. This reproduces the old
# `${NX_DB_USER:-…}` / env-first behavior exactly: an exporting box short-circuits the chain
# here before any external tool is consulted. Always present; needs no CLI.
#
# Contract (see README.md): stdout = value on hit, empty on miss; exit 0 either way.
set -uo pipefail

[ "${1:-}" = get ] || { echo "backend-env: usage: backend-env.sh get NAME" >&2; exit 2; }
NAME="${2:-}"
[ -n "$NAME" ] || { echo "backend-env: need NAME" >&2; exit 2; }
# NAME is already validated by secret-get.sh, but guard here too since adapters may be called
# directly: only a valid identifier is safe for ${!NAME} indirect expansion.
case "$NAME" in
  [A-Za-z_]*) ;; *) exit 0 ;;
esac
case "$NAME" in
  *[!A-Za-z0-9_]*) exit 0 ;;
esac

printf '%s' "${!NAME:-}"
