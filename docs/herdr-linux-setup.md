# herdr on the personal Linux stack — install & setup runbook

Bring the herdr substrate to the personal Linux mini-pc (the "nexus" box: systemd + GitHub,
runs from `tmux/linux/`), at parity with the mac. **herdr is the repo DEFAULT now (flipped
2026-07-16)** — you no longer need a per-machine `NEXUS_SUBSTRATE=herdr` flip; a fresh install
runs herdr out of the box. tmux is a deprecated, flag-selectable fallback: set
`NEXUS_SUBSTRATE=tmux` (unit env or `~/.tmux/env.sh`) only to keep a box on the legacy backend.

**TL;DR (repo-side artifacts are now committed).** On the box: `git pull` then
`bash tmux/linux/install.sh` — this links the shared herdr scripts, installs the ported Linux
`open-claude.sh`, and (if `herdr` is installed) deploys the herdr config + pickers +
`workspace-categories.txt`. Then only: **Step 1** (install herdr), **Step 5** (install
`substrated.service` from `systemd/optional/` + set the local `NEXUS_SUBSTRATE=herdr` flip on
arbiter/bus/reaper), and **Step 6** (verify). Steps 2–4 below are the detail of what the pull
already did.

## The good news: most of it ports for free

`tmux/linux/install.sh` symlinks the **shared** tmux-scripts from `tmux/mac/tmux-scripts/`
(lines 21–30), overriding only platform-specific ones. So all the herdr-aware bash —
`substrate.sh`, `agent-resolve.sh`, `agent-send.sh`, `launch-claude.sh`, `agent-registry.sh`,
`scripts/overseer-reap.sh` — reaches Linux automatically on a `git pull` + reinstall. The
Linux-specific work is narrow (below).

## Prereqs
- agents-nexus checked out on the box (GitHub remote), on the branch with the workspace work.
- The nexus stack already running under systemd (`slack-bridge.service`, `arbiter.service`,
  the tmux `agents` session) — this doc adds herdr alongside it.
- Repo-root `.env` present (DATABASE_URL, SLACK_*, tokens) — same as mac.

---

## Step 1 — Install herdr

```bash
# Preferred: the same version pinned on mac (check `herdr --version` on mac first).
curl -fsSL https://herdr.dev/install.sh | sh     # or the distro package, if one exists
herdr --version                                   # confirm it matches the mac pin
# Headless server as a user service (Step 5 wires this into systemd); quick smoke:
herdr server &>/tmp/herdr-server.log &  herdr status server | grep -E 'status|protocol'
```

## Step 2 — Pull + reinstall (gets the herdr-aware shared scripts)

```bash
cd <agents-nexus> && git pull
bash tmux/linux/install.sh
# Verify the shared herdr-aware scripts linked into ~/.tmux:
ls -l ~/.tmux/substrate.sh ~/.tmux/agent-send.sh ~/.tmux/launch-claude.sh
# agent-resolve.sh is new — confirm it linked (install globs *.sh from mac/tmux-scripts):
ls -l ~/.tmux/agent-resolve.sh || echo "MISSING — see note below"
# workspace-categories.txt is config/, NOT a tmux-script → symlink it by hand (like conductor.yaml):
ln -sfn "$PWD/config/workspace-categories.txt" ~/.tmux/workspace-categories.txt
```
> If `agent-resolve.sh` didn't link, the install glob only ran once for existing names — just
> `ln -sfn "$PWD/tmux/mac/tmux-scripts/agent-resolve.sh" ~/.tmux/agent-resolve.sh`.

## Step 3 — `open-claude.sh` / `hook-notification.sh` (now shared — nothing to reconcile)

✅ **DONE — the Linux overrides were folded into the shared mac scripts** behind `$OSTYPE`/
`[ -d ]` guards and deleted, so `open-claude.sh` and `hook-notification.sh` are now single
cross-platform copies with all the herdr work (HERDR_PANE_ID fold, seam-routed slot/rename,
`substrate register`, `.env` env-parity) — Linux inherits every future change for free via the
Step 2 symlink, same as the other shared scripts. What was Linux-specific is guarded inline:

- **open-claude.sh:** fnm's default-alias bin is prepended to PATH only when the dir exists
  (`[ -d "$HOME/.local/share/fnm/aliases/default/bin" ]` — a no-op on mac); the `.env` default
  path is `$OSTYPE`-selected (mac `~/repos` vs Linux `~/repos`), though `env.sh` normally
  sets `AGENTS_NEXUS_DIR` so the default rarely matters; `date`/`stat` already carry BSD-or-GNU
  fallbacks.
- **hook-notification.sh:** desktop notify is `$OSTYPE`-guarded — `osascript` on darwin,
  `notify-send` + terminal bell elsewhere.

