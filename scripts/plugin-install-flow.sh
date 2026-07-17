#!/usr/bin/env bash
# plugin-install-flow.sh — the marketplace-style plugin step of the nexus install.
#
# Reads plugins/catalog.toml, lets you pick plugins (multi-select; --trial = the
# default-on set, no prompts), installs each by its source backend, resolves each
# plugin's nexus.deps.toml via plugin-deps-resolve.sh, and merges the resolved env
# into the profile .env. This is what install.sh's "plugins?" branch calls.
#
# Three source backends (routed by which key catalog.toml gives each plugin):
#   source = { bundled = "<dir>" }                              → scripts/herdr-plugin-install.sh <dir>
#   source = { remote  = "owner/repo/subdir" }                  → herdr plugin install <that> [--yes]
#   source = { claude_marketplace = { marketplace=…, plugin=… } } → claude plugin marketplace add + install
#
# Usage:
#   plugin-install-flow.sh [--catalog F] [--profile ENVFILE] [--trial] [--all] [--dry-run] [--yes]
#   --trial     non-interactive; install only default=true plugins (the minimal trial)
#   --all       select every catalog plugin (non-interactive)
#   --dry-run   print the install commands instead of running them; never write the real profile
#   --yes       pass --yes to `herdr plugin install` (unattended remote installs)
# Exit: 0 = every chosen plugin installable; 1 = at least one blocked; 2 = usage/parse error.
set -u

