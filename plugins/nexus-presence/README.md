# nexus-presence plugin

A **desktop notification** the instant a herdr agent goes `blocked` — i.e. it
needs your input (a permission prompt, an elicitation dialog, an end-of-turn
question).

Event-driven: **no keybinding, no daemon, no polling.** herdr fires the plugin's
`pane.agent_status_changed` hook natively; the hook filters to the `blocked`
transition and pops an OS notification.

## Why the desktop channel

The full nexus stack (`substrated` + `slack-bridge`) already subscribes to
`pane.agent_status_changed` and posts a Slack card to `#nexus` when an agent
blocks. Presence deliberately uses a **different** channel — the desktop — so it
adds a signal without double-notifying. It's also the zero-infra path: a teammate
running herdr + this plugin gets blocked-alerts with none of the daemon/bridge
stack.

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

Next time an agent hits a permission prompt (or asks a question at end of turn),
a desktop notification fires. A breadcrumb is also appended to
`$HERDR_PLUGIN_STATE_DIR/presence.log` (herdr's per-plugin state dir).

## Rollback

```bash
herdr plugin disable nexus.presence   # or: herdr plugin unlink nexus.presence
```

No `config.toml` changes to undo.
