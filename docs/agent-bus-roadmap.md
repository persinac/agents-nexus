# Agent bus — evolution roadmap

Where the inter-agent bus goes after `slack-agent-bus` (shipped `763fbdf`). The bus
today: `agent-send.sh` → bridge `POST /send` → `#nexus-agents` → `handleBusMessage`,
with per-recipient **idle-gated delivery** (hold until `@waiting=2`, flush from an
in-memory `busQueue`) and an opt-in **presence registry** (`host→agents`, single-owner
delivery, `GET /agents`). It works, but delivery is best-effort text injection: the
queue dies on restart, there are no acks/replies, and messages are untyped.

This plan sequences the orthogonal capability axes plus the durability/reconciliation
("r12n") work into phases that compose.

## Principles (carried from the existing design)

1. **Opt-in, default off.** Every phase ships behind a flag; the working bus never regresses.
2. **Reuse what we have.** `agent-ledger.py` (flock JSONL) for durability, the
   agent-memory MCP (mnemon, SSE `:8330`) for history, the arbiter Command Center for
   observability, the presence map for routing. Pure/testable pieces go in
   `orchestrator.js` (unit-tested like the presence helpers); stateful glue + Slack I/O
   stay in `index.js`.
3. **Slack stays the human-observable transport.** A broker (NATS/JetStream) is a
   *swappable substrate behind a typed envelope*, never a prerequisite — and it must
   not break the work-Mac-behind-corp-firewall case the Slack design solved.
4. **The typed envelope is the spine.** Once messages are structured + correlated,
   everything downstream (RPC, dedup, dead-letter, broker swap, memory ingest) composes.

## Phase A — Durability & reconciliation (r12n)

**Goal:** held messages survive a bridge restart; delivery is acked, retried, dead-lettered.

**Approach**
- **Durable outbox**: a flock-guarded JSONL append-log (`~/.tmux/agent-outbox.jsonl`,
  `$AGENT_OUTBOX`), a near-copy of `scripts/agent-ledger.py`. Events: `enqueued`,
  `delivered`, `acked`, `failed`, `dead`. State projection = pending (enqueued without a
  terminal event); `compact` rewrites to live tail like the ledger.
- **Bridge wiring**: `enqueueBus` appends `enqueued`; `flushBusQueue` appends
  `delivered`/`failed`. On startup, replay pending entries back into `busQueue` (the
  reconcile step) so a restart mid-buffer loses nothing.
- **Receipts**: start with *delivered* (we know `send-keys` succeeded). Upgrade to a
  recipient *ack* later — a Stop-hook ping or an `/ack` endpoint hit when the agent
  actually consumes the message. The outbox already has the `acked` slot for it.
- **Retries + dead-letter**: a failed delivery retries with backoff; after N it's marked
  `dead` (still in the channel + outbox for inspection), surfaced via `/bus` (Phase D).

**Touch points:** `slack-bridge/index.js` (`busQueue`, `enqueueBus`, `flushBusQueue`,
startup replay); new `scripts/agent-outbox.py` (mirrors `agent-ledger.py`).
**Decisions:** persist inline in the bridge vs a sidecar script; delivered-receipt vs recipient-ack.
**Effort:** M. **Verify:** kill the bridge with a message held → restart → it still delivers.

## Phase B — Typed envelopes + request/reply (RPC)

