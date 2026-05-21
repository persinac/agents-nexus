#!/bin/bash
# Auto-maintain open MRs in garner-health/svc-chatbot:
#   - severity:: labels (severity::high if title contains "NDS", else severity::medium) when no labels
#   - Assign MR author as assignee if none
#   - Assign CODEOWNERS default reviewers if none
#   - 5+ days no activity  → comment pinging the owner + add `nudged-5d` label (one-time)
#   - 7+ days no activity  → add `stale` label + convert to Draft (one-time)
#
# "Activity" means commits to the source branch OR non-system, non-bot comments —
# label/assignee/reviewer edits and the bot's own nudge comments do NOT count.
#
# Usage:
#   svc-chatbot-mr-labels.sh              run live
#   svc-chatbot-mr-labels.sh --dry-run    log what would happen, mutate nothing

set -uo pipefail

REPO="garner-health/svc-chatbot"
REPO_ENC="garner-health%2Fsvc-chatbot"
LOG="$HOME/Library/Logs/svc-chatbot-mr-labels.log"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }

has_label() { echo "$1" | tr ',' '\n' | grep -qx "$2"; }

iso_to_epoch() {
    python3 -c "import sys, datetime; print(int(datetime.datetime.fromisoformat(sys.argv[1].replace('Z', '+00:00')).timestamp()))" "$1" 2>/dev/null
}

# Marker the bot embeds in its own comments — used to ignore them for activity calc.
BOT_MARKER="_Auto-comment from svc-chatbot-mr-labels cron._"

# last_activity_epoch IID FALLBACK_ISO
# Returns max(latest commit date, latest non-system non-bot note date, fallback).
# "Activity" deliberately excludes label changes, assignee/reviewer edits, system notes,
# and our own bot nudge comments — only commits and human review comments count.
last_activity_epoch() {
    local iid="$1" fallback="$2"
    local max=0 ts e

    ts=$(glab api "projects/$REPO_ENC/merge_requests/$iid/commits?per_page=1" 2>/dev/null \
         | jq -r '.[0].committed_date // empty')
    if [ -n "$ts" ]; then
        e=$(iso_to_epoch "$ts")
        [ -n "$e" ] && [ "$e" -gt "$max" ] && max=$e
    fi

    ts=$(glab api "projects/$REPO_ENC/merge_requests/$iid/notes?sort=desc&order_by=created_at&per_page=100" 2>/dev/null \
         | jq -r --arg marker "$BOT_MARKER" \
             '[.[] | select(.system == false) | select((.body // "") | contains($marker) | not)] | .[0].created_at // empty')
    if [ -n "$ts" ]; then
        e=$(iso_to_epoch "$ts")
        [ -n "$e" ] && [ "$e" -gt "$max" ] && max=$e
    fi

    if [ "$max" = "0" ] && [ -n "$fallback" ]; then
        e=$(iso_to_epoch "$fallback")
        [ -n "$e" ] && max=$e
    fi

    echo "$max"
}

do_glab() {
    if [ "$DRY_RUN" = "1" ]; then
        log "DRY: glab $*"
        return 0
    fi
    glab "$@" >>"$LOG" 2>&1
}

token_line=$(/usr/bin/grep '^export GITLAB_TOKEN=' "$HOME/.zprofile" 2>/dev/null || true)
[ -n "$token_line" ] && eval "$token_line"

log "=== run start (dry_run=$DRY_RUN) ==="

# CODEOWNERS default reviewers (line starting with `*`)
DEFAULT_REVIEWERS=""
for path in ".gitlab%2FCODEOWNERS" "CODEOWNERS" "docs%2FCODEOWNERS"; do
    co=$(glab api "projects/$REPO_ENC/repository/files/$path/raw?ref=main" 2>/dev/null || true)
    if [ -n "$co" ] && ! grep -q '^{"message"' <<<"$co"; then
        DEFAULT_REVIEWERS=$(grep -E '^\*[[:space:]]+' <<<"$co" \
            | head -1 \
            | sed 's/^\*[[:space:]]*//' \
            | tr -s ' \t' '\n' \
            | sed 's/^@//' \
            | grep -v '^$' \
            | paste -sd, -)
        log "CODEOWNERS@$path default reviewers: $DEFAULT_REVIEWERS"
        break
    fi
