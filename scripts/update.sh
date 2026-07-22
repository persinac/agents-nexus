#!/usr/bin/env bash
# update.sh — bring an already-installed agents-nexus box up to date.
#
# For teammates who ran ./install.sh on an earlier revision. A plain `git pull` is
# NOT enough: the pull refreshes REPO files, but the INSTALLED copies drift —
# ~/.tmux symlinks, ~/.claude/settings.json (the tool-search env + hooks/perms),
# ~/.claude/claude_code_config.json (MCP servers), the herdr base config + per-plugin
# keybinding blocks, launchd/systemd units — and REMOVED services (spark, dashboard,
# arbiter) keep running until torn down. This re-runs the idempotent install steps,
# resyncs herdr plugin chords, tears down removed services, and prunes dead .env profiles.
#
# Safe to run repeatedly. Non-destructive to your data (DB volumes for kept services are
# untouched); it only removes containers/units/volumes for services that no longer exist.
#
# Usage:
#   bash scripts/update.sh              # pull + reconcile + teardown removed services
#   bash scripts/update.sh --no-pull    # skip the git pull (reconcile the current checkout)
#   bash scripts/update.sh --dry-run    # show what WOULD change; touch nothing
set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

DRY_RUN=0
DO_PULL=1
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --no-pull) DO_PULL=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown flag: $a (see --help)"; exit 2 ;;
  esac
done

detect_os() {
  case "$(uname -s)" in
    Darwin) echo mac ;;
    Linux)  echo linux ;;
    MINGW*|MSYS*|CYGWIN*) echo windows ;;
    *) echo unknown ;;
  esac
}
OS="$(detect_os)"
PLATFORM_DIR="$REPO_DIR/tmux/$OS"

# Services removed from the stack over time. If a box still runs the container /
# volume / launchd-or-systemd unit, tear it down. (compose service name, container
# name, volume glob, launchd/systemd unit basename)
REMOVED_CONTAINERS="nexus-spark nexus-dashboard"
REMOVED_VOLUME_GLOBS="spark-index"
# launchd (mac). muninn.sync was overlay-provided (personal overlay) and retired — a box
# that applied that overlay before it dropped muninn still has the running unit; tear it down.
REMOVED_UNITS="com.agents-nexus.arbiter com.agents-nexus.guilty-spark.nightly com.agents-nexus.muninn.sync"
REMOVED_UNITS_SYSTEMD="agents-nexus-arbiter nightly-spark.timer nightly-spark.service muninn-sync.timer muninn-sync.service"  # systemd (linux)
REMOVED_PROFILES="spark dashboard"   # drop from .env COMPOSE_PROFILES / NEXUS_SERVICES

say() { printf '%s\n' "$*"; }
run() {
  if [ "$DRY_RUN" = "1" ]; then say "  [dry-run] $*"; else eval "$*"; fi
}

changed=0

say "agents-nexus update  (os=$OS, repo=$REPO_DIR)$([ "$DRY_RUN" = 1 ] && echo '  [DRY-RUN]')"
say "────────────────────────────────────────────────────────────"

# ── 1. Pull latest ────────────────────────────────────────────────────────────
if [ "$DO_PULL" = "1" ]; then
  say "1. Pulling latest…"
  before="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
  if [ "$DRY_RUN" = "1" ]; then
    git fetch --quiet origin 2>/dev/null || true
    behind="$(git rev-list --count HEAD..@{u} 2>/dev/null || echo '?')"
    say "  [dry-run] $before → origin ($behind commits behind); would ff-pull"
  else
    if ! git diff --quiet || ! git diff --cached --quiet; then
      say "  ! working tree has uncommitted changes — skipping pull (commit/stash first, or --no-pull)"
    else
      git pull --ff-only 2>&1 | sed 's/^/  /' || say "  ! pull failed (not a fast-forward?) — resolve manually"
      after="$(git rev-parse --short HEAD)"
      [ "$before" != "$after" ] && { say "  updated $before → $after"; changed=1; } || say "  already current ($after)"
    fi
  fi
else
  say "1. Pull skipped (--no-pull); reconciling $(git rev-parse --short HEAD)"
fi
say ""

# ── 2. Re-run platform install (idempotent) ───────────────────────────────────
# This is the workhorse: refreshes ~/.tmux symlinks, merges ~/.claude/settings.json
# (tool-search env + hooks + perms), ~/.claude/claude_code_config.json (MCP servers),
# launchd/systemd units, and the herdr base config. All steps are add/merge/replace —
# safe to re-run.
say "2. Reconciling platform install ($OS)…"
if [ -f "$PLATFORM_DIR/install.sh" ]; then
  # Feed empty stdin: the platform installer has one interactive prompt (agent-memory
  # dir, if it can't auto-detect). For an update the MCP config already exists from the
  # original install, so an empty answer → "skip" is the correct non-blocking default.
  run "bash \"$PLATFORM_DIR/install.sh\" </dev/null" 2>&1 | sed 's/^/  /'
else
  say "  ! $PLATFORM_DIR/install.sh not found — skipping (unknown OS?)"
