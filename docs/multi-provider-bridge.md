# Multi-provider bridge — `/nexus` over Slack **and** Discord

Status: **design / proposal** (2026-08-01). Author: agents-nexus agent, for persinac.

## Why

Today `/nexus` (fleet control + A2A relay) is delivered only over **Slack Socket
Mode** — a persistent WebSocket the bridge must babysit. When that connection is
slow or drops, commands fail (the whole "the app did not respond" saga: envelopes
arriving ~2.6s late, blowing the 3s ack/`trigger_id` budget). Slack Socket Mode is
**heavy and connection-sensitive**.

Goal: also reach `/nexus` from **Discord**, which is **more forgiving** — its
interactions arrive as **stateless HTTP POSTs** (Discord retries; no persistent
socket to maintain) — and, more broadly, make the platform layer **pluggable** so
one core serves *one or many* providers (Slack + Discord now; Teams/Telegram later).

## Reference

Modeled on Vercel's [`vercel/chat`](https://github.com/vercel/chat) — "a unified
TypeScript SDK for building chat bots across Slack, Discord, Teams, and more":
a core `Chat` instance holds a map of **adapters** (`@chat-adapter/*`), you write
handlers once, and the same handlers run for events from every connected platform.
See also [Vercel's Chat SDK announcement](https://vercel.com/blog/chat-sdk-brings-agents-to-your-users).

**Adopt vs build — recommend BUILD (thin in-house adapters).** We borrow the
*core + adapter* pattern, not the SDK itself. Rationale: our value is the **fleet**
logic (NATS A2A routing, `substrate.sh` send-keys delivery, presence, idle-gating,
the `/nexus` command surface) — `vercel/chat` abstracts the generic chat-platform
layer, which is only ~49 Slack touchpoints in our bridge. Adopting the SDK wholesale
forces a rewrite onto its `thread`/`message`/Next.js model + a research-stage
dependency with unclear Discord maturity, to replace a layer we can abstract in a
few hundred LOC. (Alternative kept open below.)

## Current state (what must be abstracted)

`slack-bridge/index.js` (~2520 LOC) is heavily Slack-coupled — **ingress** via
`socket.on('slash_commands' | 'interactive' | 'message' | 'reaction_added')` and
**egress** via ~30 `web.chat.postMessage|update|delete` / `web.views.open|update` /
`web.reactions.add` call sites + `response_url` fetches. The **A2A transport is
already cleanly abstracted** (`transports/nats-transport.js`, `createNatsTransport`
with `publish`/`subscribe`) — proof the codebase tolerates a pluggable seam, and the
template for the provider seam.

## Proposed architecture

```
                    ┌─────────────────────────── NexusCore (provider-agnostic) ──────────┐
   Slack  ─┐        │  command dispatch: status|agents|peek|clear|stop|keep|msg|spawn|   │
  (adapter)├──cmd──▶│  restore  ·  panel builder  ·  agent-bus routing (NATS)  ·  presence│
  Discord ─┘  event │  in:  Command{name,args,userId,channelId,replyToken,provider}       │
  (adapter) ◀─reply─│  out: Reply/Message{text, ephemeral?, components?[]}                │
                    └─────────────────────────────────────────────────────────────────────┘
        ▲  egress (each adapter renders the normalized Message to its native format)
        └── Notifier.fanout(message, providers:['slack','discord']|'all')  ← outbound (cards, done-pings)
```

**1. `NexusProvider` interface** (the seam). Each adapter implements:
- `start()` — connect / register commands (Slack: Socket Mode; Discord: register app
  command + stand up the HTTP interactions endpoint).
- emits `'command'` → `Command { name, args, userId, channelId, replyToken, provider }`
- emits `'action'` → `Action { actionId, value, state, userId, replyToken, provider }`
- `reply(replyToken, Message)` — answer a command/interaction (Slack: `response_url`;
  Discord: interaction follow-up webhook).
- `send(target, Message)` — proactively post (Slack: `chat.postMessage`; Discord:
  channel webhook / bot REST).

**2. Normalized `Message` model** — `{ text?, ephemeral?, components?: Row[] }`
where a `Row` holds buttons / a select. Each adapter renders it: **Slack** → Block
Kit (`section`/`actions`/`static_select`/`button`); **Discord** → components v2
(action rows, buttons, string selects). This is the one place platform rendering
lives; `fleetPanel` etc. return normalized `Message`s, not Block Kit.

**3. `SlackAdapter`** — refactor the existing code behind the interface (Socket Mode
ingress; `response_url`/`postMessage` egress). **No behavior change** — the current
ack-empty + `response_url` discipline is retained (it's exactly what Discord needs too).

**4. `DiscordAdapter`** — new. See below.

**5. `Notifier`** — holds registered providers; `fanout(msg, {providers})` sends an
outbound notification (permission card, done-ping, presence alert) to one or many.
This is the "send to one **or many** providers" ask.

## Discord specifics (the "more forgiving" win)

- **Ingress = HTTP Interactions Endpoint (recommended), not the Gateway.** Register
  an "Interactions Endpoint URL"; Discord **POSTs** each slash command / button /
  select to it. Stateless, retried by Discord, no persistent WS to babysit — the
  resilience win over Slack Socket Mode. The nexus box exposes it via the **existing
  Cloudflare tunnel** (the fleet already runs tunnels, e.g. `kagent.flashbackfleet.com`)
  → `https://nexus-discord.<domain>/discord/interactions` → bridge `:PORT`.
- **Ed25519 signature verify** on every request (`X-Signature-Ed25519` /
  `-Timestamp` against the app public key) — mandatory; reject otherwise. Also answer
  the `PING` (type 1) with `PONG`.
- **The 3s ack maps directly to what we already do.** Discord requires an interaction
  response within 3s. Send a **deferred** ack immediately (type 5,
  `DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE`), then deliver the real result via
  **follow-up webhook** (15-min window). This is the byte-for-byte analog of our
  Slack "ack empty → `response_url`" fix — the discipline transfers unchanged.
- **Commands.** Register a `/nexus` **application command** with a single string
  option (`args`) to mirror today's `/nexus <subcommand> <...>`; or native
  subcommands later. Application commands are registered once via the REST API.
- **Components / ephemeral.** Buttons + string-selects in action rows (the panel);
  ephemeral = message `flags: 64`. Selecting/clicking → interaction POST → `Action`.
- **A2A relay unchanged.** The NATS A2A bus and `substrate.sh` delivery are
  provider-agnostic already; Discord only adds an ingress/egress surface.

## Command / concept mapping (Slack ↔ Discord)

| Concept | Slack | Discord |
|---|---|---|
| command | slash command (Socket Mode) | application command (HTTP interaction) |
| fast ack | `ack()` empty ≤3s | deferred response type 5 ≤3s |
| real reply | `response_url` POST (≤30 min) | follow-up webhook (≤15 min) |
| ephemeral | `response_type: ephemeral` | message `flags: 64` |
| buttons/menus | Block Kit `actions` + `static_select` | action row + button / string select |
| interaction | `block_actions` | component interaction POST |
| proactive post | `chat.postMessage` | channel webhook / bot REST |

## Phasing

1. **Seam + Slack adapter** — extract `NexusProvider` + normalized `Message`; move
   the Slack code behind `SlackAdapter`; `fleetPanel`/handlers return normalized
   `Message`s. Pure refactor, no behavior change; unit-test the renderers (Block Kit
   snapshot stays identical).
2. **Discord adapter** — HTTP interactions endpoint (Ed25519 verify + PING), register
   `/nexus`, defer→follow-up, render `Message`→components. Ship the **inline
   subcommands first** (`/nexus status|agents|peek …` — robust, no components), then
   the panel. Add the Cloudflare tunnel route + a `discord` app (bot token + public key).
3. **Fan-out** — `Notifier.fanout` so outbound notifications hit Slack and/or Discord.
   Per-provider on/off via env, mirroring the existing drop-in pattern.

## Open decisions (need your call)

1. **Adopt `vercel/chat` vs in-house adapters** — I recommend in-house (above). If
   you'd rather bet on the SDK, Phase 1 changes to "wire `/nexus` handlers onto a
   `Chat` instance with `@chat-adapter/slack` + `@chat-adapter/discord`."
2. **Discord ingress: HTTP interactions endpoint (recommended, stateless) vs Gateway
   WS** (parity with Socket Mode; only needed if you want message events, not just
   commands). The interactions endpoint needs a Cloudflare tunnel route.
3. **One bridge process serving both** (shared core, simplest) vs a separate
   `discord-bridge` service. Recommend one process, two adapters.
4. **Discord command shape** — single `/nexus <args>` (mirror today) vs native
   subcommands with typed options.

## Next step

On your decisions, I'll open an OpenSpec change (`multi-provider-bridge`) and start
Phase 1 (the provider seam + Slack adapter refactor, behavior-preserving).
