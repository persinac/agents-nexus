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
2. **Addressed top-level** message `<target>: your message` (optionally after
   `@nexus-bot`) → resolved against the live registry. The **delimiter is colon-SPACE**
   (`": "`); `<target>` may be:
   - a **name** — `general: …`
   - a **`[host/][workspace/]name`** — `search/example-service: …`, `mac/general: …`
   - a **pane handle** `wN:pN` — `wQ:pF: …` (instance-exact; the only way to disambiguate
     two agents that share a name, e.g. two `general`s in the `interactive` bucket)
   - a **slot number** — `3: …`
   Unknown/ambiguous target → the bot logs *why* (never a silent drop) and, for a bare
   name that collides, tells you to re-address by pane handle. Regression to know: a line
   with **no space after the colon** (`general:hi`) is not treated as addressed — put a
   space after the colon. (See `docs/agent-bus-instance-addressing.md`.)
3. **Anything else** (no address, untracked thread) → **smart routing**: a haiku
   classifier scores the message against the active agents (name + working dir)
   and, above the confidence floor (`SLACK_ROUTE_MIN_CONFIDENCE`, default 0.6),
   delivers to the best match and replies `routed to \`name\` (auto · NN%)`.
   Below the floor (or no clear match) it falls back to the usage hint and asks
   you to address an agent explicitly. Set `SLACK_ROUTE_ENABLED=0` to disable;
   routing also no-ops when `ANTHROPIC_API_KEY` is unset.

Only the configured `#nexus` channel and DMs to the bot are acted on; messages in
any other channel are ignored.

## Commands

Type these as a top-level message in `#nexus` **or a DM to the bot**. The reply is
posted back to Slack (the channel/DM the request came from) — never to the terminal.

| Command | What it does |
| --- | --- |
| `status` · `status all` · `who` | Fleet roll-up — every live agent with its state: :large_green_circle: **working** · :white_circle: **idle** (at the prompt) · :large_yellow_circle: **waiting on you** (permission prompt) — plus time-in-state, repo, and a :warning: *stuck* flag when a "working" agent hasn't run a tool in a while. |
| `status <name\|slot>` | One agent, with extra detail (git branch, time since last tool). |
| `restore [repo]` | Restore a reaped/dormant agent from the ledger; bare `restore` lists dormant agents. _(needs `SLACK_SPAWN_ENABLED=1`)_ |
| `keep <name> [on\|off]` | Pin/unpin an agent so the reaper skips it even under `REAP_ALL=1`. _(needs `SLACK_SPAWN_ENABLED=1`)_ |
| `spawn <repo> [seed]` | Spawn an agent in an allowlisted repo, optionally seeded with a task. _(needs `SLACK_SPAWN_ENABLED=1`)_ |

`status` / `who` are **read-only and always available** — they do **not** require
`SLACK_SPAWN_ENABLED`. The bridge computes them itself from the agent registry and
each window's hook-maintained `@waiting` state (the same signal the arbiter and
reaper read), so they answer instantly and work even when an agent is busy or
wedged — no round-trip to the agent. The same data is exposed as JSON at
`GET /status` on the localhost port for CLI checks.

`SLACK_STATUS_STUCK_MIN` (default `10`) sets how many minutes a "working" agent can
go without running a tool before the roll-up flags it `:warning: stuck`.

### Delivery feedback: receipts + completion ping

So you're not guessing whether a busy agent got your message — especially on mobile,
with an agent cruising in auto-mode:

- **Receipt with state.** When you address an agent (`name: …`, or via smart
  routing), the bridge replies with the agent's state at delivery — `on it now`
  (was idle), `queued — it'll pick this up at its next turn` (was mid-task), or a
  heads-up that it's at a permission prompt. (The ✅ reaction still appears too.)
- **Completion ping.** When an agent **you messaged** finishes its turn and settles
  back at the prompt, the bridge posts *":white_check_mark: `name` finished — now
  idle"* to the same channel/DM. It's gated to agents you actually messaged (not
  every idle transition), and it waits for the agent to do work and *then* hold idle
  for a debounce window, so auto-mode's between-turn flicker doesn't ping early.

| Var | Default | Meaning |
| --- | --- | --- |
| `SLACK_DONE_PING` | `1` (on) | Master switch for the completion ping. `0` disables it (receipts still post). |
| `SLACK_DONE_STABLE_MS` | `20000` | How long an agent must stay idle before it's called "finished" (debounce). |
| `SLACK_DONE_POLL_MS` | `5000` | How often messaged agents are polled. |
| `SLACK_DONE_TTL_MS` | `1800000` | Give up tracking a messaged agent after this long if it never settles (30 min). |

## Orchestrator: spawn the right agent (opt-in)