fi
say ""

# ── 2b. Picker/spawn model (prefer your Claude default; rewrites per-machine env.sh) ──
# Older installs PINNED CLAUDE_MODEL=claude-opus-4-8 (200k window) in ~/.tmux/env.sh — a
# machine-specific file install.sh only ever APPENDS to, so that pin never changes on its
# own, and open-claude.sh passes it as --model, OVERRIDING whatever model your settings.json
# / CLI default is. Clearing it (empty value → open-claude.sh omits --model) makes picker +
# spawned agents fall through to your Claude default — e.g. the opus[1m] 1M window a vanilla
# `claude` already uses — with no pinned id and no entitlement assumption. env.sh is per-box,
# so we ASK, and only with a TTY attached; a headless run (update via cron) leaves it as-is.
say "2b. Picker model (env.sh CLAUDE_MODEL)…"
ENV_SH="$HOME/.tmux/env.sh"
if [ -f "$ENV_SH" ] && grep -qE '^[[:space:]]*(export[[:space:]]+)?CLAUDE_MODEL=.*claude-' "$ENV_SH"; then
  if [ "$DRY_RUN" = "1" ]; then
    say "  [dry-run] would offer to clear the pinned CLAUDE_MODEL so agents use your Claude default"
  elif [ -r /dev/tty ]; then
    printf '  Picker/spawned agents currently PIN a model, overriding your Claude default. Use your Claude default instead? [Y/n] ' > /dev/tty
    read -r _ans < /dev/tty || _ans=""
    cp "$ENV_SH" "$ENV_SH.pre-model.bak"
    case "$_ans" in
      [nN]*)
        printf '  Model id to pin (e.g. claude-opus-4-8[1m] = 1M window, claude-opus-4-8 = 200k): ' > /dev/tty
        read -r _model < /dev/tty || _model=""
        if [ -n "$_model" ]; then
          { grep -vE '^[[:space:]]*(export[[:space:]]+)?CLAUDE_MODEL=' "$ENV_SH.pre-model.bak"; \
            echo "CLAUDE_MODEL=\"\${CLAUDE_MODEL:-$_model}\""; } > "$ENV_SH"
          say "  pinned CLAUDE_MODEL=$_model (backup: env.sh.pre-model.bak)"; changed=1
        else
          mv "$ENV_SH.pre-model.bak" "$ENV_SH"; say "  no model entered — left CLAUDE_MODEL unchanged"
        fi ;;
      *)
        # Empty value (keeps the ${CLAUDE_MODEL:-} env-override seam) → no --model → Claude default.
        { grep -vE '^[[:space:]]*(export[[:space:]]+)?CLAUDE_MODEL=' "$ENV_SH.pre-model.bak"; \
          echo "CLAUDE_MODEL=\"\${CLAUDE_MODEL:-}\""; } > "$ENV_SH"
        say "  cleared CLAUDE_MODEL — agents now use your Claude default (backup: env.sh.pre-model.bak)"; changed=1 ;;
    esac
  else
    say "  - no TTY (headless run) — leaving CLAUDE_MODEL as-is; run update.sh interactively to change"
  fi
else
  say "  - CLAUDE_MODEL not pinned (already uses your Claude default) — nothing to do"
fi
say ""

