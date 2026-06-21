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
3. **Anything else** (no address, untracked thread) → **smart routing**: a haiku
   classifier scores the message against the active agents (name + working dir)
   and, above the confidence floor (`SLACK_ROUTE_MIN_CONFIDENCE`, default 0.6),
   delivers to the best match and replies `routed to \`name\` (auto · NN%)`.
   Below the floor (or no clear match) it falls back to the usage hint and asks
   you to address an agent explicitly. Set `SLACK_ROUTE_ENABLED=0` to disable;
   routing also no-ops when `ANTHROPIC_API_KEY` is unset.

Only the configured `#nexus` channel and DMs to the bot are acted on; messages in
any other channel are ignored.

## Orchestrator: spawn the right agent (opt-in)

When routing finds **no** running agent for a message (step 3 falls through), the
bridge can offer to spin one up instead of just showing the usage hint. This is
**off by default** — set `SLACK_SPAWN_ENABLED=1` to enable it.

Flow:

1. **Resolve the repo.** `scripts/spark-resolve.py` asks the live Spark MCP
   service (`localhost:8343`) which repo the message is about.
2. **Allowlist + path.** The resolved repo must be a key in the spawnable-repo
   allowlist (`~/.tmux/spawnable-repos.json` by default) — a JSON object mapping
   repo name → absolute local checkout path. This is both the safety gate and the
   name→path resolver (Spark indexes many repos that aren't cloned here). A repo
   not on the list is never offered.
3. **Confirm.** The bridge posts a Block Kit *"Spin up an agent in `repo`? [🚀 / No]"*
   card. **Nothing spawns without an explicit click** — a No, or a 5-minute
   timeout, cancels and releases the lock.
4. **Spawn, seeded.** On approval it runs `tmux new-window … open-claude.sh` with
   `SEED_PROMPT` set to the originating message, so the new agent starts on the
   task immediately (no send-keys race). The spawned agent registers normally and
   is reapable.

**Guardrails** (all evaluated before any window is created):

- **Spawnable-repo allowlist** — only listed repos are eligible.
- **Per-repo in-flight lock** — at most one spawn (or live agent) per repo; a
  second request for the same repo is told to address the existing one. The lock
  is seeded on startup from the durable ledger so it survives a bridge restart.
- **Global rate-limit** — `SLACK_SPAWN_RATE_MAX` spawns per
  `SLACK_SPAWN_RATE_WINDOW_MS` window (default 3 per 10 min), re-checked at the
  moment of approval.

### Resilience: pin + restore (survive an AFK reap)

A PuTTY/network drop does **not** kill your agents — the tmux server is
systemd-persistent. What sweeps them while you're away is the overseer reaper
(`REAP_ALL=1` every 15 min). Two mechanisms make that recoverable:

- **Pin** the active working set so the reaper skips it even under `REAP_ALL=1`:
  `scripts/agent-keep.sh <name> on` (or the Slack command `keep <name> on`), which
  sets the `@keep` window option. `agent-keep.sh list` shows what's pinned.
- **Restore** a reaped agent from the durable ledger (`scripts/agent-ledger.py`,
  default `~/.tmux/agent-ledger.jsonl`). The reaper checkpoints before killing and
  records the agent as `dormant`; restore re-spawns it in its repo (seeded from
  its checkpoint via `open-claude.sh`'s by-slug rehydration). Trigger it with the
  Slack command `restore <repo>` (or bare `restore` to list dormant agents), or
  via the **reconnect nudge** — when dormant agents exist, the next `#nexus`
  message surfaces a *"N agents were reaped while you were away — restore?"* card
  (at most once per hour; never auto-restores).

### Orchestrator config (env)

| Var | Default | Meaning |
| --- | --- | --- |
| `SLACK_SPAWN_ENABLED` | `0` (off) | Master switch for the spawn branch + restore/nudge. Off ⇒ original usage-hint behavior, zero change. |
| `SLACK_SPAWN_ALLOWLIST_FILE` | `~/.tmux/spawnable-repos.json` | JSON `{ "repo": "/abs/path", … }`. See `slack-bridge/spawnable-repos.example.json`. |
| `SLACK_SPAWN_SESSION` | `agents` | tmux session to spawn into. |
| `SLACK_SPAWN_MIN_SCORE` | `0` | Min Spark score to offer a spawn. Permissive by default — the confirm card is the real gate (Spark scores are small/reranked). |
| `SLACK_SPAWN_RATE_MAX` | `3` | Max spawns per rolling window. |
| `SLACK_SPAWN_RATE_WINDOW_MS` | `600000` | Rate-limit window (10 min). |
| `SLACK_SPAWN_CONFIRM_TTL_MS` | `300000` | Confirm-card lifetime before it expires + releases the lock (5 min). |
| `SLACK_NUDGE_MIN_INTERVAL_MS` | `3600000` | Min gap between reconnect nudges (1 h). |
| `SLACK_SPARK_PYTHON` | `$AGENTS_NEXUS_DIR/spark/.venv/bin/python` | Interpreter for the Spark resolver (needs the `mcp` SDK). |
| `SLACK_OPEN_CLAUDE` | `~/.tmux/open-claude.sh` | Launch script used for spawns/restores. |
| `AGENT_LEDGER` | `~/.tmux/agent-ledger.jsonl` | Durable agent ledger path (shared with the reaper). |

These can be set via Doppler (`nexus/prd`) alongside the other `SLACK_*` secrets, or
in the gitignored `.env`. The Spark resolver and ledger are only invoked when
`SLACK_SPAWN_ENABLED=1`.

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
  interactivity:
    is_enabled: true             # required for the Approve/Deny buttons (block_actions)
  event_subscriptions:
    bot_events:
      - message.channels         # PUBLIC channel inbound
      - message.groups           # PRIVATE channel inbound (e.g. #nexus-lan) — REQUIRED for a private control channel
      - message.im
      - app_mention
      - reaction_added           # pairs with reactions:read
  socket_mode_enabled: true
  org_deploy_enabled: false
```

**Channel type ↔ event/scope must match.** A bot only receives a channel's
messages if it subscribes to that channel type's `message.*` event AND holds the
matching `*:history` scope — being a member is not enough. The default control
channel here, `#nexus-lan`, is **private**, so it needs `message.groups` +
`groups:history`. A *public* channel needs `message.channels` + `channels:history`.
A missing event is silent: Socket Mode simply never delivers those messages and
the bridge logs nothing. (Adding only a scope without the event is the common
trap — scopes and events are configured separately.)
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
# macOS (launchd)
task launchd:install:slack-bridge     # or: task launchd:install:all
launchctl list | grep slack-bridge    # confirm loaded
# logs: /tmp/agents-nexus-slack-bridge.log

# Linux (systemd user unit) — installed + started by the tmux installer:
bash tmux/linux/install.sh            # installs deps + enables + starts the unit
systemctl --user status slack-bridge.service
journalctl --user -u slack-bridge.service -f    # logs
```

On macOS, launchd `KeepAlive` is `SuccessfulExit: false`; on Linux the systemd
unit uses `Restart=on-failure`. Both **only restart on a crash** — when tokens
are unset the bridge exits 0 cleanly and is left alone (no thrash).

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
  `launchd/com.agents-nexus.slack-bridge.plist` and reinstall. The Linux unit
  resolves node at install time (`__NODE_BIN__`, via `readlink -f`), so it just
  needs a re-run of `tmux/linux/install.sh` after a node upgrade.
- **Linux parity** is wired: `tmux/linux/systemd/slack-bridge.service` (the
  systemd analog of the plist) plus the `/notify` round-trip in
  `tmux/linux/tmux-scripts/hook-notification.sh`. The Linux hook uses
  `notify-send` (console) + a terminal bell (SSH) in place of macOS `osascript`.
  The auto-approve classifier gate is included but stays inert unless the
  `~/.tmux/.classify-venv` is present.
