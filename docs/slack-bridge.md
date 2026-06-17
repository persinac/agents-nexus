# Slack bridge — two-way #nexus ⇄ agent control

The nexus can already *post* to Slack (the Slack MCP plugin, a few webhook cron
jobs). The **bridge** adds the missing inbound direction: reply in Slack and the
running agent sees it. It also closes the loop — when an agent needs input, it
surfaces in Slack and your thread reply routes straight back to that agent.

It connects over **Socket Mode**, so there is **no public URL / tunnel** to set up.

## How it works

```
 Slack #nexus / DMs ──(Socket Mode, outbound ws)──► slack-bridge (host, :8788)
                                                        │  resolve target:
                                                        │   1. reply in a tracked thread → that agent
                                                        │   2. "name: text" / "slot: text" → registry lookup
                                                        │   3. else → usage hint
                                                        ▼
                                       ~/.tmux/agent-send.sh <slot> "<text>"
                                                        ▼
                                            tmux send-keys → agent prompt

 agent needs input → hook-notification.sh ──(POST /notify, localhost)──► slack-bridge
                                                        │  chat.postMessage to #nexus (as the bot)
                                                        ▼  record thread_ts → agent in the thread map
                                              ~/.tmux/slack-threads.json
```

- **Delivery** reuses `~/.tmux/agent-send.sh` (slot/name resolution + `tmux
  send-keys`), the same primitive `agent-registry.sh` and the arbiter use.
- **Agent names** come from `~/.tmux/registry/<pane>` (`NAME`/`SLOT`). The thread
  map stores the **name** and re-resolves to a slot at delivery time, so it
  survives slot renumbering.
- The bridge is a standalone process (own crash domain) — independent of the
  arbiter / dashboard.

## Routing rules (precedence)

1. **Thread reply** in a thread the bot started → delivered to that thread's agent.
   This is the round-trip happy path.
2. **Addressed top-level** message `name: your message`, `slot: your message`, or
   `@nexus-bot name: your message` → resolved against the live registry.
   Unknown name → the bot replies listing the active agents.
3. **Anything else** (no address, untracked thread) → a one-line usage hint. No
   delivery.

Only the configured `#nexus` channel and DMs to the bot are acted on; messages in
any other channel are ignored.

## Slack app setup (one-time)

Create an app at <https://api.slack.com/apps> → **From an app manifest**, paste
the manifest below, then install to the workspace. (Creating/installing an app in
the Garner workspace may need workspace-admin approval.)

```yaml
display_information:
  name: Nexus Bridge
features:
  bot_user:
    display_name: nexus-bridge
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write
      - chat:write.public       # post to public channels without joining
      - chat:write.customize     # post as the agent's name/icon
      - channels:history         # read the public #nexus channel
      - channels:read
      - im:history               # DM-the-bot control (optional)
      - app_mentions:read
      - reactions:write
      - reactions:read           # read reactions as control signals
      - users:read               # user_id -> display name
      - users:read.email         # email -> user_id (tag the human)
      - files:write              # post logs / diffs / screenshots
settings:
  event_subscriptions:
    bot_events:
      - message.channels
      - message.im
      - app_mention
      - reaction_added           # pairs with reactions:read
  socket_mode_enabled: true
  org_deploy_enabled: false
```

`#nexus` is a **public** channel, so `channels:history` + the `message.channels`
event carry the inbound traffic — the private-channel scopes (`groups:history` /
`groups:read` / `message.groups`) are intentionally omitted. Add them only if a
*private* channel is ever used for this (a reinstall, since scopes/events change).
The `reactions:read` / `users:read*` / `files:write` / `chat:write.*` scopes are
future-proofing (approve-by-reaction, @-tagging the human, file posts) included now
to avoid a later reinstall — trim them if you want a tighter least-privilege set.

Then collect three values:

| Value | Where | Goes in `.env` |
|---|---|---|
| **Bot token** `xoxb-…` | OAuth & Permissions → after install | `SLACK_BOT_TOKEN` |
| **App-level token** `xapp-…` | Basic Information → App-Level Tokens → generate with scope `connections:write` | `SLACK_APP_TOKEN` |
| **Channel id** `C…` | In the public `#nexus` channel, **invite the bot** (`/invite @nexus-bridge`) — public channels still only deliver `message.channels` events to member bots — then copy the id from the channel details | `SLACK_NEXUS_CHANNEL` |

`SLACK_BRIDGE_PORT` defaults to `8788`.

## Run it

```bash
task slack:bridge:install      # one-time: npm install
task slack:bridge              # foreground (logs to the terminal)
```

Supervised (recommended — restarts on crash, starts at login):

```bash
task launchd:install:slack-bridge     # or: task launchd:install:all
launchctl list | grep slack-bridge    # confirm loaded
# logs: /tmp/agents-nexus-slack-bridge.log
```

`KeepAlive` is configured with `SuccessfulExit: false`, so launchd **only restarts
on a crash** — when tokens are unset the bridge exits 0 cleanly and is left alone
(no thrash).

## Verify

```bash
# Bridge connected?
curl -s localhost:8788/health        # {"ok":true,"connected":true,"threads":N}

# Inbound — in #nexus, post:  svc-chatbot: say hi
#   → the agent's prompt receives "say hi"; the bot reacts ✅

# Outbound round-trip — trigger any permission prompt in an agent:
#   → "⏳ <agent> needs input: …" appears in #nexus
#   → reply in that thread "approve" (or whatever) → routes back to the agent
```

## Notes & safety

- **This is remote control.** Text from Slack is typed into a live agent prompt.
  `#nexus` is a **public** channel, so anyone in the workspace can post there and
  drive an agent — scope who you trust accordingly. The bot only acts on the
  configured `#nexus` channel and DMs, and only sees channels it is a member of.
  Tokens live in the gitignored `.env`.
- **Tracked vs untracked threads.** Round-trip threads are posted by the *bot*
  (via the hook → `/notify`). Posts the agent makes itself through the Slack MCP
  (as you) are not tracked, so replies to those won't auto-route — address the
  agent explicitly instead.
- **Thread map** lives at `~/.tmux/slack-threads.json` (single writer: the
  bridge). Entries older than 7 days are pruned on write.
- **launchd node path** is pinned to the installed nvm version in the plist
  `PATH`. If you upgrade node, update
  `launchd/com.agents-nexus.slack-bridge.plist` and reinstall.
- **Linux parity** is not wired yet (Mac-only for now). Porting needs a systemd
  unit plus the same `hook-notification.sh` edit in `tmux/linux/`.
