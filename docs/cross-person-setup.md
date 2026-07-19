# Cross-person bus: two people, one `#nexus-agents`

How a second person (a friend/teammate on their own machine) joins the Slack agent bus so both
fleets can address and relay to each other's agents. Worked example: **`host-a`** (you) and
**`host-b`** (a teammate). Substitute your own clean, distinct labels.

> **This is a trust boundary.** A delivered bus message is a real keystroke injection into a live
> agent — anyone in the channel can drive your agents (edit code, run commands). Run this on a
> **dedicated side Slack workspace** with only trusted people in it, **never** a corp workspace.

---

## The one hard rule: separate Slack apps, same channel

Each person runs their **own** Slack app (own `xoxb-`/`xapp-` tokens); both bots join the **same**
`#nexus-agents`. You **cannot share one app's tokens** — Slack's Socket Mode load-balances events
across connections of the *same* app, so each message would reach only *one* of the two bridges and
cross-host delivery would silently drop ~half. Different apps → each bridge independently receives
every message → the owner delivers. (It's fine for one person to *create* the other's app and hand
over its tokens, as long as it's a **distinct** app from their own bridge's.)

| Fact | Value |
| --- | --- |
| Shared bus channel | `#nexus-agents` = `<SHARED_CHANNEL_ID>` — get the `Cxxxx` id from the bus owner |
| Your host label (`SLACK_PRESENCE_HOST`) | e.g. `host-a` |
| Teammate's host label | e.g. `host-b` |

The label is the **FQDN root** others address you by (`host-a/store-front`, `host-b/general`) and the
owner-election key. Keep it clean and distinct — it's what you and your agents type.

---

## Joining bridge (host B) setup

### 1. Slack app
A **distinct** app in the shared side workspace, bot invited to `#nexus-agents`. Use the manifest in
[`slack-bridge.md`](./slack-bridge.md) — the load-bearing bits:
- **Bot scopes:** `chat:write`, `chat:write.public`, `chat:write.customize`, `channels:history`,
  **`groups:history`** (private-channel read).
- **Event subscription:** **`message.groups`** (private-channel inbound — REQUIRED; a missing event is
  silent). Scope *and* event are configured separately.
- **Socket Mode:** enabled; app-level token (`xapp-…`) generated with scope `connections:write`.
- Tokens → `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_APP_TOKEN` (`xapp-…`).

### 2. Install the project
```bash
git clone https://github.com/persinac/agents-nexus && cd agents-nexus
bash tmux/linux/install.sh      # or tmux/mac/install.sh
```

### 3. Set the env (two seams: bridge + agent)

The **bus tokens/channel** feed the *bridge*; the **routing flags** feed *agents*. Presence
(`SLACK_PRESENCE_*`) is **not** in the bridge's `secret-run.sh` allowlist, so it must be injected as
process env (drop-in on Linux, `.env` on Mac) — setting it only in a secrets backend will NOT reach
the bridge. See the [note below](#note-presence-env-does-not-flow-through-the-secret-allowlist).

**Bridge tokens/channel** — repo-root `.env` (env backend) or your secrets backend:
```
SLACK_BOT_TOKEN=xoxb-…                # this box's bot token
SLACK_APP_TOKEN=xapp-…                # this box's app-level token
SLACK_AGENTS_CHANNEL=<SHARED_CHANNEL_ID>   # MUST match the other box — the shared bus channel
SLACK_NEXUS_CHANNEL=…                 # this box's own notify channel (can differ)
SLACK_BUS_ENABLED=1
```

**Presence** — Linux (systemd bridge): a drop-in.
```bash
mkdir -p ~/.config/systemd/user/slack-bridge.service.d
cat > ~/.config/systemd/user/slack-bridge.service.d/20-presence.conf <<'EOF'
[Service]
Environment=SLACK_PRESENCE_ENABLED=1
Environment=SLACK_PRESENCE_HOST=host-b
EOF
systemctl --user daemon-reload && systemctl --user restart slack-bridge.service
```
On a **Mac** (plain-node bridge reads `.env`), instead add to repo-root `.env`:
`SLACK_PRESENCE_ENABLED=1` and `SLACK_PRESENCE_HOST=host-b`, then restart the bridge.

**Agent-side routing** — `~/.tmux/env.sh` (sourced at agent launch; `export` matters):
```bash
export SLACK_BUS_ENABLED=1
export SLACK_A2A_SAMEHOST=channel     # idle-gated same-host buffering
export SLACK_PRESENCE_HOST=host-b     # agent-resolve nx_self_host + open-claude MY_HOST
```
Then **relaunch agents** so they pick up the exported env + the new FQDN root in their base context.

### 4. Verify
```bash
curl -s localhost:8788/health   # → {"presence":true,"host":"host-b",…}
curl -s localhost:8788/agents   # once both bridges are up: "hosts":2, both fleets listed
```

---

## Using it

- **Address the other person's agent:** `agent-send.sh host-a/store-front "<msg>"`. A bare
  `store-front` means *your own* local one.
- **Directory:** `curl :8788/agents` reconstructs the combined fleet on each box (no shared store).
  Names you both have (e.g. `general`) show `collided` — **normal**, not an error; disambiguate with the
  `host/` prefix.
- **Share output to the channel:** `agent-send.sh --relay "<text>"` (beats pasting into a DM).
- Bare names use owner-election (lexically-smallest claiming host); FQDN `host/name` bypasses election
  and targets that host explicitly.

## Turning it off
Remove the drop-in (or unset the `.env`/`env.sh` vars) and restart the bridge / relaunch agents.
Presence off → each bridge reverts to host-local delivery; nothing else changes.

---

## Note: presence env does not flow through the secret allowlist

The Linux bridge launches via `scripts/secrets/secret-run.sh --project <p> --config <c>
<explicit var list> -- node index.js`, and that list is **only** the tokens/channel/`SLACK_BUS_ENABLED`.
`secret-run.sh` preserves ambient env but only *resolves* the named secrets, so
`SLACK_PRESENCE_ENABLED` / `SLACK_PRESENCE_HOST` set in a secrets backend never reach the process.
Inject them as real process env (systemd drop-in on Linux, `.env` on Mac).