done
[ -z "$DEFAULT_REVIEWERS" ] && log "CODEOWNERS not found; reviewer auto-assign disabled"

mrs_json=$(glab mr list --repo "$REPO" --per-page 100 --output json 2>>"$LOG")
[ -z "$mrs_json" ] && { log "error: empty response from glab mr list"; exit 1; }

count_total=$(jq 'length' <<<"$mrs_json")
log "fetched $count_total open MRs"

now_epoch=$(date -u +%s)
labeled_high=0; labeled_medium=0; assignees_set=0; reviewers_set=0
nudged=0; staled=0; noop=0

while IFS=$'\t' read -r iid title author labels assignees reviewers created_at is_draft; do
    actions=""

    # 1. severity:: label if no labels
    if [ -z "$labels" ]; then
        if printf '%s' "$title" | grep -q "NDS"; then
            new_label="severity::high"
        else
            new_label="severity::medium"
        fi
        if do_glab mr update "$iid" --repo "$REPO" --label "$new_label"; then
            log "labeled !$iid $new_label — $title"
            [ "$new_label" = "severity::high" ] && labeled_high=$((labeled_high+1)) || labeled_medium=$((labeled_medium+1))
            labels="$new_label"
            actions+="label "
        fi
    fi

    # 2. assignee = author if none
    if [ -z "$assignees" ] && [ -n "$author" ]; then
        if do_glab mr update "$iid" --repo "$REPO" --assignee "$author"; then
            log "assigned !$iid → @$author"
            assignees_set=$((assignees_set+1))
            actions+="assignee "
        fi
    fi

    # 3. reviewers from CODEOWNERS if none (excluding author)
    if [ -z "$reviewers" ] && [ -n "$DEFAULT_REVIEWERS" ]; then
        rev_list=$(echo "$DEFAULT_REVIEWERS" | tr ',' '\n' | grep -v "^${author}$" | paste -sd, -)
        if [ -n "$rev_list" ]; then
            if do_glab mr update "$iid" --repo "$REPO" --reviewer "$rev_list"; then
                log "reviewers !$iid → $rev_list"
                reviewers_set=$((reviewers_set+1))
                actions+="reviewer "
            fi
        fi
    fi

    # age since last *meaningful* activity (commits + non-bot human comments only)
    age_days=0
    act_epoch=$(last_activity_epoch "$iid" "$created_at")
    [ -n "$act_epoch" ] && [ "$act_epoch" -gt 0 ] && age_days=$(( (now_epoch - act_epoch) / 86400 ))

    # 4. stale + draft at 7+ days
    if [ "$age_days" -ge 7 ] && ! has_label "$labels" "stale"; then
        ok=1
        if [ "$is_draft" != "true" ]; then
            do_glab mr update "$iid" --repo "$REPO" --draft || ok=0
        fi
        do_glab mr update "$iid" --repo "$REPO" --label "stale" || ok=0
        if [ "$ok" = "1" ]; then
            log "STALE !$iid — ${age_days}d inactive — $title"
            staled=$((staled+1))
            actions+="stale "
        fi
    # 5. 5-day nudge comment + nudged-5d label
    elif [ "$age_days" -ge 5 ] && ! has_label "$labels" "nudged-5d" && [ -n "$author" ]; then
        msg="@${author} — this MR has been open with no activity for ${age_days} days. Pinging the owner for a status update.

_Auto-comment from svc-chatbot-mr-labels cron._"
        if do_glab mr note "$iid" --repo "$REPO" -m "$msg" \
           && do_glab mr update "$iid" --repo "$REPO" --label "nudged-5d"; then
            log "NUDGED !$iid — ${age_days}d inactive — @$author"
            nudged=$((nudged+1))
            actions+="nudge "
        fi
    fi

    [ -z "$actions" ] && noop=$((noop+1))

done < <(jq -r '.[] | [.iid, .title, .author.username,
                       (.labels | join(",")),
                       (.assignees | map(.username) | join(",")),
                       (.reviewers | map(.username) | join(",")),
                       .created_at,
                       (.draft // false | tostring)] | @tsv' <<<"$mrs_json")

log "done: high=$labeled_high medium=$labeled_medium assignees=$assignees_set reviewers=$reviewers_set nudged=$nudged staled=$staled noop=$noop"
