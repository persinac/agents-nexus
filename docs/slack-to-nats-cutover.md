# Slack → NATS A2A cutover guide

Move agent-to-agent (A2A) messaging off the Slack `#nexus-agents` bus onto a **NATS + JetStream**
transport. Works for a **single host** (one box, local broker) and a **multi-host** fleet (a shared,
reachable broker). The **human notify/reply leg stays on Slack** in both modes — `#nexus`, `/notify`,
threads, and `/relay` are unchanged. Only A2A (`/send`, presence, inbound delivery) moves.

> **Nothing new to build.** The transport is already implemented and shipping. This is a config +
> broker cutover. See also `docs/slack-bridge.md#nats-transport` and
> `openspec/changes/nats-a2a-bus-transport/`.

---

## What actually changes

- **`agent-send.sh` is unchanged** — it still `POST`s `:8788/send`. The transport swap is entirely
  bridge-side, so no agent-facing verbs change (`<host>/<name>`, `--relay`, `--via-slack` all keep working).
- The **stream, per-host durable consumer, and presence KV bucket are auto-provisioned by the bridge
  on startup** — idempotent (`ensureStream`/`ensureKv`, `add`-if-missing). There is **no separate
  setup script**: bring up the broker, start the bridge, done.
- Rollback is a one-line env flip + bridge restart. No data migration.

**Auto-provisioned on first boot** (from `NATS_A2A_*` env):
| Object | Default | Purpose |
|---|---|---|
| JetStream stream | `NEXUS_A2A` (subjects `nexus.a2a.>`) | durable inbox + bounded-retention audit log |
| Durable consumer | `bridge_<selfhost>` (filter `nexus.a2a.<selfhost>.>`) | this host drains only its own subtree; broker routes point-to-owner (no fan-out) |
| Presence KV bucket | `nexus_presence` (FQDN-keyed, TTL-aged) | who's reachable; `/agents` reads this in nats mode |

Subjects are `nexus.a2a.<host>.<workspace>.<name>` (the tested codec in `orchestrator.js`).

---

## Two topologies

