#!/usr/bin/env bash
# Opt-in herdr plugin installer for the nexus kit.
#
# Base install (tmux/{mac,linux}/install.sh) deploys a *plugin-free* config.toml.
# This script layers a bundled plugin on top: it links the plugin and wires its
# keybindings into the user's config.toml. herdr plugin manifests CANNOT declare
# keybindings (a manifest [[keys.command]] is silently ignored — verified against
# 0.7.3), so a plugin's chords must be appended to config.toml here.
#
# Idempotent: re-running links only if needed and appends keys only if the marker
# is absent. base install.sh re-invokes this for every already-linked bundled
# plugin, so a base re-run never loses opt-in chords.
#
# Usage: scripts/herdr-plugin-install.sh <plugin-name>      e.g. nexus-fleet
set -euo pipefail

PLUGIN="${1:-}"
if [ -z "$PLUGIN" ]; then
  echo "usage: $(basename "$0") <plugin-name>   (a directory under plugins/)" >&2
  echo "bundled plugins:" >&2
  ls -1 "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/plugins" 2>/dev/null | sed 's/^/  /' >&2 || true
  exit 2
fi

# Resolve the repo root: prefer an explicit AGENTS_NEXUS_DIR, else derive from this
# script's location (scripts/ -> repo root), following one symlink level.
_src="${BASH_SOURCE[0]}"
[ -L "$_src" ] && _src="$(readlink "$_src")"
case "$_src" in /*) ;; *) _src="$(dirname "${BASH_SOURCE[0]}")/$_src" ;; esac
SCRIPT_DIR="$(cd "$(dirname "$_src")" && pwd)"
NEXUS_DIR="${AGENTS_NEXUS_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

PLUGIN_DIR="$NEXUS_DIR/plugins/$PLUGIN"
MANIFEST="$PLUGIN_DIR/herdr-plugin.toml"
CFG="${HERDR_CONFIG:-$HOME/.config/herdr/config.toml}"

command -v herdr >/dev/null 2>&1 || { echo "herdr not found on PATH" >&2; exit 1; }
[ -f "$MANIFEST" ] || { echo "no plugin manifest at $MANIFEST" >&2; exit 1; }
[ -f "$CFG" ]      || { echo "no herdr config at $CFG — run install.sh first" >&2; exit 1; }

# Plugin id from the manifest (e.g. id = "nexus.fleet")
PLUGIN_ID="$(sed -n 's/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST" | head -1)"
[ -n "$PLUGIN_ID" ] || { echo "could not read plugin id from $MANIFEST" >&2; exit 1; }

# 1. (Re)link the plugin — unlink-then-link so a changed manifest is re-read.
herdr plugin unlink "$PLUGIN_ID" >/dev/null 2>&1 || true
herdr plugin link "$PLUGIN_DIR" >/dev/null && echo "* linked $PLUGIN_ID"

# 2. Sync the plugin's keybindings into config.toml. Idempotent by REPLACE (strip any
#    existing marked block, then re-append) so a changed keys.toml propagates.
FRAG="$PLUGIN_DIR/keys.toml"
BEGIN="# >>> nexus-plugin:$PLUGIN_ID keys >>>"
END="# <<< nexus-plugin:$PLUGIN_ID keys <<<"
if [ -f "$FRAG" ]; then
  if grep -qF "$BEGIN" "$CFG"; then
    awk -v b="$BEGIN" -v e="$END" 'index($0,b){skip=1} !skip{print} index($0,e){skip=0}' \
      "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
  fi
  { printf '\n%s\n' "$BEGIN"; cat "$FRAG"; printf '%s\n' "$END"; } >> "$CFG"
  echo "* wired $PLUGIN_ID keybindings into $CFG"
else
  echo "* $PLUGIN_ID declares no keybindings (no keys.toml) — nothing to append"
fi

# 3. Apply (server-side; no-op if the herdr server isn't running yet).
if herdr server reload-config >/dev/null 2>&1; then
  echo "* reloaded herdr config"
else
  echo "* reload skipped (herdr server not running — chords apply on next start)"
fi
echo "Done: $PLUGIN_ID installed."
