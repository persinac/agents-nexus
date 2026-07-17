#!/usr/bin/env bash
# Nightly repo sync — stash, fetch, pull default branch, pop stash.
# Iterates all git repos under $REPO_DIR (default: ~/repos).
#
# Usage:
#   ./scripts/repo-sync.sh              # sync all repos
#   ./scripts/repo-sync.sh --dry-run    # show what would happen

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/repos}"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

LOG_PREFIX="[repo-sync]"
SYNCED=0
SKIPPED=0
FAILED=0

log()  { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX $*"; }
warn() { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX [WARN] $*" >&2; }

detect_default_branch() {
  local dir="$1"
  local ref
  ref=$(git -C "$dir" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||') || true
  if [ -n "$ref" ]; then
    echo "$ref"
    return
  fi
  for candidate in main master develop; do
    if git -C "$dir" rev-parse --verify "origin/$candidate" &>/dev/null; then
      echo "$candidate"
      return
    fi
  done
  warn "$dir: could not detect default branch"
  return 1
}

sync_repo() {
  local dir="$1"
  local name
  name=$(basename "$dir")

  if [ ! -d "$dir/.git" ]; then
    return
  fi

  if ! git -C "$dir" remote get-url origin &>/dev/null; then
    warn "$name: no origin remote, skipping"
    ((SKIPPED++)) || true
    return
  fi

  local branch
  if ! branch=$(detect_default_branch "$dir"); then
    ((SKIPPED++)) || true
    return
  fi

  if $DRY_RUN; then
    log "[DRY] $name → fetch + pull $branch"
    return
  fi

  local stashed=false
  if ! git -C "$dir" diff --quiet HEAD 2>/dev/null || \
     ! git -C "$dir" diff --cached --quiet HEAD 2>/dev/null; then
    git -C "$dir" stash push -m "repo-sync $(date +%Y-%m-%d)" --quiet 2>/dev/null && stashed=true
  fi

  if ! git -C "$dir" fetch --all --prune --quiet 2>/dev/null; then
    warn "$name: fetch failed"
    $stashed && git -C "$dir" stash pop --quiet 2>/dev/null || true
    ((FAILED++)) || true
    return
  fi

  local current_branch
  current_branch=$(git -C "$dir" branch --show-current 2>/dev/null) || true

  if [ "$current_branch" != "$branch" ]; then
    if ! git -C "$dir" checkout "$branch" --quiet 2>/dev/null; then
      warn "$name: checkout $branch failed"
      $stashed && git -C "$dir" stash pop --quiet 2>/dev/null || true
      ((FAILED++)) || true
      return
    fi
  fi

  if ! git -C "$dir" pull --ff-only --quiet 2>/dev/null; then
    warn "$name: pull --ff-only failed (diverged?)"
    ((FAILED++)) || true
  else
    log "$name: synced to $branch"
    ((SYNCED++)) || true
  fi

  if $stashed; then
    if ! git -C "$dir" stash pop --quiet 2>/dev/null; then
      warn "$name: stash pop conflict — changes left in stash"
    fi
  fi
}

log "Starting repo sync in $REPO_DIR"

while IFS= read -r -d '' gitdir; do
  sync_repo "$(dirname "$gitdir")"
done < <(find "$REPO_DIR" -maxdepth 3 -name ".git" -type d -print0 2>/dev/null)

log "Done. synced=$SYNCED skipped=$SKIPPED failed=$FAILED"
