# Design — multi-provider bridge

Full narrative + diagrams: **`docs/multi-provider-bridge.md`** (merged). This captures
the locked decisions and the parts that carry implementation risk.

## Locked decisions (operator, 2026-08-01)

1. **In-house adapters**, not the `vercel/chat` SDK. Our value is the *fleet* logic
   (NATS A2A, `substrate.sh` send-keys, presence, the `/nexus` surface); the platform
   layer is only ~49 Slack touchpoints. Seam it in-house rather than rewrite onto the
   SDK's `thread`/`message`/Next.js model + a research-stage dependency.
2. **Discord ingress = HTTP interactions endpoint** (stateless, Discord-retried), not
   the Gateway. This is the resilience win over Slack Socket Mode.
3. **One bridge process**, two adapters (shared core), not a separate `discord-bridge`.
4. **Discord command mirrors `/nexus <args>`** — one application command with a single
   string option, dispatched by the same core as Slack's `/nexus <args>`.

## The seam

```
NexusProvider (adapter) {
  start()                              // connect / register commands
  onCommand(handler)                   // Command{name,args,userId,channelId,replyToken,provider}
  onAction(handler)                    // Action{actionId,value,state,userId,replyToken,provider}
  reply(replyToken, Message)           // answer a command/interaction
  send(target, Message)                // proactive post
}
Message { text?, ephemeral?, components?: Row[] }   // Row = buttons | one select
```

`fleetPanel` and the command handlers return **normalized `Message`s**; each adapter
renders — Slack → Block Kit, Discord → components v2 (action rows / buttons / string
selects). The `/nexus` dispatch (a pure `(Command, replyFn) → void`) is shared.

## Discord interactions contract (the risky/novel part)

- **Endpoint**: `POST /discord/interactions` on the bridge HTTP server, exposed via the
  existing Cloudflare tunnel. Discord validates it by sending a signed `PING`.
- **Verify EVERY request**: Ed25519 over `timestamp + rawBody` using
  `X-Signature-Ed25519` / `X-Signature-Timestamp` against `DISCORD_PUBLIC_KEY`; reject
  `401` on mismatch. Use the raw body (not re-serialized JSON).
- **PING (type 1) → PONG** `{type:1}`.
- **The 3s budget = our existing discipline.** Application-command (type 2) and
  component (type 3) interactions: reply within 3s with a **deferred** ack
  (`type:5` `DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE`, `flags:64` for ephemeral), then
  deliver the real result via a **follow-up webhook** (`/webhooks/{app}/{token}`, ~15
  min). This is the byte-for-byte analog of Slack ack-empty → `response_url`.
- **Command registration**: one `PUT` of the `/nexus` application command (string option
  `args`) via the REST API at startup (idempotent).
- **Components**: buttons/string-selects in action rows; a component interaction POST →
  `Action`; the panel's pick→reveal flow works the same (edit the message via follow-up).

## Slack↔Discord mapping

| | Slack | Discord |
|---|---|---|
| fast ack | `ack()` empty ≤3s | deferred type 5 ≤3s |
| real reply | `response_url` ≤30m | follow-up webhook ≤15m |
| ephemeral | `response_type: ephemeral` | `flags: 64` |
| menus/buttons | Block Kit `actions`/`static_select` | action row / button / string select |
| interaction | `block_actions` | component interaction POST |

## Phasing (see tasks.md)

1. **Seam + Slack adapter** — extract `NexusProvider`, normalized `Message`, and the
   `/nexus` dispatch; move Slack behind `SlackAdapter`. Behavior-preserving; Block Kit
   output byte-identical (snapshot-tested). **Verify Slack `/nexus` still works** before
   moving on.
2. **Discord adapter** — endpoint (verify+PING), register command, defer→follow-up,
   `Message`→components. Ship **inline subcommands first** (no components), then the
   panel. Operator provisions the Discord app + tunnel route.
3. **Fan-out** — `Notifier.fanout` for outbound notifications; per-provider enable env.

## Risks / notes

- Phase 1 touches the *live* `/nexus` path — keep it behavior-preserving and re-test
  Slack after (needs an operator `/nexus` in Slack, since Socket Mode can't be fully
  driven headlessly).
- Discord requires operator infra (app + tunnel) before Phase 2 is testable e2e — same
  shape as the original Slack manifest/reinstall dependency.
- Keep the raw request body for Ed25519 (a JSON round-trip changes bytes → verify fails).
