#!/usr/bin/env bash
# Client-side sync from the mini PC (nexus).
# Pulls the vault repo and fetches all code repos.
#
# Prerequisites:
#   - SSH config with "Host nexus" pointing at the mini PC
#   - Vault repo cloned locally (git clone nexus:vault ~/vault)
#   - Code repos cloned locally under ~/repos or $REPO_DIR
#
# Usage:
#   ./scripts/sync-from-nexus.sh           # sync vault + fetch repos
#   ./scripts/sync-from-nexus.sh --vault   # vault only
#   ./scripts/sync-from-nexus.sh --repos   # repos only

set -euo pipefail

VAULT_DIR="${VAULT_DIR:-$HOME/vault}"
REPO_DIR="${REPO_DIR:-$HOME/repos}"
MODE="${1:-all}"

LOG_PREFIX="[sync-from-nexus]"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX $*"; }
warn() { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX [WARN] $*" >&2; }

sync_vault() {
  if [ ! -d "$VAULT_DIR/.git" ]; then
    warn "Vault not found at $VAULT_DIR — clone it first: git clone https://github.com/persinac/obs-vault.git $VAULT_DIR"
    return 1
  fi
  log "Syncing vault (bidirectional)..."
  cd "$VAULT_DIR"
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    git add -A
    git commit -m "auto: client sync $(date -u +%Y-%m-%d)" --quiet
    log "Committed local vault changes"
  fi
  if git pull --rebase --quiet 2>/dev/null; then
    log "Vault pulled"
  else
    warn "Vault pull had conflicts — resolve manually in $VAULT_DIR"
  fi
  if git push --quiet 2>/dev/null; then
    log "Vault pushed"
  else
    warn "Vault push failed"
  fi
  cd - >/dev/null
}

sync_repos() {
  log "Fetching all repos in $REPO_DIR..."
  local count=0
  while IFS= read -r -d '' gitdir; do
    local dir
    dir=$(dirname "$gitdir")
    local name
    name=$(basename "$dir")
    if git -C "$dir" fetch --all --prune --quiet 2>/dev/null; then
      ((count++)) || true
    else
      warn "$name: fetch failed"
    fi
  done < <(find "$REPO_DIR" -maxdepth 3 -name ".git" -type d -print0 2>/dev/null)
  log "Fetched $count repos"
}

case "$MODE" in
  --vault) sync_vault ;;
  --repos) sync_repos ;;
  *)       sync_vault; sync_repos ;;
esac
