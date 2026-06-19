# Overseer — idle-agent reaper + Slack hygiene

The "overseer" is not a single daemon. By design it's **split by concern** so the
core (reaping) works even when Slack isn't installed:

| Concern | Where it lives | Trigger |
|---|---|---|
| Close agents idle >4h (checkpoint first) | `scripts/overseer-reap.sh` | scheduled timer (opt-in) |
| Prune stale Slack cards answered in the CLI | `slack-bridge/index.js` (`pruneStaleCards`) | `setInterval` in the bridge |
| Route unaddressed Slack messages to an agent | `slack-bridge/index.js` | inbound message handler |

Slack-dependent features live in the bridge (opt-in peripheral); the reaper is
standalone and Slack-independent.

---

## The reaper (`scripts/overseer-reap.sh`)

Every run it scans `~/.tmux/registry/*` and, for each agent that is **finished
(`@waiting=2`) or stuck waiting on input (`@waiting=1`)** and has been **idle
≥ `REAP_IDLE_SECS` (default 4h)**, it:

1. Runs a **final memory checkpoint** over the agent's newest transcript
   (`scripts/checkpoint-transcript.sh` — the same haiku curator the auto-checkpoint
   Stop hook uses), then
2. **closes the window** (`tmux kill-window`). The existing pane-died hooks
   (`agent-deregister.sh` + `worktree-cleanup.sh`) handle registry + worktree cleanup.

Idle time = `now − @last_tool` (set on every tool use), falling back to the
registry `AT` if `@last_tool` is unset. Actively-working agents (`@waiting=0`)
and dead panes are skipped.

### What it will NEVER reap (the command post)

The window you drive from must never be closed under you. It is protected four ways:

1. **By name** — windows named `overseer` or `orchestrator` are always skipped.
2. **By tag** — any window with `@orchestrator` set to `1` (the `overseer`
   launcher self-tags; you can tag any window: `tmux set-option -w @orchestrator 1`).
3. **Attached + viewed** — the window an attached client is currently looking at.
4. **Exclude list** — anything in `$REAP_EXCLUDE` (csv of names/panes) or
   `~/.tmux/overseer-exclude` (one name or `%pane` per line, `#` comments allowed).

### The dedicated overseer window

Run `overseer` (shell function in zshrc/bashrc) to open — or jump to — a
dedicated command-post window. It registers as `overseer`, self-tags
`@orchestrator`, and is therefore exempt from reaping. Drive your fleet from
here; the agents you spawn are separate, reapable windows.

### Config (env)

| Var | Default | Meaning |
|---|---|---|
| `REAP_IDLE_SECS` | `14400` (4h) | idle threshold |
| `REAP_DRY_RUN` | `0` | `1` = log decisions, close nothing |
| `REAP_EXCLUDE` | _(empty)_ | csv of extra names/panes to protect |
| `REAP_ALL` | `0` | `1` = prune everything idle, command post included (see below) |
| `TMUX_SESSION` | `agents` | session to scan |

### `REAP_ALL` — unattended "leave it for days" boxes

By default the reaper protects your command post (Mac: you're actively driving
it). For a box that just sits idle — the personal Linux mini-pc — set
`REAP_ALL=1` to prune **everything** idle, command post included, so the whole
box gets checkpointed and cleaned without manual hygiene. It still honors
`~/.tmux/overseer-exclude` / `$REAP_EXCLUDE`, and still won't yank a window an
attached client is actively viewing (so an SSH session you're looking at is
safe; everything gets cleaned once you detach and it goes idle).

**The Linux systemd unit sets `REAP_ALL=1` by default; the Mac launchd job does
not** (Mac = active driver, command post protected).

Decisions + actions are logged to `~/.tmux/overseer-reap.log`; each reap also
appends a `reap` event to `~/.tmux/apm.log`.

### Try it safely first

```bash
task overseer:reap:dry          # log what WOULD be closed, change nothing
REAP_IDLE_SECS=0 task overseer:reap:dry   # ...treating every idle agent as over-threshold
cat ~/.tmux/overseer-reap.log
```

### Enable the schedule (opt-in — it closes windows)

Units live under `optional/` so the normal installers do **not** turn them on.

```bash
# macOS (launchd) — every 15 min
task launchd:install:overseer-reap
# uninstall: task launchd:uninstall:overseer-reap

# Linux (systemd user timer) — every 15 min
NEXUS_DIR="$(pwd)"
for u in overseer-reap.service overseer-reap.timer; do
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$NEXUS_DIR|g" \
    tmux/linux/systemd/optional/$u > ~/.config/systemd/user/$u
done
systemctl --user daemon-reload
systemctl --user enable --now overseer-reap.timer
# disable: systemctl --user disable --now overseer-reap.timer
```

**Before enabling, make sure your command post is protected** — open it with
`overseer`, or tag your current window: `tmux set-option -w @orchestrator 1`.

---

## Slack card pruning

The bridge posts a card when an agent needs input. Cards answered **in Slack**
self-resolve, but a prompt answered **locally in the CLI** (PreToolUse clears the
pane's `@wait_since`) or an agent whose window closed used to leave the card
orphaned until its 7-day TTL — the "12 stale prompts in a thread" problem.

`pruneStaleCards()` runs every `SLACK_PRUNE_INTERVAL_MS` (default 10s) and applies
the same staleness test as the same-prompt guard across all tracked cards,
deleting the dead ones (and the agent anchor when its last pending card clears).
No config needed; it's on whenever the bridge has a channel configured.
