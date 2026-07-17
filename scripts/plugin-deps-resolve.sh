#!/usr/bin/env bash
# plugin-deps-resolve.sh — resolve ONE plugin's nexus.deps.toml: run each
# [[requires]] check, surface its guide when unsatisfied, resolve [[env]] values
# (probe → default), and report whether the plugin is installable.
#
# This is the check→guide→env engine the installer's per-plugin step calls. It is
# deliberately dependency-free (pure bash, no python/awk features) so the fleet-only
# trial resolves with nothing but a shell. Values are single-line scalars per the
# locked contract (docs/plugin-install-contract.md): a "quoted string" (\" escapes)
# or a bare true|false.
#
# Usage:
#   plugin-deps-resolve.sh <deps.toml | plugin-dir | plugins/<dir>> [opts]
# Options:
#   --env-out FILE   append resolved (non-empty) NAME=value lines here
#   --env-in FILE    source FILE before running checks/probes (e.g. the profile .env)
#   --trial          non-interactive: never prompt; optional-unsatisfied is fine
#   --quiet          summary line only
# Exit: 0 = installable (no required dep unsatisfied), 1 = blocked, 2 = usage/parse error.
set -u

DEPS=""; ENV_OUT=""; ENV_IN=""; TRIAL=0; QUIET=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-out) ENV_OUT="${2:-}"; shift 2 ;;
    --env-in)  ENV_IN="${2:-}";  shift 2 ;;
    --trial)   TRIAL=1; shift ;;
    --quiet)   QUIET=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  DEPS="$1"; shift ;;
  esac
done
[ -n "$DEPS" ] || { echo "usage: plugin-deps-resolve.sh <deps.toml|plugin-dir> [opts]" >&2; exit 2; }
# Resolve <plugin-dir> / plugins/<dir> to its nexus.deps.toml.
[ -d "$DEPS" ] && DEPS="$DEPS/nexus.deps.toml"
[ -f "$DEPS" ] || { echo "no such deps file: $DEPS" >&2; exit 2; }
PLUGIN="$(basename "$(dirname "$DEPS")")"

# Load an env layer so checks/probes see DATABASE_URL, AGENTS_NEXUS_DIR, NEXUS_TMUX_DIR, …
if [ -n "$ENV_IN" ] && [ -f "$ENV_IN" ]; then set -a; . "$ENV_IN"; set +a; fi

# ── colours (only on a TTY) ──────────────────────────────────────────────────
if [ -t 2 ]; then C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_0=$'\033[0m'
else C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_0=""; fi
say(){ [ "$QUIET" = 1 ] || printf '%s\n' "$*" >&2; }

# ── value parser: "quoted" (unescape \" and \\) or bare true|false/scalar ─────
parse_value(){
  local raw="$1" v
  case "$raw" in
    '"'*)
      v="${raw#\"}"; v="${v%\"*}"        # strip first + last quote (single-line scalar)
      v="${v//\\\"/\"}"; v="${v//\\\\/\\}"
      printf '%s' "$v" ;;
    *)
      v="${raw%%#*}"                      # bare value: drop trailing comment
      v="${v%"${v##*[![:space:]]}"}"      # rtrim
      printf '%s' "$v" ;;
  esac
}

OK=0; DEGRADED=0; BLOCKED=0
section=""; seen_table=0; SETUP_GUIDE=""
r_type=""; r_id=""; r_check=""; r_opt=""; r_guide=""
e_name=""; e_req=""; e_def=""; e_probe=""; e_desc=""

flush(){
  case "$section" in
    requires)
      [ -n "$r_id" ] || return 0
      local opt=0; case "$r_opt" in true|1) opt=1 ;; esac
      if sh -c "$r_check" >/dev/null 2>&1; then
        OK=$((OK+1)); say "  ${C_OK}✓${C_0} ${r_type}:${r_id} ${C_DIM}(satisfied)${C_0}"
      elif [ "$opt" = 1 ]; then
        DEGRADED=$((DEGRADED+1))
        say "  ${C_WARN}○${C_0} ${r_type}:${r_id} ${C_DIM}optional — degrades${C_0}"
        [ -n "$r_guide" ] && say "      ${C_DIM}${r_guide}${C_0}"
      else
        BLOCKED=$((BLOCKED+1))
        say "  ${C_ERR}✗${C_0} ${r_type}:${r_id} ${C_ERR}REQUIRED — missing${C_0}"
        [ -n "$r_guide" ] && say "      → ${r_guide}"
      fi
      ;;
    env)
      [ -n "$e_name" ] || return 0
      local cur val; eval "cur=\${$e_name:-}"; val="$cur"
      if [ -z "$val" ] && [ -n "$e_probe" ]; then val="$(sh -c "$e_probe" 2>/dev/null | head -1)"; fi
      if [ -z "$val" ] && [ -n "$e_def" ]; then val="$e_def"; fi
      local req=0; case "$e_req" in true|1) req=1 ;; esac
      if [ -n "$val" ]; then
        say "  ${C_OK}·${C_0} ${e_name}=${val}"
        [ -n "$ENV_OUT" ] && printf '%s=%s\n' "$e_name" "$val" >> "$ENV_OUT"
      elif [ "$req" = 1 ]; then
        BLOCKED=$((BLOCKED+1)); say "  ${C_ERR}·${C_0} ${e_name} ${C_ERR}REQUIRED — unset${C_0}  ${C_DIM}${e_desc}${C_0}"
      else
        say "  ${C_DIM}· ${e_name} unset (optional) — ${e_desc}${C_0}"
      fi
      ;;
  esac
}
reset_block(){ r_type=""; r_id=""; r_check=""; r_opt=""; r_guide=""; e_name=""; e_req=""; e_def=""; e_probe=""; e_desc=""; }

while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|[[:space:]]*'#'*|'#'*) continue ;; esac
  if [[ "$line" =~ ^\[\[(requires|env)\]\] ]]; then
    flush; section="${BASH_REMATCH[1]}"; seen_table=1; reset_block; continue
  fi
  if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
    key="${BASH_REMATCH[1]}"; val="$(parse_value "${BASH_REMATCH[2]}")"
    if [ -z "$section" ]; then
      case "$key" in setup_guide) SETUP_GUIDE="$val" ;; esac
    else
      # lint: a top-level key that got authored inside a table (TOML swallows it)
      if [ "$key" = "setup_guide" ]; then
        say "  ${C_WARN}!${C_0} lint: 'setup_guide' appears inside [[${section}]] — move it above the first [[…]] (TOML binds it into the table otherwise)"
      fi
      case "$section:$key" in
        requires:type) r_type="$val" ;;   requires:id) r_id="$val" ;;
        requires:check) r_check="$val" ;; requires:optional) r_opt="$val" ;;
        requires:guide) r_guide="$val" ;;
        env:name) e_name="$val" ;;        env:required) e_req="$val" ;;
        env:default) e_def="$val" ;;      env:probe) e_probe="$val" ;;
        env:describe) e_desc="$val" ;;
      esac
    fi
  fi
done < "$DEPS"
flush

VERB="installable"; RC=0
[ "$BLOCKED" -gt 0 ] && { VERB="BLOCKED"; RC=1; }
[ -n "$SETUP_GUIDE" ] && say "  ${C_DIM}${SETUP_GUIDE}${C_0}"
printf '%s%s%s: %d ok · %d degraded · %d blocked → %s\n' \
  "$([ $RC = 0 ] && echo "$C_OK" || echo "$C_ERR")" "$PLUGIN" "$C_0" \
  "$OK" "$DEGRADED" "$BLOCKED" "$VERB" >&2
exit "$RC"
