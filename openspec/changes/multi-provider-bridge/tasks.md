## Status (2026-08-01)

Proposed. Decisions locked (in-house adapters · HTTP interactions · one process ·
mirror `/nexus <args>`). Design merged (`docs/multi-provider-bridge.md`). No code yet.

## 1. Provider seam + normalized model

- [ ] 1.1 `slack-bridge/providers/types.js` — `Message {text?, ephemeral?, components?}`,
      `Command`, `Action`, and the `NexusProvider` shape (JSDoc). Pure, unit-tested.
- [ ] 1.2 Extract the provider-agnostic `/nexus` dispatch from `index.js` into a core
      `dispatchNexusCommand(command, respond)` — same helpers (status/agents/peek/clear/
      stop/keep/msg/spawn/restore), no Slack types in the signature.
- [ ] 1.3 `fleetPanel` (and any card builders) return a normalized `Message`, not Block
      Kit directly; add a Slack renderer `messageToBlockKit(Message)`.

## 2. Slack adapter (behavior-preserving refactor)

- [ ] 2.1 `slack-bridge/providers/slack.js` — `SlackAdapter`: Socket Mode ingress →
      `Command`/`Action`; `reply` via `response_url` (ack-empty first); `send` via
      `chat.postMessage`; renders `Message` via `messageToBlockKit`.
- [ ] 2.2 `index.js` wires `SlackAdapter` into the core; remove inline Slack I/O.
- [ ] 2.3 Snapshot-test: Block Kit output for the panel + each reply is byte-identical
      to today. `node --check` + `npm test` green.
- [ ] 2.4 **Verify live**: restart bridge; `/nexus status|agents|peek|<panel>` in Slack
      behaves exactly as before (needs an operator run).

## 3. Discord adapter

- [ ] 3.1 `slack-bridge/providers/discord.js` — `DiscordAdapter`: `POST /discord/
      interactions` handler with Ed25519 verify (raw body) + PING→PONG. Unit-test verify
      against a known keypair/vector.
- [ ] 3.2 Register the `/nexus` application command (string option `args`) at startup
      (idempotent PUT).
- [ ] 3.3 Interaction (type 2) → `Command`; component (type 3) → `Action`. Reply =
      deferred (type 5, `flags:64` for ephemeral) within 3s, then follow-up webhook.
- [ ] 3.4 `messageToDiscord(Message)` — action rows / buttons / string selects; wire the
      panel pick→reveal via message edit.
- [ ] 3.5 Config: `DISCORD_BOT_TOKEN` / `DISCORD_PUBLIC_KEY` / `DISCORD_APP_ID` / path;
      adapter is inert (not started) when unset.
- [ ] 3.6 **Operator infra**: create the Discord app (bot + public key + app id), add a
      Cloudflare tunnel route → `/discord/interactions`, set the interactions endpoint URL.
- [ ] 3.7 **Verify live**: `/nexus status|agents|peek` in Discord; then the panel.

## 4. Multi-provider fan-out

- [ ] 4.1 `Notifier` holding registered providers; `fanout(Message, {providers})`.
- [ ] 4.2 Route outbound notifications (permission cards, done-pings, presence alerts)
      through `Notifier`; per-provider enable env; default = originating provider only.
- [ ] 4.3 Docs: `/nexus` command reference gains Discord; `docs/multi-provider-bridge.md`
      "next step" resolved.

## Non-goals (this change)

- Discord Gateway/message events (only application-command + component interactions).
- Native Discord subcommands (mirror `/nexus <args>` instead).
- Additional providers (Teams/Telegram) — the seam makes them additive later.
