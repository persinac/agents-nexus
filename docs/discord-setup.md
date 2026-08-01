# Discord setup — values the bridge needs (Phase 2 of `multi-provider-bridge`)

Operator checklist for bringing `/nexus` up on Discord. Design: `docs/multi-provider-bridge.md`;
change: `openspec/changes/multi-provider-bridge/`. Discord ingress is a **stateless HTTP
interactions endpoint** (no gateway socket), verified by Ed25519 signature.

## The four values

Create (or reuse) an app at <https://discord.com/developers/applications>. Then collect:

| Value | Env var | Where in the portal | Secret? |
|---|---|---|---|
| **Application ID** | `DISCORD_APP_ID` | General Information → *Application ID* | no (public) |
| **Public Key** | `DISCORD_PUBLIC_KEY` | General Information → *Public Key* | no (it's a public key) |
| **Bot Token** | `DISCORD_BOT_TOKEN` | Bot → *Reset Token* → copy | **YES — secret** |
| **Server (Guild) ID** | `DISCORD_GUILD_ID` | Discord app → enable *Developer Mode* (Settings → Advanced), then right-click your server → *Copy Server ID* | no |

- **Application ID** — used to register the `/nexus` command (`PUT /applications/{app_id}/commands`)
  and to post interaction follow-ups (`/webhooks/{app_id}/{interaction_token}`).
- **Public Key** — used to Ed25519-verify **every** inbound interaction over `timestamp + rawBody`.
- **Bot Token** — used once at startup to register the `/nexus` application command (REST call with
  `Authorization: Bot …`). Interaction *responses* use the per-interaction token from the payload,
  not this token — but registration needs it, so it is required.
- **Guild ID** — *recommended*. Registering `/nexus` as a **guild** command is instant; a **global**
  command takes up to ~1 h to propagate. For a personal fleet, guild-scoped is right. Omit only if
  you want it available in every server the app joins.

## Where the values go

All four go into **Doppler `nexus` / `prd`** — the same config `secret-run.sh` already pulls the
`SLACK_*` tokens from. Set them with `doppler secrets set` (or the dashboard). Then the bridge's
`ExecStart` injection list gains `DISCORD_BOT_TOKEN DISCORD_PUBLIC_KEY DISCORD_APP_ID
DISCORD_GUILD_ID` (I do that wiring when Phase 2 lands).

> **Security:** `DISCORD_BOT_TOKEN` is a live credential — **do not paste it into chat, Slack, or a
> terminal I can see** (the transcript is durable and goes to the model API). Put it straight into
> Doppler. The other three are not secret; sharing them is harmless, but keep them in Doppler too for
> consistency. If a token ever lands in the transcript, rotate it (Bot → Reset Token).

## Infra — the interactions endpoint

The Discord adapter serves `POST /discord/interactions` on the **existing bridge HTTP server
(`127.0.0.1:8788`, `SLACK_BRIDGE_PORT`)**. Expose it through the Cloudflare tunnel:

1. Add an ingress hostname (e.g. `nexus-discord.<your-domain>`) → `http://127.0.0.1:8788` in the
   `cloudflared` config, and reload the tunnel.
2. In the portal → General Information → **Interactions Endpoint URL**, paste
   `https://nexus-discord.<your-domain>/discord/interactions` and save.
   - Discord **validates it immediately** by sending a signed `PING` — so this step only succeeds
     **after** the Phase 2 code is running (the endpoint must verify the signature and answer
     `{ "type": 1 }`). Provision the four values + invite now; set this URL after I ship Phase 2.

## Invite the app to your server

OAuth2 → URL Generator → scope **`applications.commands`** (that alone is enough for slash commands
over HTTP interactions — the `bot` gateway scope is *not* required). Open the generated URL and add
the app to your server.

## Order of operations

1. **Now (you):** create the app → grab the four values into Doppler `nexus`/`prd` → invite the app
   to your server (`applications.commands`).
2. **Then (me):** build Phase 2 — the `/discord/interactions` endpoint (verify + PING→PONG +
   defer≤3s → follow-up), register the `/nexus` guild command, render `Message` → Discord components;
   add the `DISCORD_*` names to `ExecStart`; add the Cloudflare ingress route.
3. **Then (you):** set the **Interactions Endpoint URL** in the portal (it validates against the
   now-running bridge), and run `/nexus status` in Discord to confirm.
