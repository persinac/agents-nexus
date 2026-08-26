# nexus-presence plugin

A **desktop notification** when a herdr agent goes `blocked` — i.e. it needs
your input (a permission prompt, an elicitation dialog, an end-of-turn
question) — **and is still blocked once the settle window expires.**

Event-driven: **no keybinding, no daemon, no polling.** herdr fires the plugin's
`pane.agent_status_changed` hook natively; the hook filters to the `blocked`
transition, defers, re-reads the pane's status, and pops an OS notification only
if the agent is still waiting on you.

## The settle window

On a box running the nexus Notification hook (`~/.tmux/hook-notification.sh` →
`notify-classify.py`), most permission prompts are **auto-approved by the
classifier within ~0.4–3s** and never reach a human. herdr still sees a real
`blocked` edge for each one, so notifying on the edge itself pinged you for
prompts that were already handled.

Measured on one box before this was added — this plugin's own `presence.log`
joined against `~/.tmux/auto-approve.log`:

| Window | Toasts | Classifier already took it | Human genuinely owed |
|---|---|---|---|
| 3 days | 1,596 | **82.0%** | 5.5% |
| 7 days | 4,677 | **77.1%** | 6.8% |

So the hook now waits, then re-reads the pane with `herdr agent get` and
notifies only if it is *still* `blocked` — the same semantics herdr documents for
its own `[ui.toast] delay_seconds`. If the status can't be read it **fails open**
and notifies: a spurious ping is a nuisance, a swallowed one strands an agent.

Two knobs, both optional, both read from the `~/.tmux` env layer:

| Variable | Default | Effect |
|---|---|---|
| `NEXUS_PRESENCE_DELAY_SECS` | `15` | Settle window. `0` disables the wait **and** the re-check (every blocked edge notifies, as before this change). |
| `NEXUS_PRESENCE_COOLDOWN_SECS` | = delay | Minimum seconds between toasts for one pane. Coalesces a pane that flaps `blocked→working→blocked` once per auto-approved tool call. |

The two are independent: `delay=0` with an explicit cooldown is valid (no
classifier on the box, but still no burst spam).

`0` means "no settle", not "synchronous" — the worker is always detached, so the
toast lands a beat later regardless. That is deliberate: herdr **supervises**
plugin commands (it waits for exit and records start/finish — see `herdr plugin
log list --plugin nexus.presence`), so the hook does nothing but gate and spawn,
returning in ~60ms instead of the ~1s it took before. The detached worker does
the waiting, the env sourcing (~945ms of that old ~1s, and the real reason it
moved off the hook), the status re-check, and the notify.

## Why the desktop channel — and when it IS redundant

The full nexus stack (`substrated` + `slack-bridge`) already subscribes to
`pane.agent_status_changed` and posts a Slack card to `#nexus` when an agent
blocks. Presence uses a **different** channel — the desktop — so it adds a signal
rather than repeating that one. Its real value is as the zero-infra path: a
teammate running herdr + this plugin gets blocked-alerts with none of the
daemon/bridge stack.

**On a box that also runs the nexus Notification hook, this plugin is redundant —
turn it off.** `~/.tmux/hook-notification.sh` already fires a desktop toast
("Claude Code … needs input") and it is *classifier-gated*: it exits before
notifying when `notify-classify.py` auto-approves. So presence adds a second
desktop toast for the same genuine block, differing only in title and sound.

An earlier version of this file claimed presence "never double-notifies". That was
wrong — it reasoned only about the Slack path and missed that the Notification
hook uses the desktop channel too. Verified 2026-08-26; presence is disabled on
the box where it was found.

```bash
herdr plugin disable nexus.presence
```

Keep it enabled only where `hook-notification.sh` is absent.

## Channels (precedence)

1. **`NEXUS_PRESENCE_NOTIFY_CMD`** — if set, run this shell command; the message
   is exported as `$NEXUS_PRESENCE_MSG`. Route anywhere — e.g. on a headless box:
   `NEXUS_PRESENCE_NOTIFY_CMD='logger -t nexus-presence "$NEXUS_PRESENCE_MSG"'`.
2. **macOS** — `osascript` toast (sound `$NEXUS_PRESENCE_SOUND`, default `Submarine`).
3. **Linux desktop** — `notify-send -u critical`.
4. **Fallback** — terminal bell.

Only the `blocked` transition notifies; `idle`/`working`/`done` are silent
no-ops (the fast gate exits before spawning anything).

## Install (opt-in)

```bash
scripts/herdr-plugin-install.sh nexus-presence
```

Links the plugin + reloads herdr. It declares **no keybinding** (nothing appended
to `config.toml`) — purely event-driven.

## Verify

Next time an agent hits a permission prompt the classifier won't take (or asks a
question at end of turn), a desktop notification fires. Every blocked edge
appends exactly one breadcrumb to `$HERDR_PLUGIN_STATE_DIR/presence.log`
(herdr's per-plugin state dir), tagged with what happened to it:

| `outcome=` | Meaning |
|---|---|
| `notified` | Still blocked after the settle window — you were pinged. |
| `suppressed(settled)` | Recovered within the window (classifier took it). |
| `suppressed(cooldown)` | Another toast for this pane fired too recently. |

So the suppression rate is directly greppable:

```bash
sed -n 's/.*outcome=\([a-z()]*\).*/\1/p' \
  ~/.local/state/herdr/plugins/nexus.presence/presence.log | sort | uniq -c
```

On this box that state dir is
`~/.local/state/herdr/plugins/nexus.presence/` — note it is **not** under
`~/.config/herdr/`, which is where you would probably look first.

## Rollback

```bash
herdr plugin disable nexus.presence   # or: herdr plugin unlink nexus.presence
```

No `config.toml` changes to undo.