# ── 3. Re-sync herdr plugin keybindings ───────────────────────────────────────
# Plugin chords are APPENDED into ~/.config/herdr/config.toml at install time; a
# changed keys.toml (new panels/chords) does NOT propagate on pull. Re-run the
# installer for every already-linked bundled plugin so its key block is replaced.
say "3. Re-syncing herdr plugin keybindings…"
if command -v herdr >/dev/null 2>&1 && [ -x "$REPO_DIR/scripts/herdr-plugin-install.sh" ]; then
  linked="$(herdr plugin list 2>/dev/null || true)"
  for pdir in "$REPO_DIR"/plugins/*/; do
    [ -f "$pdir/herdr-plugin.toml" ] || continue
    pid="$(sed -n 's/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$pdir/herdr-plugin.toml" | head -1)"
    pname="$(basename "$pdir")"
    # only resync plugins the box already opted into (linked); don't newly install
    case "$linked" in
      *"$pid"*) run "bash \"$REPO_DIR/scripts/herdr-plugin-install.sh\" \"$pname\"" 2>&1 | sed 's/^/  /'; changed=1 ;;
      *) say "  - $pname not linked (skip; opt in with: scripts/herdr-plugin-install.sh $pname)" ;;
    esac
  done
else
  say "  - herdr not on PATH or installer missing — skipping plugin key resync"
fi
say ""

# ── 4. Tear down REMOVED services ─────────────────────────────────────────────
# spark, dashboard, arbiter were removed from the stack. Stop/rm any that still run
# on this box so a dead endpoint/unit doesn't linger. Kept-service data is untouched.
say "4. Tearing down removed services (spark / dashboard / arbiter)…"

if command -v docker >/dev/null 2>&1; then
  for c in $REMOVED_CONTAINERS; do
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
      run "docker rm -f \"$c\" >/dev/null" && say "  removed container: $c"; changed=1
    fi
  done
  for g in $REMOVED_VOLUME_GLOBS; do
    for v in $(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -- "$g" || true); do
      run "docker volume rm \"$v\" >/dev/null 2>&1" && say "  removed volume: $v"; changed=1
    done
  done
else
  say "  - docker not available — skipping container/volume teardown"
fi

# launchd (mac) / systemd (linux) units for removed services
if [ "$OS" = "mac" ]; then
  uid_n="$(id -u)"
  for u in $REMOVED_UNITS; do
    if launchctl list 2>/dev/null | grep -q "$u" || [ -f "$HOME/Library/LaunchAgents/$u.plist" ]; then
      run "launchctl bootout \"gui/$uid_n/$u\" 2>/dev/null || true"
      run "rm -f \"$HOME/Library/LaunchAgents/$u.plist\"" && say "  removed launchd unit: $u"; changed=1
    fi
  done
elif [ "$OS" = "linux" ]; then
  for u in $REMOVED_UNITS_SYSTEMD; do
    if systemctl --user list-unit-files 2>/dev/null | grep -q "$u"; then
      run "systemctl --user disable --now \"$u\" 2>/dev/null || true"
      run "rm -f \"$HOME/.config/systemd/user/$u\"" && say "  removed systemd unit: $u"; changed=1
    fi
  done
  [ "$DRY_RUN" = 0 ] && systemctl --user daemon-reload 2>/dev/null || true
fi
say ""

# ── 5. Prune removed services from the .env profile lists ─────────────────────
# COMPOSE_PROFILES / NEXUS_SERVICES may still list spark,dashboard — drop them so a
# `docker compose up` / `task up` never tries to start a service that no longer exists.
say "5. Pruning removed services from .env profiles…"
ENV_FILE="$REPO_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  need=0
  for p in $REMOVED_PROFILES; do grep -Eq "(^|,|=)$p(,|$)" "$ENV_FILE" 2>/dev/null && need=1; done
  if [ "$need" = "1" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      say "  [dry-run] would drop [$REMOVED_PROFILES] from COMPOSE_PROFILES/NEXUS_SERVICES + remove DASHBOARD_PORT/SPARK_* in .env"
    else
      cp "$ENV_FILE" "$ENV_FILE.pre-update.bak"
      for p in $REMOVED_PROFILES; do
        perl -i -pe "s/^(COMPOSE_PROFILES=.*?),$p(,|\$)/\$1\$2/; s/^(NEXUS_SERVICES=.*?),$p(,|\$)/\$1\$2/;" "$ENV_FILE"
      done
      perl -i -ne 'print unless /^(DASHBOARD_PORT|SPARK_PORT|SPARK_WEBHOOK_SECRET)=/' "$ENV_FILE"
      say "  pruned $REMOVED_PROFILES from .env (backup: .env.pre-update.bak)"; changed=1
    fi
  else
    say "  - .env profiles already clean"
  fi
else
  say "  - no .env (profile not set up yet) — skip"
fi
say ""

# ── 6. Overlay refresh reminder (NOT auto-applied) ────────────────────────────
# Private overlays (e.g. a team "plugs" repo) drop their own files/units/catalogs into
# this checkout. This updater reconciles the PUBLIC core only — it does NOT re-fetch an
# overlay (that's a separate private repo with its own auth + versioning). If an overlay's
# contents changed upstream, re-apply it from its recorded source. We just detect + nudge.
say "6. Overlays (public core only — overlays are refreshed separately)…"
overlays_found=0
for stamp in "$REPO_DIR"/.overlay-applied.*; do
  [ -f "$stamp" ] || continue
  overlays_found=1
  oname="$(sed -n 's/^# name: //p' "$stamp" | head -1)"
  osrc="$(sed -n 's/^# source: //p' "$stamp" | head -1)"
  say "  • '$oname' applied (source: ${osrc:-unknown})"
  say "    refresh with:  scripts/overlay-apply.sh ${osrc:-<its-git-url>}"
done
[ "$overlays_found" = "0" ] && say "  - none applied (scripts/overlay-apply.sh --status to check)"
say ""

# ── Done ──────────────────────────────────────────────────────────────────────
say "────────────────────────────────────────────────────────────"
if [ "$DRY_RUN" = "1" ]; then
  say "Dry run complete — nothing changed. Re-run without --dry-run to apply."
else
  say "Update complete$([ "$changed" = 0 ] && echo ' (already current)')."
  say "Next:"
  say "  • Relaunch agents so they pick up the new ~/.claude/settings.json env + hooks"
  say "    (running sessions keep the old env until restarted)."
  say "  • herdr chords are live after the reload above — try prefix+shift+f (memory search)"
  say "    and prefix+shift+o (command center)."
  say "  • Bring the (trimmed) stack up if it isn't: docker compose up -d"
  [ "$overlays_found" = "1" ] && say "  • If your team overlay changed upstream, re-apply it (see the overlay lines above)."
fi
