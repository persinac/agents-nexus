#!/usr/bin/env bash
# crash-breadcrumb.sh — append an fsync'd temp/power/load snapshot every INTERVAL
# seconds. A spontaneous hardware power-off leaves NO kernel logs, so the LAST line
# in this file becomes the only witness to the machine's state at the moment of death:
# was it cool+idle (→ power/wall-power or deep-idle hang) or hot+busy (→ thermal)?
#
# Diagnostic for `nexus` (GEEKOM A7 Max) spontaneous IDLE reboots.
# See agent-memory note fe90c4cfd0e1 (project agents-nexus).
#
# Lives in the shared tmux-scripts/ dir (one canonical location) but is Linux-only — it reads
# /proc, /sys/class/hwmon, journalctl. Guarded so it's an instant no-op if ever invoked on mac
# (where mac install.sh would symlink it in): CRITICAL here because the body is an infinite
# loop. Only the Linux systemd unit (crash-breadcrumb.service) actually runs it.
set -u
case "$OSTYPE" in linux*) ;; *) exit 0 ;; esac

LOG="${CRASH_BREADCRUMB_LOG:-$HOME/.tmux/crash-breadcrumb.log}"
INTERVAL="${CRASH_BREADCRUMB_INTERVAL:-20}"
MAXBYTES="${CRASH_BREADCRUMB_MAXBYTES:-10485760}"   # 10 MiB, then keep last 5000 lines

# Read a hwmon value by sensor name (hwmon numbering is not stable across boots).
hwmon() {  # $1=name $2=file -> raw value, or empty
  local h
  for h in /sys/class/hwmon/hwmon*; do
    [ "$(cat "$h/name" 2>/dev/null)" = "$1" ] || continue
    cat "$h/$2" 2>/dev/null && return 0
  done
}

snap() {
  local tctl edge ppt cidle
  tctl=$(hwmon k10temp temp1_input)        # CPU die (Tctl), milli-°C
  edge=$(hwmon amdgpu temp1_input)         # iGPU edge, milli-°C
  ppt=$(hwmon amdgpu power1_average)        # APU socket power, micro-W
  # deepest-idle (C3) residency in usec — rising while "idle" confirms deep-idle entry
  cidle=$(cat /sys/devices/system/cpu/cpu0/cpuidle/state3/time 2>/dev/null || echo 0)
  printf '%s tctl=%sC gpu=%sC ppt=%sW load=%s mem=%s c3us=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$(( ${tctl:-0} / 1000 ))" "$(( ${edge:-0} / 1000 ))" "$(( ${ppt:-0} / 1000000 ))" \
    "$(cut -d' ' -f1-3 /proc/loadavg)" \
    "$(awk '/MemAvailable/{a=$2} END{printf "%dMavail", a/1024}' /proc/meminfo)" \
    "${cidle:-0}"
}

mkdir -p "$(dirname "$LOG")"
while :; do
  snap >>"$LOG"
  # Force the line to disk NOW so a power cut can't lose it from the page cache.
  sync -d "$LOG" 2>/dev/null || sync
  if [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt "$MAXBYTES" ]; then
    tail -n 5000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
  fi
  sleep "$INTERVAL"
done
