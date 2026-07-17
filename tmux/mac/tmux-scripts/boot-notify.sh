#!/usr/bin/env bash
# boot-notify.sh — on boot, post a Slack message to #nexus saying the box rebooted:
# how long it had been up, how long it was down, whether the shutdown was clean, and
# the LAST crash-breadcrumb reading (the machine's state at the moment it died).
#
# Deliberately bridge-INDEPENDENT: it posts straight to Slack via the bot token from
# Doppler (nexus/prd), the same source slack-bridge uses. A reboot notifier must not
# depend on the bridge, since the bridge is one of the things that goes down with the box.
#
# Companion to crash-breadcrumb.sh — together they turn a dead PuTTY ("network error")
# into a clear "nexus rebooted at HH:MM, cool+idle at death" notification.
# Force a test post (bypass the uptime guard): BOOT_NOTIFY_FORCE=1 boot-notify.sh
#
# Lives in the shared tmux-scripts/ dir but is Linux-only (reads /proc/uptime + journalctl).
# Guarded to a no-op on mac (mac install.sh would symlink it in); only the Linux systemd unit
# (boot-notify.service) runs it. The uptime guard below would also skip on mac, but exiting on
# $OSTYPE is explicit and avoids shelling journalctl/uptime that don't exist there.
set -u
case "$OSTYPE" in linux*) ;; *) exit 0 ;; esac

BREADCRUMB="${CRASH_BREADCRUMB_LOG:-$HOME/.tmux/crash-breadcrumb.log}"
LOGFILE="$HOME/.tmux/boot-notify.log"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOGFILE"; }

# --- Guard: only announce when genuinely near a boot (uptime < 10 min) ---
up_secs=$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 9999)
if [ "${BOOT_NOTIFY_FORCE:-0}" != 1 ] && [ "${up_secs:-9999}" -gt 600 ]; then
  log "skip: uptime ${up_secs}s > 600s (not a fresh boot)"; exit 0
fi

fmt_dur() { local s=$1
  if   [ "$s" -ge 3600 ]; then printf '%dh%dm' $((s/3600)) $(((s%3600)/60))
  elif [ "$s" -ge 60 ];   then printf '%dm%ds' $((s/60)) $((s%60))
  else printf '%ds' "$s"; fi; }

# --- Boot facts ---
boot_epoch=$(date -d "$(uptime -s)" +%s 2>/dev/null || echo 0)
boot_disp=$(date -u -d "@$boot_epoch" '+%Y-%m-%d %H:%M' 2>/dev/null)

prev_line="$(journalctl --list-boots --no-pager 2>/dev/null | awk '$1=="-1"')"
prev_start_epoch=$(date -d "$(awk '{print $4" "$5}' <<<"$prev_line")" +%s 2>/dev/null || echo 0)
prev_end_epoch=$(date -d "$(awk '{print $8" "$9}' <<<"$prev_line")" +%s 2>/dev/null || echo 0)

prev_uptime="?"; downtime="?"
[ "$prev_end_epoch" -gt "$prev_start_epoch" ] && prev_uptime="$(fmt_dur $((prev_end_epoch-prev_start_epoch)))"
[ "$prev_end_epoch" -gt 0 ] && [ "$boot_epoch" -gt "$prev_end_epoch" ] && downtime="$(fmt_dur $((boot_epoch-prev_end_epoch)))"

# Clean vs unclean: did the previous boot log a shutdown/reboot target?
if journalctl -b -1 --no-pager 2>/dev/null | grep -qE 'Reached target.*(Shutdown|Reboot|Power-Off)|systemd-shutdown|Powering off|Rebooting'; then
  how="clean shutdown/reboot"
else
  how="*unclean* — no shutdown logged (power-off or hard hang)"
fi

# Last breadcrumb from BEFORE this boot — filter by timestamp so a fresh post-boot
# line the logger just wrote can't be mistaken for the state at death.
last_pre=""
while IFS= read -r line; do
  e=$(date -d "${line%% *}" +%s 2>/dev/null) || continue
  [ "$e" -lt "$boot_epoch" ] && last_pre="$line"
done < <(tail -n 200 "$BREADCRUMB" 2>/dev/null)
[ -n "$last_pre" ] || last_pre="(no pre-crash breadcrumb captured)"

# At-a-glance verdict from the death-state temperature
tctl_n=$(grep -oE 'tctl=[0-9]+' <<<"$last_pre" | grep -oE '[0-9]+' | head -1)
verdict=""
if [ -n "${tctl_n:-}" ]; then
  if [ "$tctl_n" -lt 65 ]; then verdict=":snowflake: cool (${tctl_n}C) at death → points to power / deep-idle, not heat"
  else verdict=":fire: warm (${tctl_n}C) at death → check thermal/load"; fi
fi

MSG=":warning: *nexus rebooted* — back up ${boot_disp} UTC
• was up *${prev_uptime}*, down ~*${downtime}* · ${how}"
[ -n "$verdict" ] && MSG="${MSG}
• ${verdict}"
MSG="${MSG}
• last breadcrumb before death:
\`${last_pre}\`"

# --- Post directly via Slack bot token (Doppler nexus/prd), with brief retries ---
post() {
  doppler run -p nexus -c prd -- bash -c '
    payload=$(jq -n --arg c "$SLACK_NEXUS_CHANNEL" --arg t "$1" "{channel:\$c, text:\$t, unfurl_links:false, unfurl_media:false}")
    curl -fsS -X POST https://slack.com/api/chat.postMessage \
      -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
      -H "Content-type: application/json; charset=utf-8" \
      --data "$payload"
  ' _ "$MSG"
}

ok=0
for i in 1 2 3 4 5; do
  resp="$(post 2>&1)" && grep -q '"ok":true' <<<"$resp" && { ok=1; break; }
  log "attempt $i failed: $(printf '%s' "$resp" | tr -d '\n' | tail -c 200)"
  sleep 5
done
if [ "$ok" = 1 ]; then log "posted reboot notice (up=$prev_uptime down=$downtime; $how)"
else log "GAVE UP posting reboot notice after 5 attempts"; fi
exit 0
