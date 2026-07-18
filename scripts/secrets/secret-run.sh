#!/usr/bin/env bash
# secret-run.sh — resolve a declared set of secrets, export them, then exec a command.
#
# The launch-wrapper generalization of `doppler run -- CMD`: it resolves each NAMED secret
# through the same ordered chain as secret-get.sh, exports the ones it found, and `exec`s the
# command with that environment. Because it pre-populates the process env, any downstream
# consumer that reads bare `$VAR` / `process.env.X` / `os.environ["X"]` gets backend support
# for free — no per-consumer change.
#
# Usage:
#   secret-run.sh [--project P] [--config C] [--backends a,b,c] NAME [NAME...] -- CMD [ARGS...]
#
# Unlike `doppler run` (which injects the whole project), the name set is EXPLICIT: the chain
# has no cross-backend "list every secret" primitive (env can't enumerate; each backend differs),
# so we resolve exactly the vars the launched process needs. A name that resolves empty is left
# UNSET (never exported as ""), so a downstream `.env`/default fallback still applies.
#
# Exit: execs CMD (replacing this process, preserving PID for systemd Type=simple). Exits 2 on
#       usage error (no `--`, or empty CMD).

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
GET="$DIR/secret-get.sh"

# Pass-through flags for secret-get; collected then forwarded per name.
flags=()
proj=""; conf=""; backends=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --project)  proj="${2:-}"; flags+=(--project "$proj"); shift 2 ;;
    --config)   conf="${2:-}"; flags+=(--config "$conf"); shift 2 ;;
    --backends) backends="${2:-}"; flags+=(--backends "$backends"); shift 2 ;;
    --)         shift; break ;;
    -*)         echo "secret-run: unknown flag: $1" >&2; exit 2 ;;
    *)          break ;;
  esac
done

# Remaining args up to `--` are secret NAMEs; after `--` is the command.
names=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --) shift; break ;;
    *)  names+=("$1"); shift ;;
  esac
done

# Everything left is the command to exec.
cmd=("$@")
[ "${#cmd[@]}" -gt 0 ] || { echo "secret-run: no command after '--'" >&2; exit 2; }

# Resolve + export each declared name (skip on empty → leave unset).
# bash 3.2: "${arr[@]:-}" on an EMPTY array expands to a single '' arg, not nothing — so guard
# both arrays on their count before expanding, never with the :- default.
if [ "${#names[@]}" -gt 0 ]; then
  for n in "${names[@]}"; do
    [ -n "$n" ] || continue
    if [ "${#flags[@]}" -gt 0 ]; then
      v="$(bash "$GET" "${flags[@]}" "$n" 2>/dev/null || true)"
    else
      v="$(bash "$GET" "$n" 2>/dev/null || true)"
    fi
    [ -n "$v" ] && export "$n=$v"
  done
fi

exec "${cmd[@]}"
