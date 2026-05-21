#!/bin/bash
# Auto-rebase the typed-tool-results branch onto origin/main on weekday mornings.
# Installed as a launchd agent: ~/Library/LaunchAgents/com.alexpersinger.svc-chatbot-rebase.plist
# Self-uninstalls when the MR merges or closes.

set -uo pipefail

REPO="/Users/alex.persinger@getgarner.com/garner/repos/search/concierge/svc-chatbot"
BRANCH="typed-tool-results"
PLIST="$HOME/Library/LaunchAgents/com.alexpersinger.svc-chatbot-rebase.plist"
LOG="$HOME/Library/Logs/svc-chatbot-rebase.log"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }

notify() {
    local title="$1" msg="$2"
    /usr/bin/osascript -e "display notification \"$msg\" with title \"$title\"" 2>>"$LOG" || true
    log "notify: $title — $msg"
}

# glab needs GITLAB_TOKEN (its stored config token is expired); git uses osxkeychain.
# Pull just the export line out of zprofile rather than sourcing the whole file.
token_line=$(/usr/bin/grep '^export GITLAB_TOKEN=' "$HOME/.zprofile" 2>/dev/null || true)
if [ -n "$token_line" ]; then
    eval "$token_line"
fi

cd "$REPO" || { notify "rebase failed" "couldn't cd to $REPO"; exit 1; }

log "=== run start ==="

current=$(git rev-parse --abbrev-ref HEAD)
if [ "$current" != "$BRANCH" ]; then
    log "skip: on '$current', not '$BRANCH'"
    exit 0
fi

# Discard the .secrets.baseline timestamp noise (documented in repo checkpoint notes).
git checkout .secrets.baseline 2>/dev/null || true

if [ -n "$(git status --porcelain)" ]; then
    notify "rebase aborted" "$BRANCH has uncommitted changes"
    log "abort: working tree dirty"
    git status --porcelain >>"$LOG"
    exit 1
fi

mr_state=$(glab mr view --output json 2>>"$LOG" | jq -r '.state // empty' 2>>"$LOG")
case "$mr_state" in
    merged|closed)
        notify "rebase job done" "MR $mr_state — uninstalling launchd agent"
        log "MR $mr_state, uninstalling"
        launchctl unload "$PLIST" 2>>"$LOG" || true
        rm -f "$PLIST"
        exit 0
        ;;
    opened|reopened|locked)
        log "MR state: $mr_state"
        ;;
    *)
        log "could not determine MR state (got '$mr_state'); skipping run"
        exit 0
        ;;
esac

if ! git fetch origin main 2>>"$LOG"; then
    notify "rebase failed" "git fetch origin main failed"
    exit 1
fi

behind=$(git rev-list --count "${BRANCH}..origin/main")
if [ "$behind" = "0" ]; then
    log "already up to date with origin/main, nothing to do"
    exit 0
fi

log "rebasing $behind commit(s) from origin/main"
if ! git rebase origin/main 2>>"$LOG"; then
    git rebase --abort 2>>"$LOG" || true
    notify "rebase conflict" "$BRANCH conflicts with origin/main — manual resolution needed"
    log "rebase aborted due to conflicts"
    exit 1
fi

new_sha=$(git rev-parse --short HEAD)
if ! git push --force-with-lease 2>>"$LOG"; then
    notify "push failed" "force-with-lease rejected — branch moved on remote?"
    log "push --force-with-lease rejected"
    exit 1
fi

notify "rebase succeeded" "$BRANCH @ $new_sha (folded $behind from main)"
log "ok: rebased $behind commits, pushed $new_sha"