# ── repo root (canonicalize through any symlinks, scripts/ → root) ───────────
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd "$(dirname "$_src")" && pwd -P)"; _tgt="$(readlink "$_src")"
  case "$_tgt" in /*) _src="$_tgt" ;; *) _src="$_dir/$_tgt" ;; esac
done
SCRIPT_DIR="$(cd "$(dirname "$_src")" && pwd -P)"
NEXUS_DIR="${AGENTS_NEXUS_DIR:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
RESOLVE="$SCRIPT_DIR/plugin-deps-resolve.sh"
HPI="$SCRIPT_DIR/herdr-plugin-install.sh"

CATALOG="$NEXUS_DIR/plugins/catalog.toml"
PROFILE="$NEXUS_DIR/.env"
TRIAL=0; ALL=0; DRY=0; YES=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --catalog) CATALOG="${2:?}"; shift 2 ;;
    --profile) PROFILE="${2:?}"; shift 2 ;;
    --trial)   TRIAL=1; shift ;;
    --all)     ALL=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --yes)     YES=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -f "$CATALOG" ] || { echo "no catalog: $CATALOG" >&2; exit 2; }
[ -f "$RESOLVE" ] || { echo "resolver missing: $RESOLVE" >&2; exit 2; }

if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; OKC=$'\033[32m'; ERRC=$'\033[31m'; Z=$'\033[0m'
else B=""; D=""; OKC=""; ERRC=""; Z=""; fi
run(){ if [ "$DRY" = 1 ]; then echo "    ${D}[dry-run] $*${Z}"; else "$@"; fi; }

# ── parse catalog.toml → parallel arrays ─────────────────────────────────────
ID=(); NAME=(); DESC=(); DEF=(); KIND=(); A1=(); A2=()
pid=""; pname=""; pdesc=""; pdef=""; pkind=""; pa1=""; pa2=""
flush_plugin(){
  [ -n "$pid" ] || return 0
  ID+=("$pid"); NAME+=("${pname:-$pid}"); DESC+=("$pdesc"); DEF+=("${pdef:-false}")
  KIND+=("$pkind"); A1+=("$pa1"); A2+=("$pa2")
}
reset_plugin(){ pid=""; pname=""; pdesc=""; pdef=""; pkind=""; pa1=""; pa2=""; }
strq(){ local v="$1"; case "$v" in '"'*) v="${v#\"}"; v="${v%%\"*}";; esac; printf '%s' "$v"; }

parse_catalog(){         # append every [[plugin]] in $1 to the arrays (pid/… are globals for flush)
  local f="$1" line k v
  reset_plugin
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|[[:space:]]*'#'*|'#'*) continue ;; esac
    if [[ "$line" =~ ^\[\[plugin\]\] ]]; then flush_plugin; reset_plugin; continue; fi
    [[ "$line" =~ ^\[\[thing\]\] ]] && { flush_plugin; reset_plugin; pid="__thing__"; continue; }  # ignore thing tables
    [ "$pid" = "__thing__" ] && continue
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
      k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
      case "$k" in
        id)          pid="$(strq "$v")" ;;
        name)        pname="$(strq "$v")" ;;
        description) pdesc="$(strq "$v")" ;;
        default)     case "$v" in true*) pdef=true ;; *) pdef=false ;; esac ;;
        source)
          if   [[ "$v" =~ bundled[[:space:]]*=[[:space:]]*\"([^\"]*)\" ]]; then pkind="bundled"; pa1="${BASH_REMATCH[1]}"
          elif [[ "$v" =~ remote[[:space:]]*=[[:space:]]*\"([^\"]*)\" ]];  then pkind="remote";  pa1="${BASH_REMATCH[1]}"
          elif [[ "$v" =~ marketplace[[:space:]]*=[[:space:]]*\"([^\"]*)\"[[:space:]]*,[[:space:]]*plugin[[:space:]]*=[[:space:]]*\"([^\"]*)\" ]]; then
            pkind="claude_marketplace"; pa1="${BASH_REMATCH[1]}"; pa2="${BASH_REMATCH[2]}"
          fi ;;
      esac
    fi
  done < "$f"
  flush_plugin
}

parse_catalog "$CATALOG"

# Private, auth-gated overlays living beside the catalog: catalog.<org>.toml (NOT *.example.toml,
# NOT the main catalog). Present ONLY on org machines — gitignored here, shipped via a private repo.
# A public clone has only the *.example.toml template → nothing merges → outsiders see only the
# bundled nexus plugins. No new gate: the per-entry marketplace-registered check still guards
# install, so an overlay entry a user isn't entitled to is skipped anyway.
_cat_dir="$(cd "$(dirname "$CATALOG")" && pwd -P)"
for ov in "$_cat_dir"/catalog.*.toml; do
  [ -e "$ov" ] || continue                       # no matches → glob stays literal → skip
  case "$ov" in *.example.toml) continue ;; esac  # template is not an overlay
  [ "$ov" = "$CATALOG" ] && continue
  echo "${D}+ overlay: $(basename "$ov")${Z}" >&2
  parse_catalog "$ov"
done

N=${#ID[@]}
[ "$N" -gt 0 ] || { echo "catalog has no [[plugin]] entries" >&2; exit 2; }

# ── selection ────────────────────────────────────────────────────────────────
SEL=()
if [ "$ALL" = 1 ]; then
  for i in $(seq 0 $((N-1))); do SEL+=("$i"); done
elif [ "$TRIAL" = 1 ] || [ ! -t 0 ]; then
  for i in $(seq 0 $((N-1))); do [ "${DEF[$i]}" = true ] && SEL+=("$i"); done
  [ "$TRIAL" = 1 ] || echo "${D}(non-interactive stdin → default-on plugins)${Z}" >&2
elif command -v fzf >/dev/null 2>&1; then
  menu=""; for i in $(seq 0 $((N-1))); do
    mark=" "; [ "${DEF[$i]}" = true ] && mark="*"
    menu+="$i	[$mark] ${NAME[$i]} — ${DESC[$i]}"$'\n'
  done
  picks="$(printf '%s' "$menu" | fzf --multi --with-nth=2.. --delimiter='\t' \
            --prompt='plugins (TAB to multi-select, * = default) > ' --height=60% --reverse || true)"
  [ -n "$picks" ] && while IFS=$'\t' read -r idx _; do SEL+=("$idx"); done <<< "$picks"
else
  echo "${B}Available plugins:${Z}" >&2
  for i in $(seq 0 $((N-1))); do
    mark=" "; [ "${DEF[$i]}" = true ] && mark="*"
    printf "  %d) [%s] %s — %s\n" "$i" "$mark" "${NAME[$i]}" "${DESC[$i]}" >&2
  done
  printf "Select (space-separated numbers, blank = the * defaults): " >&2; read -r ans || ans=""
  if [ -z "$ans" ]; then for i in $(seq 0 $((N-1))); do [ "${DEF[$i]}" = true ] && SEL+=("$i"); done
  else for x in $ans; do SEL+=("$x"); done; fi
fi
[ "${#SEL[@]}" -gt 0 ] || { echo "no plugins selected — done (the minimal trial)." >&2; exit 0; }

# ── per-plugin: install (by backend) → resolve deps → collect env ────────────
ENVTMP="$(mktemp "${TMPDIR:-/tmp}/nexus-env.XXXXXX")"; trap 'rm -f "$ENVTMP"' EXIT
ANYBLOCK=0; INSTALLED=""; BLOCKED=""
for i in "${SEL[@]}"; do
  echo >&2; echo "${B}▸ ${NAME[$i]} (${ID[$i]})${Z}" >&2
  case "${KIND[$i]}" in
    bundled)
      echo "  install: bundled → herdr-plugin-install.sh ${A1[$i]}" >&2
      run bash "$HPI" "${A1[$i]}" >&2 || echo "    ${ERRC}install failed${Z}" >&2
      DEPS="$NEXUS_DIR/plugins/${A1[$i]}/nexus.deps.toml" ;;
    remote)
      echo "  install: remote → herdr plugin install ${A1[$i]}" >&2
      run herdr plugin install "${A1[$i]}" $([ "$YES" = 1 ] && echo --yes) >&2 || echo "    ${ERRC}install failed${Z}" >&2
      DEPS="" ;;   # remote plugin ships its own deps.toml at the install path — resolve TODO
    claude_marketplace)
      # AUTH-GATE: only install if the marketplace is ALREADY registered locally — that means the
      # user set it up and is entitled to it. We never `marketplace add` a URL from the catalog
      # (this repo is public; an org's marketplace URL/name must not be attempted for everyone). A
      # user without it registered sees nothing org-specific and no install fires.
      mkt="${A1[$i]}"; plug="${A2[$i]}"
      if command -v claude >/dev/null 2>&1 && claude plugin marketplace list 2>/dev/null | grep -q "$mkt"; then
        echo "  install: claude-marketplace → ${plug}@${mkt} (marketplace registered)" >&2
        run claude plugin install "${plug}@${mkt}" >&2 || echo "    ${ERRC}install failed${Z}" >&2
        INSTALLED="$INSTALLED ${ID[$i]}"
        echo "    ${D}(deps resolve at the installed path — not run here)${Z}" >&2
      else
        echo "  ${D}skip: ${plug}@${mkt} — marketplace '${mkt}' not registered (org-gated; nothing attempted)${Z}" >&2
      fi
      continue ;;
    *) echo "  ${ERRC}unknown source backend for ${ID[$i]} — skipped${Z}" >&2; continue ;;
  esac
  # Resolve deps (read-only probes; safe even in a real run). Only bundled has a local deps file here.
  if [ -n "${DEPS:-}" ] && [ -f "$DEPS" ]; then
    if bash "$RESOLVE" "$DEPS" --env-out "$ENVTMP" $([ -f "$PROFILE" ] && echo --env-in "$PROFILE"); then
      INSTALLED="$INSTALLED ${ID[$i]}"
    else
      ANYBLOCK=1; BLOCKED="$BLOCKED ${ID[$i]}"
    fi
  else
    INSTALLED="$INSTALLED ${ID[$i]}"
    [ "${KIND[$i]}" != bundled ] && echo "    ${D}(deps resolve at the installed path — not run here)${Z}" >&2
  fi
done

# ── merge resolved env into the profile (dedupe by NAME, last wins) ──────────
echo >&2
if [ -s "$ENVTMP" ]; then
  # dedupe keeping the LAST occurrence of each NAME
  DEDUP="$(awk -F= '{k=$1; v=substr($0,index($0,"=")+1); last[k]=v; order[k]=NR} END{for(k in last) printf "%d\t%s=%s\n", order[k], k, last[k]}' "$ENVTMP" | sort -n | cut -f2-)"
  if [ "$DRY" = 1 ]; then
    echo "${B}env that would be merged into ${PROFILE}:${Z}" >&2
    printf '%s\n' "$DEDUP" | sed 's/^/    /' >&2
  else
    touch "$PROFILE"
    while IFS= read -r kv; do
      [ -n "$kv" ] || continue
      name="${kv%%=*}"
      if grep -q "^${name}=" "$PROFILE" 2>/dev/null; then
        : # keep the user's existing value — never clobber
      else
        printf '%s\n' "$kv" >> "$PROFILE"
      fi
    done <<< "$DEDUP"
    echo "${OKC}✓${Z} merged env into ${PROFILE} (existing values kept)" >&2
  fi
fi

# ── summary ──────────────────────────────────────────────────────────────────
echo >&2
echo "${B}Plugins:${Z}${INSTALLED:- none}${Z}" >&2
[ -n "$BLOCKED" ] && echo "${ERRC}Blocked (unmet required deps):${Z}$BLOCKED — see guides above" >&2
exit "$ANYBLOCK"
