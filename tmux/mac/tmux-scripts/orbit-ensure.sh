#!/usr/bin/env bash
# Ensure a GitLab checkout has the Orbit hooks, leaving NO diff: `orbit setup` writes two
# git-TRACKED paths, so every artifact is rerouted to an untracked one.
# Usage: orbit-ensure.sh [dir]  (default $PWD, idempotent) | orbit-ensure.sh --all
set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export ORBIT_TELEMETRY_ENABLED=false
REPO_DIR="${REPO_DIR:-$HOME/repos}"
VERBOSE="${ORBIT_ENSURE_VERBOSE:-0}"
log() { [ "$VERBOSE" = 1 ] && printf '%s\n' "$*" >&2; return 0; }

command -v orbit >/dev/null 2>&1 || exit 0

_toplevel() { git -C "$1" rev-parse --show-toplevel 2>/dev/null; }

_is_gitlab() {  # remote host check; never prints the URL (it can embed a token)
  local u; u="$(git -C "$1" remote get-url origin 2>/dev/null)" || return 1
  case "$u" in *gitlab.com*) case "$u" in *github.com*) return 1 ;; esac; return 0 ;; esac
  return 1
}

_has_orbit() {
  grep -qs 'orbit hook-guard' "$1/.claude/settings.json" "$1/.claude/settings.local.json"
}

# --git-common-dir, not $repo/.git: in a linked worktree `.git` is a FILE, so the naive
# path silently does not exist and the artifact stays visible as untracked.
_exclude() {  # <repo> <pattern>
  local gd ex; gd="$(git -C "$1" rev-parse --git-common-dir 2>/dev/null)" || return 0
  case "$gd" in /*) ;; *) gd="$1/$gd" ;; esac
  ex="$gd/info/exclude"; mkdir -p "$gd/info" 2>/dev/null || return 0
  grep -qxF "$2" "$ex" 2>/dev/null || printf '%s\n' "$2" >> "$ex"
}

_lift_hooks() {  # tracked settings.json → move orbit entries into settings.local.json
  python3 - "$1" <<'PY'
import json, os, sys
repo = sys.argv[1]
src, dst = (os.path.join(repo, ".claude", n) for n in ("settings.json", "settings.local.json"))
try: cur = json.load(open(src))
except Exception: sys.exit(0)
moved = {}
for event, groups in (cur.get("hooks") or {}).items():
    hit = [g for g in groups
           if any("orbit" in (h.get("command") or "") for h in (g.get("hooks") or []))]
    if hit: moved[event] = hit
if not moved: sys.exit(0)
local = json.load(open(dst)) if os.path.exists(dst) else {}
lh = local.setdefault("hooks", {})
for event, groups in moved.items():
    cur_list = lh.setdefault(event, [])
    for g in groups:
        if g not in cur_list: cur_list.append(g)
with open(dst, "w") as f:
    json.dump(local, f, indent=2); f.write("\n")
PY
}

_drop_identical_backups() {  # orbit's one-time *.orbit-backup; git is the backup
  local d="$1" b orig rel
  for b in "$d"/CLAUDE.md.orbit-backup "$d"/.claude/settings.json.orbit-backup; do
    [ -f "$b" ] || continue
    orig="${b%.orbit-backup}"; rel="${orig#$d/}"
    if git -C "$d" ls-files --error-unmatch "$rel" >/dev/null 2>&1 \
       && git -C "$d" show "HEAD:$rel" 2>/dev/null | cmp -s - "$b"; then rm -f "$b"
    else _exclude "$d" '*.orbit-backup'; fi
  done
}

ensure() {  # <dir>
  local d; d="$(_toplevel "${1:-$PWD}")" || return 0
  [ -n "$d" ] || return 0
  _is_gitlab "$d" || return 0
  _has_orbit "$d" && return 0

  local md_tracked=0 st_tracked=0
  git -C "$d" ls-files --error-unmatch CLAUDE.md >/dev/null 2>&1 && md_tracked=1
  git -C "$d" ls-files --error-unmatch .claude/settings.json >/dev/null 2>&1 && st_tracked=1

  orbit setup claude --dir "$d" >/dev/null 2>&1 || { log "orbit-ensure: setup failed in $d"; return 0; }

  if [ "$md_tracked" = 1 ]; then git -C "$d" checkout -- CLAUDE.md 2>/dev/null
  else _exclude "$d" "/CLAUDE.md"; fi

  if [ "$st_tracked" = 1 ]; then
    _lift_hooks "$d"
    git -C "$d" checkout -- .claude/settings.json 2>/dev/null
    _exclude "$d" "/.claude/settings.local.json"
  else
    _exclude "$d" "/.claude/settings.json"
  fi

  _drop_identical_backups "$d"
  log "orbit-ensure: installed in $d"
}

if [ "${1:-}" = --all ]; then
  find "$REPO_DIR" -maxdepth 4 -name .git -not -path "*/node_modules/*" 2>/dev/null \
    | while read -r g; do ensure "$(dirname "$g")"; done
else
  ensure "${1:-$PWD}"
fi
exit 0