| | **single-host** | **multi-host** |
|---|---|---|
| Broker | local `nats` container on `:4222` | hosted NATS, reachable by IP/domain |
| `NEXUS_A2A_MODE` | `single-host` | `multi-host` |
| Slack A2A paths | **OFF** (NATS is the sole A2A medium; no `#nexus-agents` listening, no Slack presence gossip) | **ON** as a fallback/migration shadow (Slack A2A + gossip still run) |
| Presence | JetStream KV only (`/health` → `presence:false` for Slack gossip; reachability via KV) | KV **and** Slack gossip |
| Auth | none needed (loopback) | **required** — token or NKEY/JWT creds + TLS |
| `NATS_URL` | `nats://127.0.0.1:4222` | `tls://broker.internal:4222` (the box's reachable addr) |

Rule of thumb: **single-host** = you, one machine, no peers → simplest, no auth, no dual path.
**multi-host** = 2+ boxes on one broker → hosted broker + auth, and every box sets `multi-host`.

---

## Environment variables

All live in the active profile `.env` (git-ignored). The bridge reads them fill-gaps at startup.

### Core (both modes)
| Var | Value | Notes |
|---|---|---|
| `NEXUS_BUS_TRANSPORT` | `nats` | `slack` (default) \| `nats`. The transport selector. |
| `NEXUS_A2A_MODE` | `single-host` \| `multi-host` | Topology. single-host forces NATS + disables Slack A2A. Default `multi-host`. |
| `NATS_URL` | `nats://127.0.0.1:4222` | In-stack from another container: `nats://nexus-nats:4222`. Cross-host: the broker's reachable address. |
| `NATS_A2A_STREAM` | `NEXUS_A2A` | JetStream stream name. |
| `NATS_A2A_SUBJECT_PREFIX` | `nexus.a2a` | Subject root. |
| `NATS_PRESENCE_KV` | `nexus_presence` | Presence KV bucket. |
| `SLACK_BUS_ENABLED` | `1` | **Master switch** — must stay `1`; gates `POST /send` + delivery even under NATS. |

> `SLACK_AGENTS_CHANNEL` is **not required** for NATS A2A (single-host ignores it). Keep your
> Slack bot/app tokens set — the human notify/reply leg still uses them.

### Auth (multi-host — pick ONE; not needed for a loopback single-host broker)
| Var | Value | Notes |
|---|---|---|
| `NATS_CREDS` | path to a `.creds` file | NKEY/JWT — the scale/prod path (subject-scoped). |
| `NATS_TOKEN` | shared token | simple auth. |
| `NATS_USER` / `NATS_PASS` | user/password | basic auth. |

### Compose (single-host local broker only)
| Var | Default | Notes |
|---|---|---|
| `NATS_PORT` | `4222` | client port mapping |
| `NATS_MONITOR_PORT` | `8222` | HTTP monitor (`/healthz`, used by the container healthcheck) |

### Tuning (optional, sane defaults in code)
- Stream retention / offline-buffer horizon = `max_age` (the audit window).
- Consumer redelivery lease `ackWaitMs` (default 5 min) — must exceed how long the idle-gate may hold a message.
- Presence KV TTL — entries expire unless the heartbeat refreshes them.

---

## Installer flow

The interactive `./install.sh` has an **A2A bus transport** step:

```
A2A bus transport — how agents message each other:
  slack — via the #nexus-agents channel (default; fine for a single box)
  nats  — via a NATS+JetStream broker (durable; required for a cross-machine fleet)
Transport (slack/nats): nats
```

Choosing `nats` then branches on the broker location:

```
Run a LOCAL NATS container on this box (adds the 'nats' compose profile)? [Y/n]
```

- **Yes → single-host.** Sets `NATS_URL=nats://127.0.0.1:4222`, adds the `nats` compose profile,
  writes `NEXUS_A2A_MODE=single-host`. No auth prompts (loopback).
- **No → multi-host.** Prompts for `NATS_URL` (e.g. `nats://nexus-box:4222` / `tls://broker.internal:4222`),
  then `NATS_CREDS` (path, optional) or `NATS_TOKEN` (secret, optional). Writes `NEXUS_A2A_MODE=multi-host`.
  You can defer auth and finish later.

The installer writes the `# ── A2A bus transport: NATS + JetStream ──` block into `.env`
(`NEXUS_BUS_TRANSPORT=nats`, the mode, `NATS_URL`, any creds/token, `NATS_A2A_STREAM`,
`NATS_A2A_SUBJECT_PREFIX`), brings up the `nats` profile if local, and starts/restarts the bridge.

**Deferred cross-machine auth** — the dedicated re-entry point:
```bash
./install.sh --finish-nats     # set/replace NATS_URL + auth on a nats box, restart the bridge
```
It refuses unless `NEXUS_BUS_TRANSPORT=nats` is already set, replaces `NATS_URL` in place, drops any
old `NATS_CREDS`/`NATS_TOKEN`, appends the new auth, and restarts.

---

## Cutover — single host

1. **Choose NATS + local broker** in `./install.sh` (or set the env block by hand), answering **Yes**
   to the local-container prompt. This writes:
   ```
   NEXUS_BUS_TRANSPORT=nats
   NEXUS_A2A_MODE=single-host
   NATS_URL=nats://127.0.0.1:4222
   NATS_A2A_STREAM=NEXUS_A2A
   NATS_A2A_SUBJECT_PREFIX=nexus.a2a
   ```
2. **Bring up the broker:**
   ```bash
   docker compose -f docker-compose.work.yml --profile nats up -d nats
   ```
   (`nats:2.10-alpine`, `container_name: nexus-nats`, JetStream on, data on the `nats-data` volume,
   ports 4222 + 8222.)
3. **Restart the bridge** so it reads the new env and provisions the stream/consumer/KV:
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.agents-nexus.slack-bridge   # mac
   # or restart however the bridge runs on this box
   ```
4. **Verify:**
   ```bash
   curl -s localhost:8788/health
   # expect: "transport":"nats","a2a_mode":"single-host","nats":true,"presence":false
   agent-send.sh --via-slack <a-live-agent-name> "ping via nats"
   # → delivered; JetStream stream + durable consumer advance by 1
   ```

> **Keep `nexus-nats` up.** If the broker is down in nats mode, cross-agent A2A stops (same-host
> name sends still fall back to instant `send-keys`).

---

## Cutover — multi-host

Do the single-host steps on the box that will **host** the broker first (or stand up a dedicated
NATS box), then point every participant at it.

1. **Stand up a reachable, authenticated broker.** Either promote the local container (bind beyond
   loopback, open the firewall for `:4222`, add **TLS + creds/token**) or run a dedicated NATS server.
   A loopback-only broker will **not** work cross-host.
2. **On every box** (broker host included), set:
   ```
   NEXUS_BUS_TRANSPORT=nats
   NEXUS_A2A_MODE=multi-host
   NATS_URL=tls://<broker-ip-or-domain>:4222
   NATS_CREDS=/path/to/box.creds        # or NATS_TOKEN=… / NATS_USER + NATS_PASS
   NATS_A2A_STREAM=NEXUS_A2A
   NATS_A2A_SUBJECT_PREFIX=nexus.a2a
   ```
   Fastest path on an already-nats box:
   ```bash
   ./install.sh --finish-nats     # point NATS_URL at the shared broker + set auth, restart
   ```
3. **Restart each bridge.** Each provisions its own durable consumer `bridge_<selfhost>` (filtering
   only `nexus.a2a.<selfhost>.>`), so the broker routes each message to the owning host — no fan-out.
4. **Verify per box:**
   ```bash
   curl -s localhost:8788/health     # "transport":"nats","a2a_mode":"multi-host","nats":true
   curl -s localhost:8788/agents     # combined fleet (reachability from the presence KV)
   ```
   Cross-host round-trip:
   ```bash
   agent-send.sh <other-host>/<name> "cross-host ping"
   ```

**Multi-host keeps the Slack A2A path live** as a shadow/fallback during migration. Once every box
is confirmed on NATS, you can flip a fully-migrated single-owner box to `single-host` to drop the
dual path — but on a true multi-box fleet, leave it `multi-host`.

---

## Rollback (instant, either mode)

```bash
# in the active .env:
NEXUS_BUS_TRANSPORT=slack
# (or remove the NATS block entirely)
launchctl kickstart -k gui/$(id -u)/com.agents-nexus.slack-bridge
```

Bridge returns to the Slack `#nexus-agents` bus + in-memory idle-gate. No data loss
(`slack-threads.json` persists; the JetStream stream is left intact for re-cutover).

---

## Gotchas (learned the hard way)

- **`nats` npm is deprecated.** The bridge uses the v3 scoped packages: `@nats-io/transport-node`,
  `@nats-io/jetstream`, `@nats-io/kv` (all `^3.4.0`). v3 is functions-not-methods (`jetstream(nc)`).
- **`npm install` in `slack-bridge/` before flipping.** Those `@nats-io/*` deps are in
  `package-lock.json` but a `git pull` does **not** install them — a box that installed *before* they
  were added logs `Cannot find package '@nats-io/transport-node'` and silently stays on Slack (bridge
  runs, but `transport:nats` + `nats:false`). A full `./install.sh` runs it; `./install.sh --finish-nats`
  now runs it too. Flipping by hand (editing `.env`)? Run `( cd slack-bridge && npm install )` first.
- **Non-overlapping subject spaces.** JetStream forbids two streams whose subjects overlap. Anything
  you provision must own a distinct subject prefix, or stream creation fails (`subjects overlap`).
- **Use the alpine image for healthchecks.** `nats:2.10-alpine` has a shell + `wget`; the distroless
  `nats` image has neither, so the `/healthz` healthcheck fails.
- **KV keys forbid `~`** (subjects allow it) — the codec escapes with `~` for subjects and `=` for KV
  keys. Already handled; noted so you don't "fix" it.
- **`SLACK_BUS_ENABLED=1` is still the master switch** even under NATS — leave it on.
