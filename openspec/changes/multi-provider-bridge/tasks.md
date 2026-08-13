## Status (2026-08-12)

In progress. Decisions locked (in-house adapters · HTTP interactions · one process ·
mirror `/nexus <args>`). Design merged (`docs/multi-provider-bridge.md`).

**Seam foundation landed (PR #38):** the normalized model + Slack render/parse + a
testable `SlackAdapter`, unit-tested.

**Phase 1 complete (2026-08-12):** `dispatchNexusCommand(command, reply)` is extracted
and provider-agnostic — verified to contain no Slack-shaped code — and `fleetPanel`
returns a normalized `Message` (raw Block Kit moved to `fleetPanelBlocks`, ridden into
`Message.blocks` via the documented escape hatch).

**Phase 2 code complete (2026-08-12), pending live verify:** `SlackAdapter` now owns
both Socket Mode listeners and the `response_url` POST; index.js has no inline Slack
ingress left. Suite 84/84, `node --check` clean.

**Restarted 2026-08-12** onto the 2.2 code (pid 968703, clean boot, socket handshake OK,
zero errors). 2.4's in-Slack re-test is still outstanding.

**Phase 3 code complete (2026-08-12), pending operator infra:** `DiscordAdapter` +
`messageToDiscord` + the interactions route are in, 115/115 tests. The adapter is inert
without `DISCORD_*` env, so this is a no-op for the current Slack-only deployment.

**⚠ Disk is ahead of the running process again.** Phase 3 touched the live Slack panel
path (`handleNexusButton` now delegates to the shared `dispatchNexusAction`), so the
panel needs re-testing after the *next* restart — 2.4 run against pid 968703 covers the
2.2 code, not this.

Rollback: `git checkout index.js providers/ orchestrator.js` + restart — no state,
schema, or config changed, so the revert is clean.

## 1. Provider seam + normalized model

- [x] 1.1 `slack-bridge/providers/types.js` — `Message {text?, ephemeral?, components?}`,
      `Command`, `Action`, and the `NexusProvider` shape (JSDoc). Pure, unit-tested. (PR #38)
- [x] 1.2 Extract the provider-agnostic `/nexus` dispatch from `index.js` into a core
      `dispatchNexusCommand(command, respond)` — same helpers (status/agents/peek/clear/
      stop/keep/msg/spawn/restore), no Slack types in the signature. (2026-08-12;
      callback named `reply` so the Slack-specific module-level `respond()` isn't
      shadowed. `eph()` is now `ephemeral()` from the normalized model, so all ~15
      helpers return `Message` unchanged in text.)
- [x] 1.3 `fleetPanel` (and any card builders) return a normalized `Message`, not Block
      Kit directly; add a Slack renderer `messageToBlockKit(Message)`. (2026-08-12;
      renderer already landed in 2.1. Raw Block Kit split into `fleetPanelBlocks` so the
      wire shape stays directly assertable. `replace_original` — Slack-only, no model
      equivalent yet — stays at the `handleNexusButton` call site rather than widening
      `Message`; task 3.4 introduces the portable "edit original" concept.)

## 2. Slack adapter (behavior-preserving refactor)

- [x] 2.1 `slack-bridge/providers/slack.js` — `SlackAdapter`: Socket Mode ingress →
      `Command`/`Action`; `reply` via `response_url` (ack-empty first); `send` via
      `chat.postMessage`; renders `Message` via `messageToBlockKit`. Pure fns + adapter
      unit-tested (injected socket/web/post). (PR #38)
- [x] 2.2 `index.js` wires `SlackAdapter` into the core; remove inline Slack I/O.
      (2026-08-12; both `socket.on('slash_commands')` and `socket.on('interactive')` are
      gone from index.js — the adapter is now the sole registrant of each, verified
      mechanically, since a leftover listener would double-handle every button click.
      `post: respond` reuses the existing error-swallowing POST so egress is unchanged.
      Reconciled the `view_submission` hole the adapter's header deferred to "wiring
      time": new `onViewSubmission(body, ack)` escape hatch hands `handleNexusSubmit`
      the RAW body and RAW ack — a modal submit acks WITH a response, so it must never
      be pre-acked, and it has no normalized equivalent yet. With no handler registered
      the envelope is dropped unacked, i.e. the pre-2.2 behavior is preserved.
      `onCommand` gained the raw envelope as a 2nd arg so the slash log line still
      prints what the user literally typed — bare `/nexus` normalizes to `home`, so the
      Command alone can't reproduce it.)
- [x] 2.3 Snapshot-test: Block Kit output for the panel + each reply is byte-identical
      to today. `node --check` + `npm test` green. (2026-08-12; 84/84. Every reply
      funnels through `messageToBlockKit`, which can emit exactly two shapes here, and
      both are pinned: ephemeral text (`messageToBlockKit(ephemeral(t))` deep-equals the
      old `{response_type:'ephemeral',text}`, covering all ~15 helpers at once) and the
      panel (4 states in orchestrator.test.js). Ack ordering is pinned too — a test
      asserts the slash ack fires strictly before the handler.)
- [ ] 2.4 **Verify live**: restart bridge; `/nexus status|agents|peek|<panel>` in Slack
      behaves exactly as before (needs an operator run).
      **← ONLY remaining Phase-2 task. Blocked on an operator at a terminal:**
      `systemctl --user restart slack-bridge`, then in Slack exercise `/nexus` (panel),
      `status`, `agents`, `peek`, a panel pick→act round-trip (checks `replace_original`),
      and one modal submit (checks the new `onViewSubmission` ack path).

## 3. Discord adapter

- [x] 3.1 `slack-bridge/providers/discord.js` — `DiscordAdapter`: `POST /discord/
      interactions` handler with Ed25519 verify (raw body) + PING→PONG. Unit-test verify
      against a known keypair/vector. (2026-08-12; verified against RFC 8032 test vector
      1 specifically because the SPKI DER wrapper is hand-built — Node's KeyObject API
      won't ingest a bare 32-byte Ed25519 key, and a wrong-but-self-consistent wrapper
      would still pass a generated-keypair round-trip while 401ing every real request.
      `handleInteraction` returns `{status, json, work}`: the caller writes the response,
      THEN runs `work()`. That split is what beats the 3s deadline and keeps the
      deferred-then-follow-up dance testable instead of a floating promise.
      **The HTTP route uses `Buffer.concat`, not the `body += chunk` the other routes
      use** — Discord signs exact bytes, and decoding chunks independently mangles any
      multi-byte character straddling a boundary, so the signature would fail only on
      large payloads and only sometimes. Pinned by a UTF-8 test.)
- [x] 3.2 Register the `/nexus` application command (string option `args`) at startup
      (idempotent PUT). (2026-08-12; bulk overwrite — the body IS the whole command set,
      so re-running can't duplicate. Fired non-awaited at startup and never fatal: a
      Discord outage must not stop the Slack bridge coming up.)
- [x] 3.3 Interaction (type 2) → `Command`; component (type 3) → `Action`. Reply =
      deferred (type 5, `flags:64` for ephemeral) within 3s, then follow-up webhook.
      (2026-08-12; `parseCommandText` now lives in types.js and is shared with Slack, so
      `/nexus msg a hello  there` parses identically on both by construction rather than
      by coincidence. Note the constraint: **Discord fixes ephemerality at ACK time**,
      before the handler has produced a Message — every `/nexus` branch is ephemeral on
      Slack, so we always defer ephemeral. An in-channel reply would need the ack to know
      in advance.)
- [x] 3.4 `messageToDiscord(Message)` — action rows / buttons / string selects; wire the
      panel pick→reveal via message edit. (2026-08-12; `fleetPanel` now carries BOTH
      `blocks` and `components` — `messageToBlockKit` prefers blocks so Slack stays
      deep-equal, `messageToDiscord` ignores them and renders components. Resolved the
      `replace_original` parking note from 1.3: the portable pair is `edit`/`post` on the
      new provider-agnostic `dispatchNexusAction`, which both adapters now drive.
      Discord specifics handled: a select must own its action row, buttons chunk at 5,
      rows cap at 5, selects cap at 25 options, and `custom_id` packs Slack's
      `action_id` + `value` as `id|value`.
      **Fixed a latent seam bug:** `interactiveToAction` read `a.value`, but Slack puts a
      select's choice in `selected_option.value` — so the normalized Action silently
      dropped the picked agent. Harmless until now only because `handleInteractive` read
      the raw body; normalizing the panel would have broken `nx:pick`.)
- [x] 3.5 Config: `DISCORD_BOT_TOKEN` / `DISCORD_PUBLIC_KEY` / `DISCORD_APP_ID` / path;
      adapter is inert (not started) when unset. Optional `DISCORD_GUILD_ID` registers
      `/nexus` to one guild (visible immediately) instead of globally (~1h to propagate)
      — a testing knob; the two scopes are INDEPENDENT, so a command registered to both
      appears twice in that guild. (2026-08-12; all three required
      together — a public key without a bot token can verify a request but not answer
      it — so a partial config is inert rather than half-working. Unset = Slack-only,
      byte-for-byte as before. `DISCORD_INTERACTIONS_PATH` overrides the route.)
- [~] 3.6 **Operator infra**: create the Discord app (bot + public key + app id), add a
      Cloudflare tunnel route → `/discord/interactions`, set the interactions endpoint URL.
      **App + secrets DONE (2026-08-12)**: app created, bot in the test server, all three
      values plus `DISCORD_GUILD_ID` in Doppler `nexus`/`prd`, unit allowlist extended,
      bridge restarted (pid 1205913). Verified locally without printing any value: all
      four present in the process env, the endpoint answers `401 invalid request
      signature` to a bad signature, and startup logged `/nexus registered (app 1533…,
      guild 5253… — visible immediately)` — Discord returns that only for a valid bot
      token AND app id AND a guild the bot actually belongs to.
      Global scope is ALSO still registered from the first run, so `/nexus` may appear
      twice in that guild once the global copy propagates; clear it with `PUT []` to the
      global commands URL if it becomes annoying.
      **PUBLIC ROUTE DONE (2026-08-13)** via Tailscale Funnel, not Cloudflare: no
      cloudflared on this host, and Caddy serves only a tailnet address on :80. Granted
      the node the `funnel` nodeAttr in the tailnet ACL, then `tailscale funnel --bg
      8789` → `https://nexus.rhino-augmented.ts.net/discord/interactions` (valid Let's
      Encrypt cert, confirmed reachable from off-tailnet). Public surface verified as
      exactly one route: the Discord path 401s a bad signature; `/send` `/relay`
      `/notify` `/request` `/health` `/agents` `/status` `/` all 404.
      Note: the first couple of requests after starting Funnel return curl `000` (no
      response) — cold start, not a fault; it stabilises within seconds.
      **BUT Discord will not accept the URL.** `PATCH /applications/@me` with
      `interactions_endpoint_url` returns 400 `APPLICATION_INTERACTIONS_ENDPOINT_URL_INVALID`
      ("could not be verified") — identical to the portal, so the portal/typo is not the
      variable. Crucially, the bridge logs ZERO traffic during the attempt: Discord's
      validator never opens a connection. Everything on our side is verified good:
      external third-party traffic DOES reach :8789 through the funnel (proven — an
      off-network GET produced an `unmatched GET` log line), TLS is a valid LE cert, the
      path matches (trailing slash tolerated), a bad signature 401s, and
      `DISCORD_PUBLIC_KEY` is byte-equal to the app's own `verify_key` per
      `GET /applications/@me`. Bot token + app id are proven by successful command
      registration.
      Untested variable: this host has NO IPv6 egress, and the funnel publishes AAAA
      records — if Discord's validator prefers IPv6 and Tailscale's v6 ingress is
      unreachable from their network, the symptom is exactly this silence. Also possible:
      Discord declines `.ts.net` hosts outright.
      **RESOLVED (2026-08-13): Discord will not connect to a `.ts.net` Funnel host.**
      Fronted the SAME port 8789 with a named Cloudflare tunnel
      (`nexus-discord.augmented-rhino.com`) and the endpoint verified on the first
      attempt — nothing else changed. Confirmed by the bridge log finally showing
      Discord's two-request handshake: a 736-byte bad-signature probe (correctly 401) and
      a signed PING (correctly PONG). The 401 line during verification is EXPECTED, not a
      fault; body size distinguishes real Discord traffic (~700B) from hand-rolled probes
      (~10B).
      Setup: `~/.cloudflared/config.yml` (tunnel `nexus-discord`,
      id 73677ddd-2a34-4888-ae16-e208ef8d5fbf) with ingress scoped to hostname AND
      `path: ^/discord/interactions/?$` → `http://127.0.0.1:8789`, catch-all
      `http_status:404`. Runs as user unit `cloudflared-nexus-discord.service`
      (enabled). Verified publicly: Discord route 401, `/send` `/relay` `/health` `/`
      all 404 — refused at the tunnel, never reaching an origin.
      Tailscale Funnel has since been closed — `.ts.net` is unreachable and Cloudflare is
      the sole public path. The global `/nexus` command scope was also cleared (PUT `[]`)
      so the guild-scoped registration is the only one, removing the duplicate entry.
      **NEVER tunnel port 8788.** It also serves `/notify`, `/send`, `/request` and
      `/relay`, which are unauthenticated *by design* because they are loopback-bound and
      trusted-local — `/send` and `/relay` inject text into agents. Exposing that origin
      would publish unauthenticated agent-command injection to the internet.
      Mitigated in code: `DISCORD_HTTP_PORT` (set to 8789 in the unit) serves the
      interactions route and NOTHING else on its own loopback port, and the route moves
      off 8788 entirely when it is set. Verified: on :8789 the Discord route answers 401
      and `/send` `/relay` `/notify` `/request` `/health` `/agents` `/status` all 404.
      That is structural isolation, not a proxy path rule a later `tailscale funnel 8788`
      could undo. **Point the tunnel at 8789.**
      Gotcha hit on the way: the secret was named `DISCORD_APPLICATION_ID`, and
      `secret-run.sh` skips unresolvable names silently, so the service simply came up
      inert with no diagnostic.
- [~] 3.7 **Verify live**: `/nexus status|agents|peek` in Discord; then the panel.
      **`/nexus status` CONFIRMED WORKING END-TO-END (2026-08-13)** — real interaction →
      Ed25519 verify → deferred ack → `dispatchNexusCommand` → follow-up edit, with the
      correct fleet output rendered in Discord. Zero follow-up failures, zero interaction
      errors, zero runtime errors. The shared core answered Discord unchanged, which is
      the seam's whole premise.
      **PANEL VERIFIED TOO (2026-08-13)**: `/nexus` bare renders the select + buttons
      from portable `components`; `nx:pick` edits the panel in place; `nx:do:peek` posts
      beside it. Both halves of the `edit`/`post` pair confirmed against real Discord.
      Two live-only defects found and fixed — neither could have surfaced on Slack:
      1. **Slack text dialect.** `:large_green_circle:` and `*bold*` render literally on
         Discord. Added `slackTextToDiscord` in the Discord renderer (Slack output is
         byte-identical, verified). Substitutes KNOWN emoji keys only and skips code
         spans — a blanket `/:\w+:/` strip would have turned `nx:do:peek` into `nxpeek`.
      2. **2000-char content limit.** Discord 400s an over-length message and drops the
         WHOLE reply; Slack has no equivalent cap, so a 40-line `peek` (~3.9kB) had never
         been a problem. `capContent` truncates to 2000 and balances a code fence if the
         cut lands mid-block. Failure logging now reports Discord's field-level error and
         our payload size, so a length problem is diagnosable from the status code alone.
      Untested, low risk: `agents` and `peek` as slash commands (same text path as
      `status`, which passes).

### Known gaps (not blockers)

- Reply text is Slack-flavored: `:warning:`/`:rocket:` render as literal colons on
  Discord, and `*bold*` is italic there. Cosmetic; a `Message.text` dialect pass belongs
  with 4.x if it matters.
- `dispatchNexusCommand` / `dispatchNexusAction` still live in `index.js`, which cannot
  be imported by a test (it opens a Socket Mode connection at module load). They are
  verified structurally, not by unit test. Extracting them to `core.js` would close that
  and is cheap — worth doing before Phase 4 adds fan-out logic beside them.
- `/nexus spawn` on Discord replies over Discord, but `doSpawn` still posts its *result*
  to the Slack control channel via `chat.postMessage`. Phase 4's `Notifier` is where that
  gets routed per-provider.

## 4. Multi-provider fan-out

- [x] 4.1 `Notifier` holding registered providers; `fanout(Message, {providers})`.
      (2026-08-13; `providers/notifier.js`, pure + unit-tested, not yet wired — 4.2 is
      what makes it live. Two deliberate properties: **failures are isolated** via
      `Promise.allSettled`, so a Discord outage cannot swallow a permission card that
      also had to reach Slack; and **the default is origin-only**, because broadcasting
      by default would double every notification the moment a second provider registers.
      `targets` maps provider → channel id, since a Slack channel id is meaningless to
      Discord; a provider with no target is reported `skipped`, never silently dropped.
      The Notifier does no rendering — `messageToBlockKit`/`messageToDiscord` own that.)
- [ ] 4.2 Route outbound notifications (permission cards, done-pings, presence alerts)
      through `Notifier`; per-provider enable env; default = originating provider only.
- [ ] 4.3 Docs: `/nexus` command reference gains Discord; `docs/multi-provider-bridge.md`
      "next step" resolved.

## Non-goals (this change)

- Discord Gateway/message events (only application-command + component interactions).
- Native Discord subcommands (mirror `/nexus <args>` instead).
- Additional providers (Teams/Telegram) — the seam makes them additive later.