When routing finds **no** running agent for a message (step 3 falls through), the
bridge can offer to spin one up instead of just showing the usage hint. This is
**off by default** — set `SLACK_SPAWN_ENABLED=1` to enable it.

Flow:

1. **Resolve the repo.** An LLM classifier (haiku) picks which *spawnable* repo
   the message concerns, matching it against each allowlisted repo's name +
   description. This replaced a Spark index lookup — we only ever spawn
   allowlisted repos, so classifying within that small, described set is more
   reliable than embedding a one-line message against the whole index.
   (`scripts/spark-resolve.py`, the MCP resolver, stays in-tree but is no longer
   on the spawn path.)
2. **Allowlist + path.** The repo must be a key in the spawnable-repo allowlist
   (`~/.tmux/spawnable-repos.json` by default) — a JSON object mapping repo name →
   `{ "path": "/abs/checkout", "desc": "what it is" }` (a bare path string still
   works). This is both the safety gate and the name→path resolver (Spark indexes
   many repos that aren't cloned here). A repo not on the list is never offered.
   The **descriptions** feed the classifier and auto-fill from Spark:
   `scripts/spark-summary.py` distills each repo's Spark `installation_summary`
   into `~/.tmux/spark-summaries.json`, which the bridge merges in — a hand-written
   `desc` always overrides. `nightly-spark.service` refreshes that cache after each
   nightly index sync (`ExecStartPost`).
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
| `SLACK_SPAWN_ALLOWLIST_FILE` | `~/.tmux/spawnable-repos.json` | JSON `{ "repo": { "path": "/abs", "desc": "…" }, … }` (a bare path string is also accepted). See `slack-bridge/spawnable-repos.example.json`. |
| `SLACK_SPAWN_SUMMARIES_FILE` | `~/.tmux/spark-summaries.json` | Spark-derived description cache (`scripts/spark-summary.py`); fills any repo with no hand-written `desc`. Refreshed nightly. |
| `SLACK_SPAWN_SESSION` | `agents` | tmux session to spawn into. |
| `SLACK_SPAWN_MIN_CONFIDENCE` | `0.5` | Min repo-classifier confidence to offer a spawn. |
| `SLACK_SPAWN_MIN_SCORE` | `0` | _Legacy_ Spark-resolver score floor — the resolver is off the spawn path; kept for the in-tree `spark-resolve.py`. |
| `SLACK_SPAWN_RATE_MAX` | `3` | Max spawns per rolling window. |
| `SLACK_SPAWN_RATE_WINDOW_MS` | `600000` | Rate-limit window (10 min). |
| `SLACK_SPAWN_CONFIRM_TTL_MS` | `300000` | Confirm-card lifetime before it expires + releases the lock (5 min). |
| `SLACK_NUDGE_MIN_INTERVAL_MS` | `3600000` | Min gap between reconnect nudges (1 h). |
| `SLACK_SPARK_PYTHON` | `$AGENTS_NEXUS_DIR/spark/.venv/bin/python` | Interpreter for the Spark resolver/summary scripts (needs the `mcp` SDK). |
| `SLACK_OPEN_CLAUDE` | `~/.tmux/open-claude.sh` | Launch script used for spawns/restores. |
| `AGENT_LEDGER` | `~/.tmux/agent-ledger.jsonl` | Durable agent ledger path (shared with the reaper). |

These can be set via Doppler (`nexus/prd`) alongside the other `SLACK_*` secrets, or
in the gitignored `.env`. The Spark resolver and ledger are only invoked when
`SLACK_SPAWN_ENABLED=1`.

## Inter-agent bus (opt-in)

`agent-send.sh` is **dual-mode**. A **local** target (a pane/slot, or a name in this
host's registry) is delivered with `tmux send-keys` exactly as before — instant, no
network. A **non-local** agent name is routed through the bridge so agents on *other
hosts* can be reached. The bus is **off by default**; disabled, `agent-send.sh`
behaves exactly as it always has.

Flow:

1. `agent-send.sh <name> <msg>` — if `<name>` is local, `send-keys` and done.
2. Otherwise (bus enabled) it POSTs `{ to, from, msg }` to the bridge's `POST /send`.
3. The bridge publishes `to: ↩ from <from>: <msg>` to the dedicated **`#nexus-agents`** channel.
4. Every host's bridge sees that over Socket Mode; the one whose registry owns `to`
   delivers it locally (`handleBusMessage`). The others ignore it — no host delivers twice.
5. The recipient sees `↩ from <sender>: …` and can reply with `sender: <reply>` back through the bus.

`agent-send.sh --via-slack <name> <msg>` forces the bus path even for a local target;
`--local` forces the fast `send-keys` path.

**Namespaced addressing (`host/name`).** A bare `<name>` is matched against each
bridge's *own* registry — fine within one fleet, but ambiguous the moment two people
share one `#nexus-agents` (both have a `general`, an `orchestrator`, …). Address a
specific person's box with `agent-send.sh <host>/<name> <msg>`: the `host/` prefix is
matched against the target bridge's `SLACK_PRESENCE_HOST` (see [cross-person](#cross-person-two-people-one-bus)),
so only that bridge delivers. A namespaced target is inherently cross-machine, so it
always routes through the bus (and errors clearly if the bus is off). Bare names are
unchanged. See the presence [collision note](#phase-2--presence-registry-opt-in-slack_presence_enabled)
for why the prefix also bypasses owner-election.

**Relay (share output, don't paste it).** `agent-send.sh --relay <text>` posts `<text>`
to `#nexus-agents` for a **human** to read — no target, no delivery. It's the
copy-paste killer: instead of pasting your agent's output into a Slack DM, relay it
into the shared channel where the other person (and their agents' operator) sees it.
Relays are tagged `↩ relay from <agent>@<host>` and are routed out of the delivery
path by a `::nexus-relay::` sentinel, so a relay whose body starts with `word:` is
never mistaken for a `name:` delivery. Multi-line output keeps its shape. Bus-only.

### Same-host routing (`SLACK_A2A_SAMEHOST`)

By default same-host A2A uses the instant `send-keys` path. Set `SLACK_A2A_SAMEHOST=channel`
(in the **agent** env) to route same-host messages through `#nexus-agents` instead.
The reason isn't visibility — it's **buffering**: a raw `send-keys` into an agent that's
mid-task gets lost (or interrupts the run). Routing through the bus lets the bridge
**hold** the message and deliver it only when the recipient is idle (see below), so an
agent's running task is never interrupted and the message is never dropped.

In channel mode, **every form of addressing a registered agent goes through Slack** —
a NAME routes as-is, and a `slot` / `%pane` target is **reverse-resolved to that agent's
registry NAME** first (the bus keys on name) so it round-trips too. Only two cases stay
local: a **bare control digit** (a permission-menu input — idle-gating it would deadlock
the prompt) and a **window with no registered agent** (there's no name to route by).

> The bus keys on agent **names**, so a slot/%pane is mapped to its name before posting;
> if you watch `#nexus-agents`, that's why a message you sent to `slot 4` shows up
> addressed to the agent's name.

> Set `SLACK_A2A_SAMEHOST=channel` only in the **agent** env (`~/.tmux/env.sh`), **not**
> in the bridge's env — and the bridge additionally forces `SLACK_A2A_SAMEHOST=local` on
> its own delivery calls, so a final-hop send-keys can never re-route back through the
> bus and loop, regardless of ambient config.

**Launch-caveat nudge.** `SLACK_A2A_SAMEHOST` is read at agent **launch**, so an agent
started before you set `=channel` keeps doing local `send-keys` until relaunched — the
classic "why didn't it hit the channel?" trap. To surface it, when the bus is on but a
message to a real agent goes local only because same-host routing is off, `agent-send.sh`
prints a one-line **stderr** note (`delivered locally — SLACK_A2A_SAMEHOST≠channel …`).
It's silent for digits/unregistered windows (which stay local by design), in channel mode,
and for the bridge's own deliveries (`SLACK_A2A_NUDGE=0`).

### Idle-gated delivery (`SLACK_BUS_DEFER`, default on)

Every bus delivery is gated on the recipient's `@waiting` window-option (the
hook-maintained state the arbiter + reaper also read): the bridge injects with
`send-keys` only when the agent is **idle at the prompt** (`@waiting=2`). When it's
**working** (`0`/unset) or at a **permission prompt** (`1`), the message is held in a
per-pane queue and flushed when the agent next goes idle — one message per idle window,
so each inter-agent message gets its own turn. `#nexus-agents` is the durable record
(replay/audit); the queue makes delivery non-lossy and non-interrupting. Set
`SLACK_BUS_DEFER=0` to revert to immediate `send-keys`.

### Human-typing guard (`SLACK_BUS_HUMAN_GRACE_MS`, default off)

`@waiting=2` means the Claude **process** is idle — but that's also exactly when a
**human** may be composing a draft in that pane, and a `send-keys` inject then
interleaves with their keystrokes. On top of the `@waiting` gate, the bridge can
additionally hold a message while a human is actively typing **into the recipient
pane**, detected from tmux `client_activity`: the pane must be the **focused** pane of
its session (`window_active=1` **and** `pane_active=1` — the latter alone just means
"active within its own window"), and an attached client on that session must have sent a
keystroke within the grace window. The message stays in the same per-pane queue and
flushes on the next poll once typing goes quiet (or you hit Enter, which flips
`@waiting` busy and the idle gate takes over). Only the agent you're currently focused
on is affected; traffic to other agents is unchanged. Set
`SLACK_BUS_HUMAN_GRACE_MS` to the recency window in ms (e.g. `10000`); `0` disables it.

> **Scope of the signal.** This catches *active* typing. A draft you type and then walk
> away from (idle past the grace) ages out and will be delivered into the parked draft —
> covering that needs a content-based `capture-pane` check (see IDEAS #32). Cross-host
> keystrokes aren't visible to another host's bridge, but a human only ever types on one
> host, so that's inherent, not a gap. Fail-open: any tmux error → treated as "not
> typing" (the `@waiting` gate is still the primary guard), so a transient error reverts
> to today's behavior rather than deferring forever.

### Bus config (env)

| Var | Default | Meaning |
| --- | --- | --- |
| `SLACK_BUS_ENABLED` | `0` (off) | Master switch. On the **bridge** it enables `POST /send` + delivery on `#nexus-agents`. In an **agent's** env it tells `agent-send.sh` to attempt the bus for a non-local name (else it prints "Agent not found", as today). |
| `SLACK_AGENTS_CHANNEL` | — | Channel id of `#nexus-agents`. Required on the bridge for the bus to be live. |
| `SLACK_BRIDGE_PORT` | `8788` | Port `agent-send.sh` POSTs `/send` + `/relay` to (shared with `/notify`). |
| `SLACK_A2A_ENTER_DELAY` | `0.4` | Agent env. Seconds `agent-send.sh` waits after a literal `send-keys` paste before the submit `Enter`, so the TUI doesn't coalesce them into a newline (message lands but never sends). |
| `SLACK_A2A_SAMEHOST` | `local` | **Agent env.** `local` = same-host A2A via instant `send-keys`; `channel` = route same-host targets (name, or slot/%pane resolved to name) through the bus so they're buffered + idle-gated. |
| `SLACK_A2A_NUDGE` | `1` | Agent env. `1` = print the launch-caveat stderr note when a message to a real agent goes local only because routing is off. The bridge sets `0` on its own deliveries. |
| `SLACK_BUS_DEFER` | `1` (on) | **Bridge.** Idle-gate bus delivery: inject only when the recipient is idle (`@waiting=2`), else queue + flush on idle. `0` = immediate `send-keys`. |
| `SLACK_BUS_FLUSH_MS` | `4000` | Bridge: how often the queue-flush poll runs. |
| `SLACK_BUS_QUEUE_MAX` | `50` | Bridge: max held messages per pane; oldest dropped beyond (still in `#nexus-agents`). |
| `SLACK_BUS_HUMAN_GRACE_MS` | `0` (off) | **Bridge.** Also hold a bus message while a human is actively typing into the recipient's **focused** pane (via tmux `client_activity`), so an inject doesn't clobber a draft. Value = keystroke-recency window in ms (e.g. `10000`); `0` disables. Requires `SLACK_BUS_DEFER` on. |
| `SLACK_PRESENCE_ENABLED` | `0` (off) | **Phase 2.** On the bridge, enables the presence registry (announce live agents on `#nexus-agents`, consume peers, single-owner delivery, `GET /agents`). Requires the bus on. Off → Phase 1 host-local delivery, unchanged. |
| `SLACK_PRESENCE_HEARTBEAT_MS` | `300000` | How often the bridge re-announces its full agent set (also re-announces on every registry change). |
| `SLACK_PRESENCE_TTL_MS` | `960000` | A peer host is dropped from the map if no snapshot arrives within this window (crash/offline). |
| `SLACK_PRESENCE_HOST` | `hostname()` | Override the host label this bridge announces under (the owner-tiebreak key). |
| `SLACK_PRESENCE_FQDN` | `0` (off) | **Bridge.** Publish presence as **v2** per-instance `{name, workspace, pane}` records instead of a bare-name list, so two same-named agents on one host are distinct and addressable by `host/workspace/name` or `host/pane`. Consume-side is always v1+v2 tolerant; this flag only controls what this bridge *publishes* (degrades to v1 among pre-FQDN peers). Requires presence on. |

> **Two processes, two envs:** the **bridge** reads these from its process env — Doppler
> (`nexus/prd`) on the Linux box, or repo-root `.env` on a vanilla Mac (commit `73637b0`,
> no Doppler wrap); **agents** read `SLACK_BUS_ENABLED` from their shell env (e.g.
> `~/.tmux/env.sh`). Both must have it set for an end-to-end remote send. `curl
> :8788/health` reports `"bus": true` once the bridge side is live.

### NATS transport (opt-in, `NEXUS_BUS_TRANSPORT=nats`) {#nats-transport}

The A2A **medium** is pluggable. Slack was the right bootstrap — a free durable log + a
human UI in one — but it does not scale to a company-wide fleet: Socket Mode caps concurrent
connections per app (~10), which forces one Slack app *per participant* (hundreds of
security-approved apps in a Vanta workspace), plus `chat.postMessage` rate limits and PHI in
Slack. `NEXUS_BUS_TRANSPORT=nats` routes **agent-to-agent** traffic over a NATS + JetStream
broker instead; the **human** notify/reply leg (`#nexus`, `/notify`, threads, `/relay`) stays
on Slack. This realizes [agent-bus-roadmap Phase G](agent-bus-roadmap.md#phase-g--broker-substrate-optional-last)
(full change: `openspec/changes/nats-a2a-bus-transport`).

**What moves, what doesn't.** Only the transport touch points change — publish (`/send`) and
inbound A2A delivery. Routing (`parseAddress`), the delivery layer (`send-keys` / SDK inbox),
the `@waiting` idle-gate, and presence *semantics* are unchanged. **`agent-send.sh` is
unchanged** — it still POSTs `:8788/send`; the transport is chosen bridge-side.

**Mapping** (the subject/key codec is unit-tested in `orchestrator.js`; the transport is in
`slack-bridge/transports/nats-transport.js`):

| Slack bus | NATS transport |
| --- | --- |
| FQDN token scanned off one channel by every host | **Subject** `nexus.a2a.<host>.<workspace>.<name>`; a bridge subscribes only its own `nexus.a2a.<self-host>.>` — the broker routes to the owner, no fan-out |
| `#nexus-agents` history = the durable log | **JetStream stream** `NEXUS_A2A` (bounded retention) — offline recipients drain from their durable consumer on reconnect; also the audit/replay record |
| in-memory `busQueue` idle-gate | Same in-memory idle-gate today (ack-on-receive). Ack-on-idle — holding the JetStream message un-acked until delivery so a hold survives a bridge restart — is a tracked follow-up |
| presence gossip on the channel | **JetStream KV** `nexus_presence`, FQDN-keyed with a TTL; upserted on a heartbeat, TTL-aged on departure |
| one Slack app per participant | one broker; a participant joins with a **credential** (NKEY/JWT), not a messaging app |

**Config** (env — see `.env.example`): `NEXUS_BUS_TRANSPORT` (`slack` default | `nats`),
`NATS_URL`, `NATS_A2A_STREAM`, `NATS_A2A_SUBJECT_PREFIX`, `NATS_PRESENCE_KV`, and one of
`NATS_CREDS` / `NATS_TOKEN` / `NATS_USER`+`NATS_PASS`. The bus is still gated by
`SLACK_BUS_ENABLED=1` (the master switch). `curl :8788/health` reports `"transport":"nats"`
and `"nats":true` once connected.

**Topology (`NEXUS_A2A_MODE`)** — `single-host` (one box, no peers) FORCES the `nats` transport
and turns the Slack A2A paths **off entirely**: the bridge does not listen to `#nexus-agents`
for A2A and does not gossip presence on Slack, so NATS + its KV are the *sole* A2A medium — no
latent dual delivery path, no redundant presence. `/agents` then reads reachability from the KV
(`"presence":"nats-kv"`). Default `multi-host` keeps the Slack A2A + gossip available (cross-firewall
fallback / migration). The human notify/reply leg is on Slack regardless of the mode.

**Broker** (single-node pilot; add TLS + subject-scoped creds + a 3-node cluster for the
company fleet):

```bash
docker compose -f docker-compose.work.yml --profile nats up -d   # nats://…:4222 (+ :8222 monitor)
```

**Migration is dual-run + flag-reversible.** Point a host at NATS (`NEXUS_BUS_TRANSPORT=nats`);
the human legs keep working on Slack. Roll back any time with `NEXUS_BUS_TRANSPORT=slack` +
restart — the bridge is back on the `#nexus-agents` bus with the in-memory idle-gate, no other
change. Cut the fleet over host-by-host.

> **Verification status:** the transport (publish, durable consumer, host isolation, offline
> backlog, envelope round-trip, KV presence) is integration-tested against a live
> `nats-server` (`slack-bridge/transports/nats-transport.itest.mjs`, run manually — needs a
> broker on `:4222`, not part of `npm test`). The subject/KV codec is unit-tested in
> `npm test`. Full bridge-in-nats-mode end-to-end (bridge + broker + Slack tokens together) is
> the rollout step (change tasks 8.x).

### Typed envelopes + request/reply (Phase B) {#typed-envelopes}

A2A messages carry a versioned **typed envelope** `{ v, id, ts, from, to, kind, corr?, reply_to?, body, meta? }`. `kind` is one of:

| kind | meaning | delivered as |
| --- | --- | --- |
| `msg` (default) | fire-and-forget nudge (today's behavior) | `↩ from <sender>: <body>` |
| `request` | expects a reply; carries an `id` | `↩ request from <sender> [id X]: <body>` + a one-line reply hint |
| `reply` | answers a request; `corr` = the request's `id` | `↩ reply from <sender> [re X]: <body>` |
| `event` | notification, no reply | `↩ event from <sender>: <body>` |

**Backward compatible.** A message with no `kind` (the legacy NATS record or a bare `to: ↩ from x: y` line) is a `msg`, and a `msg`'s delivered text is byte-for-byte unchanged — so a mixed old/new fleet interoperates. On Slack, `msg` stays the human-readable addressed line; `request`/`reply`/`event` ride a `to: ::nexus-env:: {json}` sentinel line (addressed so the owner routes it, sentinel so it never parses as a plain delivery).

**agent-send.sh verbs** (bare send unchanged):

```bash
agent-send.sh --request <to> "what's the deploy status?"      # mints an id; reply routes back to you
agent-send.sh --reply <id> <to> "green, shipped 5m ago"        # answers request <id>
agent-send.sh --event <to> "cache warmed"                      # notification, no reply
```

A `request` is delivered with the exact reply command, so the recipient agent knows how to answer. Request/reply is **async** (the agent replies on its next turn), not a blocking RPC.

**Await a reply programmatically** — `POST /request` publishes a request and holds the response until the reply arrives or the deadline elapses (so a skill/loop/Conductor node can ask an agent and get an answer):

```bash
curl -s localhost:8788/request -H 'content-type: application/json' \
  -d '{"to":"svc-chatbot","body":"is CI green on main?","deadline_ms":30000}'
# → {"ok":true,"status":"ok","from":"…/svc-chatbot","body":"yes, green"}   (or {"status":"timeout"})
```

Config: `SLACK_BUS_REQUEST_TTL_MS` (default `120000`) — the default request deadline. This is roadmap [Phase B](agent-bus-roadmap.md#phase-b--typed-envelopes--requestreply-rpc); full change: `openspec/changes/bus-typed-envelopes`.

### Enable it (one-time)

1. Create a **dedicated** `#nexus-agents` channel and invite the bot. ⚠️ It **must be a
   different channel from `#nexus`** (`SLACK_NEXUS_CHANNEL`): the bridge treats every
   message whose channel is `SLACK_AGENTS_CHANNEL` as bus traffic and `return`s before
   the human-message path (`handleMessage`), so pointing the bus at the control channel
   silently swallows cards, routing, and thread replies.
2. In the Slack app's **Event Subscriptions → bot events**, add the `message.<type>`
   event for that channel's type (`message.channels` public / `message.groups`
   private) **and** the matching `channels:history` / `groups:history` scope — the
   event and scope are configured **separately** (see the warning below).
3. Set `SLACK_BUS_ENABLED=1` + `SLACK_AGENTS_CHANNEL=<Cxxxx>` in Doppler `nexus/prd`,
   and `SLACK_BUS_ENABLED=1` in `~/.tmux/env.sh` so agents attempt the bus. Restart the bridge.
4. Verify: `curl :8788/health` shows `"bus":true`; a `--via-slack` send to a local
   agent round-trips through `#nexus-agents` and is delivered with the `↩ from` tag.

#### Per-host rollout (e.g. a second box / the Mac work machine)

Step 2's Slack-app config (events/scopes) is **app-level — done once for the whole
fleet**. The bridge-side bus env lives wherever that host's bridge reads its env:
**Doppler `nexus/prd`** (shared across Doppler hosts) on the Linux box, or repo-root
**`.env`** on a vanilla Mac (commit `73637b0` — the macOS bridge runs plain `node`, no
Doppler wrap). Bringing the bus up on another box is just local plumbing:

1. `git pull` (the bridge code + `agent-send.sh` are shared/symlinked; nothing host-specific to port).
2. Set the bridge-side bus env for that host. **Doppler box:** ensure the Doppler CLI is
   authed (the unit launches via `doppler run -p nexus -c prd`, systemd on Linux) — the
   shared `nexus/prd` flags then apply. **Vanilla Mac:** put `SLACK_AGENTS_CHANNEL=<Cxxxx>`
   (its own dedicated channel) + `SLACK_BUS_ENABLED=1` in repo-root `.env`; `kickstart -k`
   the bridge to reload.
3. Run the installer (`bash tmux/linux/install.sh` or `bash tmux/mac/install.sh`) — it
   seeds `SLACK_BUS_ENABLED` + `SLACK_A2A_SAMEHOST` into `~/.tmux/env.sh` (default
   off/local) and reinstalls the service.
4. Opt in locally: set `SLACK_BUS_ENABLED=1` (and `SLACK_A2A_SAMEHOST=channel` for the
   idle-gated buffer) in that box's `~/.tmux/env.sh`. Restart the bridge; **relaunch
   agents** so they pick up the exported env.
5. `curl :8788/health` → `"bus":true`. Same-host A2A now buffers through `#nexus-agents`
   and delivers on idle — independently on each box (no cross-host coordination needed
   unless both bridges run against the same channel at once + presence is enabled).

> **Windows parity:** the dual-mode logic lives in `tmux/mac/tmux-scripts/agent-send.sh`
> (symlinked as `~/.tmux/agent-send.sh` on mac + Linux). The Windows copy
> (`tmux/windows/tmux-scripts/agent-send.sh`) is not yet updated and remains local-only.

### Phase 2 — presence registry (opt-in, `SLACK_PRESENCE_ENABLED`)

Phase 1 delivery is host-local: a bridge delivers to a name only if it's in that
host's own registry, so two hosts with the same agent name would both deliver.
Phase 2 closes that gap **without any shared store** — it rides the same Socket
Mode fan-out as the bus:

- **Announce:** each bridge posts a full-state snapshot of its *live* local agents
  (registry ∩ live tmux panes) to `#nexus-agents` as a `::nexus-presence:: {v,host,agents,ts}`
  control line — on startup, on a heartbeat, and on every registry change (so a
  spawn/reap propagates in ~2s). The leading `::` keeps the addressed-delivery
  parser from ever mistaking it for a `name: text` message.
- **Consume:** peers fold each snapshot into an in-memory `host → {agents, ts}` map;
  a host that stops heartbeating ages out after `SLACK_PRESENCE_TTL_MS`.
- **Single owner:** the owner of a *bare* name is the **lexically-smallest host**
  that claims it — every bridge computes the same answer, so exactly one delivers.
  `handleBusMessage` defers when presence names another owner, even if its own
  registry also matches (a stale entry or a genuine collision). **A namespaced
  `host/name` target bypasses this election** — an explicit host prefix *is* the
  owner designation. This is what makes two people's same-named agents both
  addressable (see [cross-person](#cross-person-two-people-one-bus)); without it,
  election would funnel every hit for a shared name to whoever sorts first.
- **Collisions** (a name on >1 host) are logged and surfaced in `GET /agents`.
  Between two people this is the *normal* state (you both have a `general`), not an
  error — namespaced addressing is how you disambiguate, and `GET /agents` is the
  directory that tells you which hosts claim a name.
- **Reachability:** `curl :8788/agents` → `{ self, hosts, agents:[{name,workspace,pane,host,owner,collided}], collisions }`
  — one row **per instance** (with FQDN presence on, two same-named agents on one host are two rows).
  `/health` also reports `presence` + `host`.

Enable it as the **cross-host** rollout step (mirrors the bus's own enablement) by injecting
`SLACK_PRESENCE_ENABLED=1` (+ `SLACK_PRESENCE_HOST=<label>`) as **process env** on each host's
bridge and restarting. ⚠️ On a Doppler/Linux box these do **not** flow through Doppler: the bridge
launches via `secret-run.sh` with an explicit secret allowlist (tokens/channel/`SLACK_BUS_ENABLED`
only), so a value set only in Doppler never reaches the process. Inject via a **systemd drop-in**
(`~/.config/systemd/user/slack-bridge.service.d/*.conf` → `Environment=…`); a vanilla Mac reads
repo-root `.env` directly. Full worked example (two people): [`cross-person-setup.md`](./cross-person-setup.md).
It's only useful once ≥2 bridges run it; on a single host it's inert noise, so it ships **off**.
Tracked in `openspec/changes/slack-agent-bus`.

### FQDN presence — instance identity (opt-in, `SLACK_PRESENCE_FQDN`)

Phase 2's presence gossips a per-host **set of bare names**, so two agents that share a
name on one host (e.g. two `general`s in different buckets) collapse into one entry and
become unaddressable — a message to `host/name` hits an ambiguous local match and is
silently dropped. FQDN presence (**v2**) fixes this by publishing per-instance records:

- **Wire:** `::nexus-presence:: {v:2, host, agents:[names], instances:[{name, workspace, pane}], ts}`.
  `agents` stays bare **names** so a pre-FQDN (`v:1`) bridge — which does `agents.map(String)` —
  reads them correctly (records there would become `"[object Object]"`); the rich identity rides
  in a separate `instances` field it ignores. A v1 line (bare names, no `instances`) folds in as
  `workspace:''`. `parsePresence` prefers `instances`, falls back to `agents`. Back-compat runs
  **both** ways — mixed fleets interoperate.
- **Identity + collisions:** owner election and collision detection key on the full
  `host/workspace/name`. Two same-named agents in *different* workspaces are distinct, not
  a collision; the same `workspace/name` twice (across hosts, or twice on one host) IS one.
- **Addressing:** reach a specific instance cross-host with `host/workspace/name`, or
  `host/pane` for the tie-broken intra-bucket duplicate. An ambiguous bare name is now a
  **logged, actionable** drop (it names the qualified addresses to retry), not a silent one.
- **Registration:** `substrate.sh register` already records each agent's `WORKSPACE`
  (falling back to `$NEXUS_WORKSPACE` / the herdr bucket), which is what these records carry.

Off by default; enable per host like presence (process env on the bridge). Tracked in
`openspec/changes/presence-instance-identity`.

### Cross-person (two people, one bus)

The bus was built for **one person's fleet across several machines**. It also works
for **two people on two machines** sharing one `#nexus-agents` — so each can address
and relay to the other's agents while debugging together — with one caveat and two
must-set knobs.

**Topology.** A **dedicated side workspace** (not the the org workspace) is strongly
recommended: a cross-person bus means anyone in the channel can `send-keys` into your
agents (see below), and a side workspace keeps that blast radius off an audited corp
space. Each person creates their **own** Slack app there (own `xoxb-`/`xapp-`); both
bots join the **same** `#nexus-agents`. Different tokens are fine — delivery is
registry-gated, so two bots reading the channel just both observe and only the owner
acts.

**Must-set on each box** (repo-root `.env` on a vanilla Mac; Doppler `nexus/prd` on a
Doppler box):

| Var | alex's box | buddy's box |
| --- | --- | --- |
| `SLACK_AGENTS_CHANNEL` | *(same channel id)* | *(same channel id)* |
| `SLACK_BUS_ENABLED` | `1` | `1` |
| `SLACK_PRESENCE_ENABLED` | `1` | `1` |
| `SLACK_PRESENCE_HOST` | `alex` | `buddy` |

`SLACK_PRESENCE_HOST` is the load-bearing one: it's both the directory label and the
`host/` you address. Set clean, distinct labels (`alex`/`buddy`), not raw hostnames —
they're what you and your agents type.

**Using it:**

- **Address a specific person's agent:** `agent-send.sh buddy/general <msg>`. Bare
  `general` still means *your* local `general`.
- **See who's running what:** `curl :8788/agents` (or the `who`/`status` roll-up) —
  presence reconstructs the combined `alex` + `buddy` fleet on each box, no shared
  store. Shared names show as `collided` — expected, not an error.
- **Share output:** `agent-send.sh --relay <text>` posts it to the channel for the
  other person to read, replacing the paste-into-DM habit.

**The one caveat — a trust boundary.** A delivered bus message is a real `send-keys`
keystroke injection into a live Claude. Cross-person, that means your buddy (or anyone
in the channel) can type into your agents. Between two trusted people this is usually
fine — but *know* it, keep it on a side workspace, and don't point a cross-person bus
at a channel a wider team can post to. (A visible-suggestion receive mode, instead of
auto-inject, is a possible future gate; today delivery is auto-inject only.)

> **Windows:** a symmetric setup needs the other person's box to run a bridge + the
> tmux registry so their names resolve and `send-keys` can land. The `tmux/windows/`
> tree runs under **MSYS2** (bash + tmux) but is **not yet bus-aware** — its
> `agent-send.sh` predates the bus and `hook-notification.sh` has no `/notify`. Bus
> parity for Windows is a separate port (and requires MSYS2-with-`tmux`, not plain
> Git-for-Windows bash, which has no tmux). Until then, a Windows peer can *drive your
> agents* by typing `alex/name: …` as a human in the channel and *read your relays*,
> but their own agents aren't reachable.

## Slack app setup (one-time)

Create an app at <https://api.slack.com/apps> → **From an app manifest**, paste
the manifest below, then install to the workspace. (Creating/installing an app in
the the org workspace may need workspace-admin approval.)

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
curl -s localhost:8788/health        # {"ok":true,"connected":true,"threads":N,"bus":bool,"presence":bool,"host":"…"}

# Fleet reachability (when presence is enabled)
curl -s localhost:8788/agents        # {"self","hosts",agents:[{name,host,owner,collided}],collisions:[…]}

# Inbound — in #nexus, post:  example-service: say hi
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
  systemd analog of the plist) plus the `/notify` round-trip in the now-shared
  `tmux/mac/tmux-scripts/hook-notification.sh` (the Linux override was folded in —
  it picks `notify-send` (console) + a terminal bell (SSH) vs macOS `osascript`
  behind an `$OSTYPE` guard, so there is one copy). The auto-approve classifier
  gate is included but stays inert unless the `~/.tmux/.classify-venv` is present.
