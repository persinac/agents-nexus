#!/usr/bin/env bash
# backend-doppler.sh — resolve a secret from Doppler (https://doppler.com).
#
# `get NAME` → `doppler secrets get NAME --plain --project $P --config $C`. Doppler is an
# OPTIONAL per-machine tool: a box without the CLI (e.g. a work laptop) must degrade to a miss,
# not error — so the chain continues to the next backend. Mirrors the FileNotFoundError catch
# that agent-runner/conductor.py uses for the same reason.
#
# Scope comes from the environment (set per-call by secret-get.sh --project/--config):
#   DOPPLER_PROJECT  (default: nexus)   DOPPLER_CONFIG (default: prd)   DOPPLER_BIN (default: doppler)
# The nexus/prd defaults reproduce this fleet's historical scope; a caller that needs a
# different project (e.g. conductor's Trello uses infrastructure/prd) passes it explicitly.
#
# Contract (see README.md): stdout = value on hit, empty on miss/tool-absent; exit 0 either way.
set -uo pipefail

[ "${1:-}" = get ] || { echo "backend-doppler: usage: backend-doppler.sh get NAME" >&2; exit 2; }
NAME="${2:-}"
[ -n "$NAME" ] || { echo "backend-doppler: need NAME" >&2; exit 2; }

bin="${DOPPLER_BIN:-doppler}"
command -v "$bin" >/dev/null 2>&1 || { printf ''; exit 0; }   # tool absent → miss, fail-soft

proj="${DOPPLER_PROJECT:-nexus}"
conf="${DOPPLER_CONFIG:-prd}"

# stderr silenced so an error message can never contaminate the value; `|| true` so a
# non-zero exit (unknown secret, not-authed) becomes an empty miss, not a chain abort.
out="$("$bin" secrets get "$NAME" --plain --project "$proj" --config "$conf" 2>/dev/null || true)"
printf '%s' "$out"
