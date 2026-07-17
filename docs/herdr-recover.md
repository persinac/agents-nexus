# herdr restart resilience — `scripts/herdr-recover.sh`

A herdr **server** restart (`brew services restart herdr` / a launchd/systemd
KeepAlive respawn / a crash) is not transparent to the fleet:

- the in-pane `claude` **processes die** — herdr may keep a pane shell, but the REPL
  inside is gone;
- **pane ids churn** (`w6` → `wA`), so stored handles point at nothing;
- and — unlike tmux — herdr fires **no `pane-died` hook**, so `agent-deregister`
  never runs. The `~/.tmux/registry/*` entries, the `agent-ledger`, and the
  `substrated` cache all go **stale**, and the agents are simply gone with nothing to
  bring them back.

`herdr-recover.sh` is the herdr counterpart of the tmux `pane-died` hook, plus
recovery. It is **herdr-only** (a no-op under `NEXUS_SUBSTRATE=tmux`, where the
`pane-died` hook already handles dereg + worktree cleanup), Slack-independent,
idempotent, and fail-open — the same discipline as the reaper.

## What it does

For every registry entry whose herdr pane is **dead**:

1. **Reconcile (always, safe).** Downgrade `live` ledger entries that no longer have a
   backing window (`agent-ledger reconcile`) so the rosters match reality, and print a
   **recovery plan** — what was lost, and how to bring it back. No windows are opened
   or closed.
2. **Recover (opt-in).** Respawn the casualty from its **last checkpoint** via
   `open-claude.sh` — which re-injects that repo's recent checkpoint notes + memory
   recall — into its **original cwd + workspace** (read from the stale registry entry,
   which still holds them; the workspace is recreated by *label*, so the fleet comes
   back organized as it was). Enable with `--respawn` / `HERDR_RECOVER_RESPAWN=1`.

## Respawn scope (who comes back)

Respawn is deliberately conservative:

| Agent | `--respawn` | `--all` |
|---|---|---|
| ledger-tracked `live` (orchestrator / Conductor fleet) | ✅ revived | ✅ |
| untracked (interactive picker agent) | ⏭️ left closed | ✅ revived |
| ledger `dormant` (the reaper closed it on purpose) | 🚫 never | 🚫 never |
| command post (`overseer`/`orchestrator`, `@orchestrator`) | 🚫 never | 🚫 never |
| in `~/.tmux/overseer-exclude` / `$REAP_EXCLUDE` | 🚫 never | 🚫 never |
| pane still alive | ⏭️ skipped (still running) | ⏭️ |

The default `--respawn` scope is the ledger-tracked fleet because those agents
self-deregister on a *clean* exit (their `finally`) and the reaper deregisters what it
kills — so a lingering ledger-`live` entry with a dead pane is genuinely a restart
victim. Untracked picker agents have no such signal, so they're only revived under
`--all` (the unattended-box "bring the whole fleet back").

## Usage

```bash
nexus-recover                 # reconcile rosters + print what was lost (no respawn)
nexus-recover --respawn       # also revive the ledger-tracked fleet from checkpoints
nexus-recover --all           # revive EVERY non-excluded casualty (implies --respawn)
nexus-recover --dry-run --respawn   # log decisions, change nothing
# or the Task equivalents:
task herdr:recover · task herdr:recover:respawn · task herdr:recover:dry
```

Decisions + actions log to `~/.tmux/herdr-recover.log`.

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `HERDR_RECOVER_RESPAWN` | `0` | `1` = respawn ledger-tracked casualties (same as `--respawn`) |
| `HERDR_RECOVER_ALL` | `0` | `1` = respawn every non-excluded casualty (same as `--all`) |
| `HERDR_RECOVER_DRY_RUN` | `0` | `1` = log decisions, change nothing |
| `HERDR_RECOVER_SEED` | _(built-in)_ | the restart-restore instruction seeded into each revived agent |
| `REAP_EXCLUDE` | _(empty)_ | csv of names/panes to never revive (shared with the reaper) |

## Running it automatically (opt-in)

Units live under `optional/` so a normal install never auto-enables them. **Installing
one presumes the box is cut over to herdr** (the tool no-ops otherwise).

- **macOS (launchd)** — reconcile-only, every 10 min + at load. Respawn stays
  on-demand here since the mac is attended:
  ```bash
  task launchd:install:herdr-recover      # uninstall: task launchd:uninstall:herdr-recover
  ```
- **Linux nexus box (systemd)** — the box is unattended for days, so its unit sets
  `HERDR_RECOVER_RESPAWN=1` + `HERDR_RECOVER_ALL=1` (mirrors `REAP_ALL=1` on the reaper
  there): after a restart it brings the **whole** fleet back. See
  `docs/herdr-linux-setup.md` Step 5.

## Related

- **The reaper** (`docs/overseer.md`) is the mirror image — it *closes* idle agents
  (checkpoint first). Reaper-closed agents are ledger-`dormant`, which recovery never
  revives. The reaper now also `substrate deregister`s a herdr agent it kills (the
  `pane-died` hook won't), so a reaped agent doesn't linger as a recovery candidate.
- **The ledger** (`scripts/agent-ledger.py`) is the shared source of truth for "which
  agents are live vs. dormant"; both the reaper and recovery read/write it.