**Goal:** structured, correlated messages; agent A can *ask* B and await a reply. The single biggest capability unlock (backs the old #10 handoff/chaining and #15 agent-awareness ideas).

**Approach**
- **Envelope**: `{v, id, corr, from, to, kind, intent, body, ts}`. Backward-compatible —
  a plain message is `{kind:"text", body}`. Posted as a compact JSON line; pure
  parse/format/validate in `orchestrator.js` (unit-tested), so `handleBusMessage` accepts
  envelope-or-text.
- **Correlation**: every message gets an `id`; a reply carries `corr=<request id>`. The
  bridge keeps a `pendingRequests` map and routes a matching reply back to the asker.
- **CLI**: `agent-send.sh --ask <name> <msg>` posts a request and returns a `corr` (or
  blocks on a fifo/file with `--timeout`); `agent-send.sh --reply <corr> <msg>` answers.
  Rendered to the recipient as a readable line that says how to reply
  (`↩ ask from A [corr abc]: … — reply: agent-send.sh --reply abc …`).

**Touch points:** `tmux/mac/tmux-scripts/agent-send.sh` (`--ask`/`--reply`, envelope
build), `slack-bridge/index.js` (`/send` envelope, parse + `corr` routing,
`pendingRequests`), `orchestrator.js` (envelope schema + helpers).
**Decisions:** JSON-in-text vs Slack metadata blocks; sync-block vs async corr; how a CLI
caller awaits (fifo/file poll).
**Effort:** L. **Verify:** A `--ask` B → B `--reply` → A receives the correlated answer.

## Phase C — Capability presence + routing

**Goal:** route by capability/availability ("who can review Go *right now*"), not just by name.

**Approach**
- Extend the presence snapshot to `{host, agents:[{name, caps[], task, busy}]}` (caps
  declared via a registry `CAPS=` line or a small caps file; `busy` from `@waiting`).
  > The per-instance record structure (v2 presence, `agents:[{name, workspace, pane}]`) is
  > already delivered by `presence-instance-identity` — Phase C just adds the `caps`/`task`/`busy`
  > fields to the same record and a `resolveByCapability` reducer over the instance map.
- `orchestrator.js` gains `resolveByCapability(cap)` (pure) → idle candidates;
  `agent-send.sh --any <cap> <msg>` lets the bridge pick a reachable, idle, least-loaded owner.

**Touch points:** `orchestrator.js` (schema + capability helpers), `index.js`
(`publishPresence` includes caps, capability resolution), registry/caps source.
**Decisions:** caps declared vs inferred-from-repo; selection policy (idle-first / round-robin / least-loaded).
**Effort:** M. **Depends on:** presence (done), ideally B for clean routing.
**Verify:** `--any review …` routes to an idle agent advertising `review`.

## Phase D — Bus observability in the Command Center

**Goal:** see the mesh — who's talking to whom, queue depths, dead-letters, pending RPCs.

**Approach**
- Bridge exposes `GET /bus` (per-pane queue depths, outbox pending/dead counts, recent
  traffic, pending requests) beside `/agents` + `/health`.
- Arbiter adds `/api/system/bus` (mirrors `/api/system/agents` — fetch the bridge
  endpoint or read the outbox JSONL); the dashboard gets a **Bus** tab (mirrors the
  Memory/Spark views), with a sender→recipient force-graph reusing `MemoryGraphView`.

**Touch points:** `index.js` (`/bus`), `arbiter/index.js` (`/api/system/bus`), dashboard webview.
**Decisions:** poll vs SSE; read bridge endpoint vs outbox file.
**Effort:** M. **Depends on:** A (the outbox is the data source).
**Verify:** send traffic → it + queue depth appear live in the dashboard.

## Phase E — Bus → memory ingestion

**Goal:** inter-agent exchanges become queryable fleet history.

**Approach:** on delivery/ack the bridge best-effort `create_note`s to the agent-memory
MCP (mnemon SSE `:8330`, same MCP-over-SSE client shape as `spark-resolve.py`), linking
both agent names + repos. Throttled/summarized (e.g. only `--ask`/`--reply` + non-trivial
messages) so it doesn't flood the store.

**Touch points:** `index.js` (post-delivery hook → mnemon client helper).
**Decisions:** ingest all vs RPC-only vs flagged; sync vs batched; dedup.
**Effort:** S–M. **Depends on:** A/B. **Verify:** an exchange surfaces in `query_notes`/`search_similar`.

## Phase F — Flow control & safety

**Goal:** no storms, fair sharing, priority lanes.

**Approach**
- Per-sender rate limit (reuse `orchestrator.rateState`, already used for spawn).
- Loop detection: track recent `(from,to,bodyHash)` / `corr`-chain depth; throttle + warn
  a runaway A→B→A (surface in `#nexus` + the dashboard).
- Priority: control messages (acks, RPC replies) get a fast lane / preempt the idle-gate; chatter waits.

**Touch points:** `index.js` (rate state, loop guard, priority in `flushBusQueue`),
`orchestrator.js` (`rateState` exists; add a pure loop-hash helper).
**Decisions:** thresholds; control-vs-chatter classification.
**Effort:** M. **Depends on:** A (queue), B (corr depth). **Verify:** a ping-pong gets throttled; an ack preempts queued chatter.

## Phase G — Broker substrate (optional, last)

> **Status: landing early via `openspec/changes/nats-a2a-bus-transport`.** The company-scale
> pressure (Slack Socket-Mode caps → one bot per participant) pulled G forward ahead of A/B.
> Shipped so far: the `publish/subscribe` transport seam in `index.js` + `slack-bridge/transports/`,
> a `NatsTransport` (JetStream stream + durable per-host consumer + KV presence), the FQDN↔subject
> codec (`orchestrator.js`, unit-tested), and `NEXUS_BUS_TRANSPORT={slack|nats}` (default slack).
> Caveats vs the full vision below: the wire envelope is the minimal `{to,from,msg,ts}`, **not**
> the Phase-B typed envelope (do B next, behind the same seam), and idle-gating is still
> ack-on-receive (ack-on-idle is the follow-up that makes a hold restart-durable). Slack stays the
> human mirror + cross-firewall fallback, exactly as intended.

**Goal:** real durable pub/sub where reachable (intra-host, or a central reachable node) — the "throw NATS at it" rung, deliberately enabled *by* the earlier phases rather than instead of them.

**Approach:** behind the Phase-B envelope, abstract transport to a `publish/subscribe`
interface with **Slack** and **NATS JetStream** (or Redis Streams) implementations,
selectable per-host. Slack stays as the human-observable mirror + the cross-firewall
fallback for boxes a broker can't reach (the work Mac). The envelope (B) + outbox (A) make
this a transport swap, not a rewrite.

**Touch points:** a transport interface in `index.js`; per-host config.
**Decisions:** central vs per-host broker; reachability (Tailscale?); keep the Slack mirror.
**Effort:** L + infra. **Depends on:** B.

## Sequencing

```
A (durable queue + receipts) ─┐
                              ├─> C (capability routing)
B (typed envelopes + RPC) ────┼─> D (observability)
                              ├─> E (memory ingest)
                              ├─> F (flow control)
                              └─> G (broker substrate, optional)
```

**A → B are the spine.** Do them first (durability you can feel + the RPC capability
jump); C/D/E/F hang off them and parallelize; G is the optional infra bet, kept last so
the message *shape* is settled before the transport moves. Each phase is its own OpenSpec
change behind its own flag.

## Open questions

- **Receipt semantics:** delivered-receipt (cheap, know-it-was-typed) vs recipient-ack (true consumption, needs a hook/endpoint).
- **Envelope wire format:** JSON-in-text (simple, human-readable in Slack) vs Slack metadata blocks (clean, but Slack-coupled — worse for a future broker).
- **Capability source:** declared per agent vs inferred from the repo/CLAUDE.md.
- **Broker reachability:** central NATS needs the work Mac to reach it (Tailscale) — or that host just stays Slack-only.
