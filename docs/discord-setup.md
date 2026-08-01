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

For slash commands over HTTP interactions you only need the **`applications.commands`** scope — the
`bot` gateway scope is *not* required. **Skip the portal's "Install" button and the OAuth2 URL
Generator** (both are common sources of the "Invalid Form Body" error below) and open this URL
directly, with your Application ID filled in:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=applications.commands&integration_type=0
```

- `integration_type=0` = **Guild install** (adds to a server). `1` is user-install — not what we want.
- `applications.commands` needs **no** `permissions=` value.
- Open it → pick your test server → Authorize.

### Troubleshooting "Invalid Form Body" (error code 50035)

This is a validation error on the authorize request — the server is fine. Ranked causes:

1. **`bot` scope with no/invalid permissions.** The URL Generator adds `bot` and then a
   `permissions=` bitfield; an empty or malformed one fails validation. **Fix:** drop `bot` entirely
   (use just `applications.commands`, as above). If you truly want the bot as a member, add
   `&scope=bot%20applications.commands&permissions=0`.
2. **`integration_type` vs Installation-context mismatch.** If the portal's **Installation** tab has
   *Guild Install* disabled but the URL passes `integration_type=0` (or vice-versa), Discord rejects
   it. **Fix:** Portal → **Installation** → enable **Guild Install** under *Installation Contexts*;
   under *Default Install Settings → Guild Install* set **Scopes = `applications.commands`** and leave
   **Permissions** empty. If *User Install* is on and unused, turn it off.
3. **Malformed URL.** The space between multiple scopes must be `%20` (or `+`); a raw space or a
   stray character trips 50035. The single-scope URL above avoids this.
4. **Deprecated scope** → `SCOPE_INVALID`. Use exactly `applications.commands`.

The Application ID is **not** secret — paste it into the URL yourself, or share it and I'll hand you
the exact link.

Sources: Discord API error 50035 / "Invalid Form Body" and OAuth2 scope validation —
<https://github.com/discord/discord-api-docs/issues/2565>,
<https://discord.com/developers/docs/reference>.

## Order of operations

1. **Now (you):** create the app → grab the four values into Doppler `nexus`/`prd` → invite the app
   to your server (`applications.commands`).
2. **Then (me):** build Phase 2 — the `/discord/interactions` endpoint (verify + PING→PONG +
   defer≤3s → follow-up), register the `/nexus` guild command, render `Message` → Discord components;
   add the `DISCORD_*` names to `ExecStart`; add the Cloudflare ingress route.
3. **Then (you):** set the **Interactions Endpoint URL** in the portal (it validates against the
   now-running bridge), and run `/nexus status` in Discord to confirm.
