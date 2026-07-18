#!/usr/bin/env bash
# backend-vault.sh — EXAMPLE org secrets backend: HashiCorp Vault (KV v2).
#
# Shipped from a private overlay (this is the worked example). It lands beside the core
# adapters at <core>/scripts/secrets/backend-vault.sh via the overlay path-mirror; the core's
# secret-get.sh discovers any backend-<name>.sh with no registration. Activate it by adding
# `vault` to NEXUS_SECRETS_BACKENDS (see overlay.toml's [[env]] blocks).
#
# `get NAME` → `vault kv get -field=NAME <VAULT_SECRET_PATH>`. Config from the environment:
#   VAULT_ADDR         Vault server (also honored by the vault CLI directly)
#   VAULT_SECRET_PATH  KV path holding the fields (default: secret/agents-nexus)
#   VAULT_TOKEN        auth token — a REAL secret; set it in ~/.tmux/env.sh, never in the overlay
#
# Contract (see the core scripts/secrets/README.md): stdout = value on hit, empty on
# miss/tool-absent; exit 0 either way (fail-soft so the chain continues).
set -uo pipefail

[ "${1:-}" = get ] || { echo "backend-vault: usage: backend-vault.sh get NAME" >&2; exit 2; }
NAME="${2:-}"
[ -n "$NAME" ] || { echo "backend-vault: need NAME" >&2; exit 2; }

command -v vault >/dev/null 2>&1 || { printf ''; exit 0; }   # tool absent → miss, fail-soft

path="${VAULT_SECRET_PATH:-secret/agents-nexus}"
out="$(vault kv get -field="$NAME" "$path" 2>/dev/null || true)"
printf '%s' "$out"
