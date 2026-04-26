#!/usr/bin/env bash
# Auto-commit and push vault changes.
# Runs as part of the nightly pipeline after checkpoint notes accumulate.
#
# Usage:
#   ./scripts/vault-commit.sh           # commit + push
#   ./scripts/vault-commit.sh --dry-run # show what would be committed

set -euo pipefail

VAULT_DIR="${VAULT_DIR:-$HOME/vault}"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

LOG_PREFIX="[vault-commit]"
log()  { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX $*"; }
warn() { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX [WARN] $*" >&2; }

if [ ! -d "$VAULT_DIR/.git" ]; then
  warn "$VAULT_DIR is not a git repo — run: git clone https://github.com/persinac/obs-vault.git $VAULT_DIR"
  exit 1
fi

cd "$VAULT_DIR"

if git diff --quiet HEAD 2>/dev/null && git diff --cached --quiet HEAD 2>/dev/null && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  log "No changes to commit"
  exit 0
fi

if $DRY_RUN; then
  log "[DRY] Would commit these changes:"
  git status --short
  exit 0
fi

git add -A
git commit -m "auto: vault sync $(date -u +%Y-%m-%d)"
log "Committed vault changes"

if git remote get-url origin &>/dev/null; then
  git pull --rebase --quiet 2>/dev/null || warn "Pull before push had conflicts — resolve manually"
  if git push --quiet 2>/dev/null; then
    log "Pushed to origin"
  else
    warn "Push failed — changes committed locally"
  fi
else
  log "No origin remote — commit is local only"
fi
