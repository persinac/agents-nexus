# Nexus Spontaneous Reboot — Diagnosis & Action Plan

**Host:** `nexus` — GEEKOM A7 Max, AMD Ryzen 9 7940HS (Zen4 Phoenix, Radeon 780M),
16 GB (12 GB usable after the iGPU carve-out), Ubuntu kernel `6.14.0-37-generic`,
BIOS AMI **1.26 (2025-09-15)**, **no UPS** (`/sys/class/power_supply` empty).

**Status (2026-06-22):** 🟡 Stable on a software stopgap (CPU boost disabled).
Root cause is **marginal power delivery under boost current transients**; the
definitive fix is hardware (DC brick) and is still pending. **Boost stays off
until the brick is replaced.**

> This supersedes the original "deep-idle C-state" theory in
> [mini-pc-setup.md → Phase 11](./mini-pc-setup.md#phase-11--stability-spontaneous-reboots-amd-deep-idle).
> That fix was applied and the box **still crashed** — so it is not the cause
> (see Timeline). The C-state cap is kept only as a harmless belt-and-suspenders.

## Symptom

Box hard-resets on its own — ~8–12 s downtime, then auto-power-on. The journal
ends mid-line with **no OOM / MCE / thermal / panic / oops**. Cool (42–64 °C) and
mostly idle at the moment of death. From SSH it looks like a "network error" (the
TCP session dies *with* the box — it's the box vanishing, not the LAN).

## Diagnosis timeline

| When | Finding |
|------|---------|
| Jun 19–21 | First clusters after ~10 days stable. Ruled out: OOM, MCE/EDAC, thermal, panic, amdgpu hang, watchdog, suspend. `sar` showed ~95% idle before each crash → hypothesis **AMD deep-idle C-state hang**; wall power a weak second. |
| Jun 21 | Mitigation applied: disabled C2/C3 + `processor.max_cstate=1` (GRUB); built `crash-breadcrumb` + `boot-notify`. |
| **Jun 22 05:30–05:47** | **3rd cluster — 6 hard resets in 17 min, with the C-state fix active** (cmdline has `max_cstate=1`, only POLL+C1 idle states exist, breadcrumb `c3us=0`). → **C-state hypothesis FALSIFIED.** |
| Jun 22 | **Differential:** 4 sibling mini-PCs on the *same power strip*, heavier 24/7 k8s load, but **Ryzen 5700-series (~15–25 W, flat draw)** — rock-solid 60+ days. nexus = 7940HS boosting to 5.26 GHz = **violent current transients (di/dt)**. Same wall, only the high-transient box resets. Barrel/connection verified solid. → **wall power exonerated; root cause = nexus's own high-transient power path (DC brick / board VRM / AGESA boost).** |
| Jun 22 ~06:14 | Stopgap: **disabled CPU boost** (`cpu-boost-off.service`). Box then held **12 h+ including the full-fleet restore (loadavg → 1.7)** with zero resets. |

Two death signatures from `~/.tmux/crash-breadcrumb.log`, both **load-independent**:
died at `ppt=20 W` *rising* (docker stack spin-up) **and** at `ppt=6 W` idle —
instant reset regardless of load = electrical instability, not a thermal/load
threshold.

## Current mitigation (in place)

| Lever | State | Notes |
|-------|-------|-------|
| CPU boost disabled | `cpu-boost-off.service` enabled+active; `/sys/devices/system/cpu/cpufreq/boost=0` | Pins ~base clock, removes the boost current transients. **This is what's keeping it up.** |
| Deep C-states capped | `processor.max_cstate=1` (GRUB cmdline) | Carried over from the earlier round; harmless, and survives a BIOS flash (it's a kernel param). |

```bash
# Revert the boost cap (only after the hardware fix is confirmed):
sudo systemctl disable --now cpu-boost-off.service   # its ExecStop re-enables boost
# or live: echo 1 | sudo tee /sys/devices/system/cpu/cpufreq/boost
```

Cost of the stopgap: CPU capped at base clock — agents/LLM run slower, but the box
stays alive.

## Action plan (ordered by leverage / risk)

1. **DC brick swap — the definitive fix.** Replace or borrow a **19 V, ≥120 W**
   adapter with the matching barrel (OEM spec: **19 V⎓6.32 A, 120 W**).
   ⚠️ Sibling 5700U bricks may be < 120 W — check the label, or an under-spec brick
   muddies the test.
   - Stable on a known-good brick → the brick was failing → buy a replacement.
   - Still resets on a known-good brick → internal (board VRM / RAM) → reseat RAM,
     then consider RMA.
2. **BIOS update from 1.26** — tunes Phoenix boost/load-line behavior. Do this
   *after* the brick swap (see warning below). Procedure in the next section.
3. **UPS** — definitive wall-power test, ongoing protection, and it makes a BIOS
   flash safe.
4. **Re-enable boost and confirm** once the hardware fix is in and the box has been
   stable for a few days under real load.

> ⚠️ **Do not flash the BIOS while the box is unstable.** A power cut mid-flash
> permanently bricks the board, and `cpu-boost-off.service` (a Linux unit) does
> **not** apply in the EFI-shell flash environment. Swap the brick first; flash
> only when stable, ideally on a UPS.

### BIOS update procedure (GEEKOM — EFI-shell, no Windows needed)

`fwupd`/LVFS is unavailable (GEEKOM doesn't publish there), so it's a manual flash:

1. **Get the file** from GEEKOM support — it's gated behind a ticket. Provide the
   exact model + serial + current BIOS 1.26 + the reset symptom. One file flashes
   **BIOS + EC** together. Confirm it is newer than 1.26.
2. **FAT32 USB** — copy the package to the root, including the `efi/` and `shell/`
   folders and `Startup.nsh` (that's what auto-runs in the EFI shell). Nothing else
   on the stick.
3. **Del** at boot → Security → **disable Secure Boot** → save.
4. **F7** boot menu → select the USB → the auto-flash runs.
   **Do not cut power or pull the USB.**
5. **Del** → re-enable Secure Boot → save. Verify: `sudo dmidecode -t0 | grep -i version`.

- BIOS settings reset to defaults afterward; our fixes are Linux-side (GRUB param +
  systemd unit) and survive untouched.
- If a **Power Supply Idle Control = Typical Current Idle** option appears
  post-update, set it (firmware-grade idle-reset hardening).

Refs: [GEEKOM — How to update BIOS/EC](https://help.geekompc.com/hc/en-us/articles/10949109335695-How-to-update-BIOS-or-EC),
[A7-series BIOS instructions](https://help.geekompc.com/hc/en-us/articles/15277457953295-A5-Pro-A6-A7-A8-AE7-AE8-AX7-Pro-AX8-Pro-BIOS-Setup-Instructions).

## Recovery runbook — restoring agents after a crash

A hard reboot kills the tmux panes but **not** the Claude transcripts — every turn
is fsync'd to `~/.claude/projects/<project-slug>/<session-uuid>.jsonl`, which
survives a power cut. To bring an agent back with its context:

```bash
# 1) Latest non-empty transcript for the repo (ignores 0-byte post-reboot stubs):
pd=~/.claude/projects/-home-persinac-repos-<project-slug>
ls -1t "$pd"/*.jsonl | while read -r f; do [ -s "$f" ] && { echo "$f"; break; }; done

# 2) Open a fleet window at the NEXT FREE index.
#    (Plain `tmux new-window -t agents` tries to reuse the current index and fails
#     with "index N in use" — compute the next index explicitly.)
name=<repo>; cwd=~/repos/flashback-fleet/$name; uuid=<from step 1>
next=$(( $(tmux list-windows -t agents -F '#{window_index}' | sort -n | tail -1) + 1 ))
read pane slot < <(tmux new-window -d -t "agents:$next" -c "$cwd" -n "$name" \
                     -P -F '#{pane_id} #{window_index}')

# 3) Register in the fleet (mirrors open-claude.sh):
printf 'SLOT=%s\nNAME=%s\nCWD=%s\nAT=%s\nPANE_ID=%s\n' \
  "$slot" "$name" "$cwd" "$(date +%s)" "$pane" > ~/.tmux/registry/$pane

# 4) Resume — MUST run in the project cwd (sessions are keyed by project dir):
tmux send-keys -t "$pane" \
  'source ~/.tmux/env.sh; claude --resume '"$uuid"' --name '"$name"' --model "$CLAUDE_MODEL" --effort "$CLAUDE_EFFORT"' Enter
```

`claude --resume` parks at a prompt before loading:

- **1 = Resume from summary** (recommended) — cheap; the agent gets the gist.
- **2 = Resume full session as-is** — costs the session's *entire* token count
  (~120–270 k each in the Jun 22 incident, ~900 k for six). The full transcript
  stays on disk either way, so summary is the sensible default and you can
  full-resume a specific agent later if it needs the detail.

> **A2A heads-up:** an agent resumed straight to an idle prompt never fires its
> Stop hook, so its `@waiting` stays **unset**, and the Slack bus (idle-gated
> delivery) defers messages to it *indefinitely*. After the fleet settles, check
> each with `tmux show-options -wqv -t <pane> @waiting` — anything not `2` won't
> receive bus traffic. Unstick it with `tmux set-option -w -t <pane> @waiting 2`
> (the bridge's flush delivers within ~4 s), or just send the agent one message so
> it runs a turn and self-sets.

## Verification / how to read it

- **`boot-notify` → Slack `#nexus`** posts on every crash-reboot. **Silence = good.**
- **`~/.tmux/crash-breadcrumb.log`** — the last line is the box's state at death:
  cool + idle → power; hot + busy → thermal.
- `uptime` and `journalctl --list-boots` — confirm a single unbroken boot.

## Memory (agent-memory stack, project `agents-nexus`)

Diagnosis `62d9f15bdfd9` · boost-off mitigation `4b8ebacb5d43` · 11 h hold
`7ee8629f210f` · recovery roster `38c29c5b080c` · restore + load-test `1ca020d0bb99`
· session checkpoint `74dde492283d`.
