#!/usr/bin/env bash
# backend-aws-sm.sh — resolve a secret from AWS Secrets Manager.
#
# `get NAME` → `aws secretsmanager get-secret-value --secret-id <AWS_SM_PREFIX>NAME
#               --query SecretString --output text`. For boxes where Doppler isn't available
# but AWS creds are (keeps secrets centralized + rotatable rather than on-disk).
#
# Config from the environment:
#   AWS_SM_PREFIX  (default: empty)  → secret-id = "${AWS_SM_PREFIX}NAME", e.g. "agents-nexus/"
#   AWS_SM_BIN     (default: aws)
#   plus the standard AWS_* credential chain (AWS_ACCESS_KEY_ID / AWS_PROFILE / AWS_REGION / …)
#
# Fail-soft: missing `aws` CLI, absent secret, or expired creds → empty miss, exit 0. The `aws`
# CLI prints the literal string "None" when a secret exists but has no SecretString (binary-only)
# — we normalize that to empty so it reads as a miss.
#
# Contract (see README.md): stdout = value on hit, empty on miss; exit 0 either way.
set -uo pipefail

[ "${1:-}" = get ] || { echo "backend-aws-sm: usage: backend-aws-sm.sh get NAME" >&2; exit 2; }
NAME="${2:-}"
[ -n "$NAME" ] || { echo "backend-aws-sm: need NAME" >&2; exit 2; }

bin="${AWS_SM_BIN:-aws}"
command -v "$bin" >/dev/null 2>&1 || { printf ''; exit 0; }   # tool absent → miss, fail-soft

id="${AWS_SM_PREFIX:-}$NAME"
out="$("$bin" secretsmanager get-secret-value --secret-id "$id" \
        --query SecretString --output text 2>/dev/null || true)"
[ "$out" = "None" ] && out=""
printf '%s' "$out"