So there is **no Step-3 reconcile work** anymore — `bash tmux/linux/install.sh` (Step 2) links
the shared versions. `tmux/linux/tmux-scripts/` is now **empty**: every script — including the
Linux-only `boot-notify.sh` / `crash-breadcrumb.sh` (sysfs / journalctl), which carry an
`$OSTYPE` guard so they no-op if ever linked on mac — lives in the one shared `tmux-scripts/`
dir. If you ever add a new platform difference, prefer an inline `$OSTYPE`/`[ -d ]` guard in the
shared script over resurrecting an override.

## Step 4 — Linux herdr config

Create `tmux/linux/herdr/` (mac's `config.toml` hardcodes mac `/Users/...` paths). Mirror
`tmux/mac/herdr/` with `__HOME__` placeholders the installer substitutes:
- `config.toml` — same keys (prefix `ctrl+a`, `focus_agent = prefix+1..9`, picker on
  `prefix+shift+n` → `nexus-pick.sh`, `prefix+shift+b` → `nexus-workspace-new.sh`, the `[ui]`
  sidebar block), but the `[[keys.command]]` paths point at `__HOME__/.config/herdr/*.sh`.
- `nexus-pick.sh` / `nexus-workspace-new.sh` — copies of the mac ones (they're already
  `$HOME`-relative; the only mac-ism is `/opt/homebrew/bin` in PATH → use the Linux herdr
  path, e.g. `/usr/local/bin` or `~/.local/bin`).
Deploy: symlink `config.toml`, `nexus-pick.sh`, `nexus-workspace-new.sh` into `~/.config/herdr/`
(the installer should do this; until it does, `ln -sfn` by hand). Apply with
`herdr server reload-config`.

## Step 5 — systemd units

**Install `substrated.service`** — committed in `tmux/linux/systemd/optional/` (opt-in, so a
tmux-default install doesn't auto-enable it). Substitute the placeholders the way `install.sh`
does for the auto units, then enable:
```bash
NODE_BIN="$(readlink -f "$(command -v node)")"
sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$PWD|g" -e "s|__NODE_BIN__|$NODE_BIN|g" \
  tmux/linux/systemd/optional/substrated.service > ~/.config/systemd/user/substrated.service
```
**Substrate flip — no longer required.** herdr is the repo default now, so arbiter /
slack-bridge / overseer-reap all come up on herdr with no unit env. You only need
`SUBSTRATED_PORT` if it differs from the 8422 default; set `Environment=NEXUS_SUBSTRATE=tmux`
on a unit *only* to pin that service to the legacy tmux backend. Then:
```bash
systemctl --user daemon-reload
systemctl --user enable --now substrated.service
systemctl --user restart arbiter.service slack-bridge.service
# overseer-reap is opt-in + destructive — enable deliberately, and dry-run first:
REAP_DRY_RUN=1 NEXUS_SUBSTRATE=herdr SUBSTRATED_PORT=8422 bash scripts/overseer-reap.sh
```

**Install `herdr-recover.timer`** — herdr **restart resilience** (opt-in), committed in
`tmux/linux/systemd/optional/`. A herdr server restart kills the in-pane agents and fires
no `pane-died` hook, so rosters go stale and agents are gone. On this **unattended** box the
unit is set to auto-**respawn** the whole fleet from their last checkpoints after a restart
(`HERDR_RECOVER_RESPAWN=1` + `HERDR_RECOVER_ALL=1` — the recovery analog of `REAP_ALL=1` on
the reaper). Full detail: `docs/herdr-recover.md`.
```bash
for u in herdr-recover.service herdr-recover.timer; do
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$PWD|g" \
    tmux/linux/systemd/optional/$u > ~/.config/systemd/user/$u
done
systemctl --user daemon-reload
systemctl --user enable --now herdr-recover.timer
# dry-run first to see what it WOULD reconcile/revive, change nothing:
HERDR_RECOVER_DRY_RUN=1 NEXUS_SUBSTRATE=herdr SUBSTRATED_PORT=8422 bash scripts/herdr-recover.sh --all
# on demand any time: nexus-recover [--respawn|--all]
```

> **The substrated daemon is a long-lived process.** A later `git pull` that changes
> `substrated/index.mjs` (e.g. the events.subscribe **push** layer) is not picked up until you
> `systemctl --user restart substrated.service` — the running node process holds the old code.
> (Same on mac: `launchctl kickstart -k gui/$(id -u)/com.agents-nexus.substrated`.) The daemon
> subscribes to herdr `agent_status` events for an instant idle-gate and reconciles the roster on
> the slow `SUBSTRATED_POLL_MS` poll (which is also the fallback when push is down); it
> self-reconnects the subscription on a herdr blip (`SUBSTRATED_RECONNECT_MS`, default 1000).

## Step 6 — Flip the interactive shell + verify

Add to `~/.tmux/env.sh` on the box (so the picker + your agents default to herdr locally):
```bash
export NEXUS_SUBSTRATE=herdr
export SUBSTRATED_PORT=8422
```

### Verification checklist
- `herdr status server` → running, protocol matches mac.
- `NEXUS_SUBSTRATE=herdr ~/.tmux/substrate.sh workspace-list` → prints (empty ok).
- `NEXUS_SUBSTRATE=herdr ~/.tmux/substrate.sh spawn probe ~ 'sleep 60' --workspace test/x --print`
  → agent lands in bucket `test/x`; then `~/.tmux/substrate.sh workspace-close test/x`.
- `ctrl+a N` in herdr → repo picker → bucket prompt appears.
- Spawn a real agent; its `~/.tmux/registry/<handle>` has `WORKSPACE=` + `SUBSTRATE=herdr`;
  `/checkpoint` writes to agent-memory (DATABASE_URL reached the MCP server → env parity works).
- `curl -sf 127.0.0.1:8422/health` → `{…"herdrConnected":true,"pushConnected":true}`.
  `pushConnected:true` means the events.subscribe push layer is live (state changes hit the
  idle-gate instantly, not on the poll interval). If it reads `false` while `herdrConnected:true`,
  push is down and the daemon is serving poll-only — check the herdr socket, then restart the unit.
- `curl -sf 127.0.0.1:8788/health` → bus healthy; post a bus message to the agent by name.

## Rollback to tmux (instant, per-machine)
herdr is the default, so rolling *back* to tmux is now the explicit opt-out: set
`NEXUS_SUBSTRATE=tmux` in `~/.tmux/env.sh` + the units and `systemctl --user restart`. Every
consumer reverts to the tmux backend (still fully present — the tmux code paths were kept as
exactly this fallback). No repo change needed.

## What's a real port vs free (current status)
| Piece | Status |
|---|---|
| Shared scripts (`substrate.sh`, `agent-resolve.sh`, `agent-send.sh`, `launch-claude.sh`, `agent-registry.sh`, `overseer-reap.sh`) | **Free** — symlinked from mac on reinstall |
| `open-claude.sh` + `hook-notification.sh` | **Free** — folded into the shared mac scripts behind `$OSTYPE` guards; the Linux overrides were deleted (Step 3) |
| `boot-notify.sh` + `crash-breadcrumb.sh` | **Free** — Linux-only logic, but moved into the shared `tmux-scripts/` dir behind an `$OSTYPE` guard (no-op on mac); run only by their systemd units |
| `tmux/linux/herdr/` config + pickers | ✅ **Committed** — `install.sh` deploys when `herdr` is present |
| `workspace-categories.txt` symlink | ✅ **Auto** — `install.sh` (when `herdr` present) |
| `substrated.service` (events.subscribe push daemon) | ✅ **Committed** in `systemd/optional/` — install deliberately (Step 5); `restart` on update to reload the daemon code |
| `herdr-recover.{service,timer}` (restart resilience) | ✅ **Committed** in `systemd/optional/` — opt-in (Step 5); on this box auto-respawns the whole fleet from checkpoints after a herdr/OS restart. `docs/herdr-recover.md` |
| `NEXUS_SUBSTRATE=herdr` flip on arbiter/bus/reaper | **Local** (Step 5) — per-machine, kept out of the repo |
| herdr install + headless server unit | **Install** (Step 1, 5) |
| Conductor missions (`conductor.py` is shared) | **Free** — a `--distribute` mission registers + `@orchestrator`-tags its detached orchestrator and registers + `@cohort`-tags each worker (all via the shared `substrate register`/`deregister` verbs, self-cleaning on exit), and reaches the missions DB via `conductor_db`'s `.env` fallback; no dependence on `open-claude.sh` |

Notes: the mixed-fleet + workspace-identity behavior all comes with the shared scripts. See
`docs/herdr-workflow.md` (workspace buckets, addressing grammar) and `docs/herdr-spike.md`
(the op-map). herdr enforces globally-unique agent names — same as mac. **Registry parity
(agenda #8, DONE for the fleet's own spawners):** every fleet-spawned herdr agent now writes a
`~/.tmux/registry/<handle>` file — picker launches (open-claude), the Conductor orchestrator, and
its workers — so the registry-driven reaper + `peers` see them on Linux too. Still TODO:
skill-owned `substrate spawn` tasks (`swarm-bg` etc.) register via the same `register`/`deregister`
verbs when they grow a headless worker.
