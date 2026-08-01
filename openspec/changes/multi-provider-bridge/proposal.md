## Why

`/nexus` (fleet control + A2A relay) is delivered only over **Slack Socket Mode** —
a persistent WebSocket the bridge must babysit. When that link is slow or drops,
commands fail (the "the app did not respond" saga: envelopes arriving ~2.6s late,
blowing the 3s ack/`trigger_id` budget). Slack Socket Mode is heavy and
connection-sensitive.

We want `/nexus` reachable from **Discord** too — whose interactions arrive as
**stateless HTTP POSTs** (retried by Discord; no socket to maintain), i.e. *more
forgiving* — and, more broadly, a **pluggable platform layer** so one core serves
*one or many* providers (Slack + Discord now; Teams/Telegram later). Modeled on the
core+adapter pattern of [`vercel/chat`](https://github.com/vercel/chat); full design
in `docs/multi-provider-bridge.md`.

## What Changes

- Introduce a provider-agnostic **NexusCore**: the `/nexus` command dispatch
  (status/agents/peek/clear/stop/keep/msg/spawn/restore + the panel) and A2A routing,
  consuming normalized `Command`/`Action` events and producing normalized `Message`s.
- Add a **`NexusProvider`** seam + a normalized **Message/Command/Action** model
  (one place platform rendering lives).
- Refactor the existing Slack code behind a **`SlackAdapter`** — **behavior-preserving**
  (retains ack-empty + `response_url`).
- Add a **`DiscordAdapter`**: an HTTP **interactions endpoint** (Ed25519 verify,
  PING/PONG, defer≤3s → follow-up≤15min), the `/nexus` application command, and
  `Message`→Discord-components rendering.
- Add a **`Notifier`** that fans an outbound notification (permission card, done-ping)
  to one **or many** providers.

**Locked decisions (with operator, 2026-08-01):** (1) **in-house** thin adapters, not
the `vercel/chat` SDK; (2) Discord ingress via the **HTTP interactions endpoint**, not
the Gateway; (3) **one** bridge process hosting both adapters; (4) Discord command
**mirrors** `/nexus <args>` (a single string option), not native subcommands.

## Capabilities

### New Capabilities

- `multi-provider-bridge`: The provider contract. A `NexusProvider` adapter normalizes
  a platform's slash commands / interactions into `Command`/`Action` events and renders
  normalized `Message`s to that platform; the `/nexus` dispatch is provider-agnostic; a
  `Notifier` sends to one or many providers. Covers the Slack + Discord adapters, the
  Discord interactions-endpoint contract (Ed25519 verification, PING→PONG, deferred ack
  within 3s then follow-up), and multi-provider fan-out.

### Modified Capabilities

<!-- The slack-agent-bus / orchestrator-spawn BEHAVIOR is unchanged; only its Slack
     I/O moves behind SlackAdapter. No requirement text in those specs changes; the new
     multi-provider-bridge capability fully covers the provider seam + Discord. -->

## Impact

- **Code**: new `slack-bridge/providers/{types,slack,discord}.js`; extract the
  provider-agnostic core (`/nexus` dispatch + `fleetPanel` → normalized `Message`); a
  `Notifier`; `index.js` becomes wiring/bootstrap. No change to
  `transports/nats-transport.js`, `substrate.sh` delivery, presence, or the reaper.
- **Infra (operator)**: a **Discord application** (bot token + public key + app id), a
  **Cloudflare tunnel** route → the bridge `/discord/interactions` endpoint, and the
  `/nexus` app command registered.
- **Config**: per-provider enable via env — existing `SLACK_*`; new `DISCORD_*`
  (`DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_APP_ID`, interactions path/port).
- **Docs**: `docs/multi-provider-bridge.md` (design, already merged) + a `/nexus`
  command reference update for Discord.
