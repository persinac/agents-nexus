# Ideas & Roadmap

## High impact — low effort

1. ~~**Windows toast notifications**~~ — removed; red status bar indicator (item 3) is sufficient
2. **Batch approve/reject** — done: `qa` function (e.g., `qa 1` to approve all waiting agents)
3. **Wait duration in status bar** — done: three-state color system (green=working, grey=idle, red+timer=needs input) via `PreToolUse`, `Stop`, and `Notification` hooks
4. **CLAUDE.md files per repo** — done: `CLAUDE.md.template` + `claude-init` command to scaffold

## Medium effort — multiplier effects

| # | Priority | Idea | Status |
|---|----------|------|--------|
| 5 | 10 | **Session templates** — predefined layouts for common workflows (e.g., `work-stack frontend backend tests`). One command to set up a whole workstream | backlog |
| 6 | 1 | **Git worktree integration** — auto-create worktrees when spawning agents in the same repo, so two agents can work on different branches without conflicts | done |
| 7 | 5 | **Agent summary on peek** — enhance `v()` to parse the last Claude output and show a one-line status instead of raw terminal output | done |
| 8 | 5 | **Stuck agent detection** — if an agent's been "running" (green) for >10min with no tool use logged, flag it yellow | done |
| 9 | — | **Agent-to-agent messaging** — `/msg <slot> <message>` slash command + `agent-send.sh` script | done |

## Bigger bets

| # | Priority | Idea | Status |
|---|----------|------|--------|
| 10 | 3 | **Agent handoff / chaining** — when agent 2 finishes, auto-send its summary to agent 3. Pipeline: write → review → test | |
| 11 | 2 | **Searchable history & agent memory** — central knowledge store across all devices. Design: [`searchable-history-design.md`](docs/searchable-history-design.md) | done |
| 11a | 2 | ↳ Central Postgres schema (`memory_events` + `memory_nodes` + `memory_links` + `memory_entities`) + MCP server with basic CRUD tools | done |
| 11b | 2 | ↳ Local wrapper + CF tunnel + tmux hook integration (auto-capture events from `Stop`/`PreToolUse`/`Notification` hooks) | done |
| 11c | 3 | ↳ Entity extraction + backlinks ("everything about auth-module") + tag taxonomy | done |
| 11d | 4 | ↳ pgvector embeddings on notes + semantic search (MAGMA-style anchor identification) | done |
| 11e | 5 | ↳ Temporal edges | done |
| 11g | 6 | ↳ Auto-inject knowledge into agent prompts (`/recall` + MAGMA retrieval + token-budgeted context) | done |
| 12 | 4 | **Cost/token dashboard** — track token usage per agent per session alongside APM | |
| 13 | 7 | **Auto-routing** — when an agent goes red, automatically `v` it in a small persistent pane at the bottom | |
| 14 | 2 | **Pixel agents dashboard** — fork [pixel-agents](https://github.com/pablodelucca/pixel-agents) React webview into a standalone Electron/web app. Replace VS Code postMessage with WebSocket fed by tmux hook data. Animated pixel art characters show agent state (typing/reading/waiting/idle) in a fun office visualization. Source: `webview-ui/` in the pixel-agents repo. | done |
| 15 | 2 | **Agent awareness** — agents should know about other running agents (window slot, repo, branch, status) and be able to use `/msg` to request info from them. Could be a CLAUDE.md snippet or a hook that injects context on session start. | done |
| 16 | 6 | **Causal inference** — slow-path LLM batch over note pairs to infer why changes happened. Anthropic Message Batches API. Run after session end. Defer until ~50+ notes in corpus. | |
| 17 | 6 | **Knowledge graph web UI** — interactive visualization of memory nodes + links. Orthogonal views (temporal/causal/semantic/entity). Extend pixel-dashboard. Build after causal inference produces links worth visualizing. | **v1 done** — bipartite force-graph (notes + entities, `mentions` edges) in the dashboard: `Graph` toolbar button → `MemoryGraphView` + arbiter `/api/system/memory/graph` + `memory-graph.py`. Temporal/causal/semantic lenses pending inference (#11e/#16) |
| 18 | 5 | **Versioned agentic scheduled jobs** — add a `jobs/` directory pattern: each job is a folder with a plist + script (e.g. `obs-digest`, `obs-tidy`). Installer links scripts to `~/.local/bin` and plists to `~/Library/LaunchAgents`. Makes it easy to add new Claude-powered cron-style jobs. | |
| 21 | 3 | **Per-installation `last_indexed` history in the Timers panel** — `installations.json` already stores `indexed_at` and `last_remote_ts` per repo. Surface this in the Command Center Timers panel (or a new "Installations" tab) so you can see at a glance which repos have stale embeddings, which are due for a re-index, and how the sync run distributes across repos over time. Arbiter would expose `/api/system/installations` reading the JSON; UI is a sortable table. | done |
| 22 | 3 | **Memory search box in the Command Center** — mnemon already speaks SSE on `:8330/sse`. Add a search input in the Command Center that fans out to mnemon's `search_similar` / `query_notes` tools and renders results inline. Bridges the agent-memory DB to a human surface — you can browse what the agents have learned without dropping into Claude or psql. Probably a new "Memory" tab in `CommandCenter.tsx` plus a small arbiter proxy for the MCP call. | done |
| 25 | 1 | **Interactive Block Kit approve/deny cards** — render permission requests in `#nexus` as Block Kit cards (agent + repo/cwd, command in a code block, risk tag from the classifier) with `[Approve] [Approve+don't ask] [Deny]` buttons. Tap → Socket Mode `block_actions` → bridge sends the digit. One tap beats typing `1`. See "Slack Bridge UX & Agent Bus" below. | **done** (`ad067e5`) — buttons + approve-by-reaction + terminal mirror + same-prompt guard |
| 26 | 1 | **Live fleet status board** — one bot-maintained message (`chat.update`) listing every agent + state (working / ⏳ waiting / 🟢 auto-approving / idle / done), driven by existing hooks; pinned in `#nexus`. At-a-glance mission control. | backlog |
| 27 | 3 | **Per-agent threads + lifecycle feed** — group each agent's requests under a persistent root message; post agent start / turn-finished (Stop hook) / idle as a feed so `#nexus` is fleet activity, not just prompts. | **partial** — per-agent threads (anchor per agent) + delete-on-resolve done (`ad067e5`); lifecycle feed (start/turn-finished/idle posts) still pending |
| 28 | 2 | **Slack as the inter-agent message bus** — route `agent-send.sh` through Slack (dual-mode: local→send-keys, remote→Slack) so the Mac fleet and the Linux box can talk, with full observability. Delivery half already exists (bridge inbound routing). See below. | **shipped** (`763fbdf`) — dual-mode bus + per-recipient idle-gated delivery (buffer until `@waiting=2`); presence registry behind `SLACK_PRESENCE_ENABLED`. Same-host buffering via `SLACK_A2A_SAMEHOST=channel`. Cross-host + Windows deferred |
| 29 | 3 | **AWS Secrets Manager as a Doppler alternative** — let a host source bridge/agent secrets from AWS SM instead of Doppler, for boxes where Doppler isn't available (e.g. a work Mac, where the bridge currently falls back to a local plaintext `.env`). Not as ergonomic as `doppler run --`, but keeps secrets centralized + rotatable rather than on-disk. A small launch wrapper fetches the secret JSON at bridge/agent start and exports it (mirroring the `doppler run --` wrapping in the systemd unit / launchd plist), selectable per-host. | backlog |
| 30 | 2 | **Agent bus v2** — evolve the shipped bus (#28) from best-effort text injection into a durable, typed, observable mesh. Full plan: [`docs/agent-bus-roadmap.md`](docs/agent-bus-roadmap.md). A→B are the spine; the rest hang off them. | backlog |
| 30a | 2 | ↳ **Durability & reconciliation** — flock JSONL outbox (mirrors `agent-ledger.py`) so held messages survive a bridge restart; delivered-receipts, retries/backoff, dead-letter | backlog |
| 30b | 2 | ↳ **Typed envelopes + request/reply (RPC)** — `{id,corr,from,to,kind,intent,body}`; `agent-send.sh --ask/--reply` with correlation so A can ask B and await a structured answer (the big capability unlock; backs #10/#15) | backlog |
| 30c | 4 | ↳ **Capability presence + routing** — extend presence with `caps[]`/`busy`; `--any <cap>` routes to a reachable idle agent that can do it | backlog |
| 30d | 4 | ↳ **Bus observability** — bridge `/bus` + arbiter `/api/system/bus` + a Command Center "Bus" tab (reuse the memory force-graph) showing traffic, queue depth, dead-letters | backlog |
| 30e | 5 | ↳ **Bus → memory ingest** — best-effort `create_note` to mnemon on delivery so inter-agent exchanges become queryable fleet history | backlog |
| 30f | 5 | ↳ **Flow control** — per-sender rate limit (reuse `rateState`), loop detection (A→B→A / corr-depth), priority lane for acks/replies | backlog |
| 30g | 6 | ↳ **Broker substrate (optional)** — swap transport to NATS JetStream / Redis Streams *behind the envelope*, Slack kept as the human-observable mirror + cross-firewall fallback. The "throw infra at it" rung, deliberately last | backlog |
| 30h | 2 | ↳ **Large-message chunking (no truncation)** — no-loss follow-up to the #28 silent-truncation bug (integration-tests hit a ~1500-char cut on an inbound reply). Option A (shipped) caps the bus body at `SLACK_BUS_MAX_CHARS` with a visible `…[truncated N chars]` marker so loss is never silent; Option B = split over-cap messages into `[k/N]` parts, buffer per-sender on the receiving bridge, and reassemble before send-keys so nothing is ever dropped. | backlog |
| 31 | 3 | **Gemini fallback when Claude is down** — when both Anthropic tiers (corp the corporate gateway + direct `api.anthropic.com`) are unreachable or overloaded, translate Claude Code's requests to Gemini via a new LiteLLM `/v1/messages` service so agents stay alive through an Anthropic outage. `nexus-proxy` remains the front door (Langfuse + session-tagging intact); opt-in behind `GEMINI_FALLBACK_ENABLED`; fires on hard outages only (5xx/529 + connection errors, **not** 429). See below. | backlog |

## Slack Bridge — bot & channel UX + agent bus (ideas 25-28)

The Slack bridge is live: `#nexus` (public) surfaces mutating permission prompts + questions; the auto-approve classifier keeps read-only noise out; replies route back via thread / `name: text` and answer permission menus (`yes/approve→1`, etc.). Two directions to build on that.

**✅ Shipped so far (commit `ad067e5`, 2026-06-17):** Block Kit approve / approve+don't-ask / deny buttons (#25); approve-by-reaction (`:one:`/`:two:`/`:three:`, ✅/❌); per-agent threads with an anchor per agent + **delete-on-resolve** (#27 threads half); Slack-answer → terminal **mirror** (`flashPane` flashes `↩ Slack: <answer>` on the agent's tmux pane); and a **same-prompt `@wait_since` guard** that fixed a cross-window bug where clicking a stale, never-deleted card injected a keystroke into a live pane (e.g. the orchestrator's) and "rejected" whatever prompt was open. The mirror-only build never deleted cards, so ~250 had accumulated; backlog cleared on deploy. Round-trip + guard verified end-to-end. **Still pending:** live fleet status board (#26), the lifecycle feed (#27 feed half), the `@nexus` command surface, and the inter-agent bus (#28, section B).

### A. Make the bot + channel more useful & organized

**Quick, high-impact**
1. **Interactive Block Kit cards (idea 25).** Replace the plain-text request with a card: agent name + repo/cwd, the command in a code block, a **risk tag** (the classifier already returns read/modify → show "⚠️ modifies state"), and buttons `[Approve] [Approve + don't ask] [Deny]`. A tap emits a Socket Mode `block_actions` event → the bridge maps it to the menu digit and delivers (reusing the pane-id delivery + word→digit logic). The Socket Mode pipe already exists; this adds an action handler + a card builder. Biggest UX win.
2. **Approve-by-reaction.** ✅ on the request approves. Scopes already requested (`reactions:read` + `reaction_added`). Lightest possible path.

**Organization**
3. **Live fleet status board (idea 26).** One bot-maintained message the bot edits (`chat.update`) listing every active agent + state (working / ⏳ waiting-on-you / 🟢 auto-approving / idle / done), pinned. Driven by the existing `PreToolUse` / `Stop` / `Notification` hooks (same data as the tmux status bar). At-a-glance mission control in Slack. Biggest "organized" win.
4. **Per-agent threads (idea 27).** Group each agent's requests under a persistent root message (`🧵 example-service`) so top-level stays clean.
5. **Lifecycle feed (idea 27).** Agent start / turn-finished (`Stop` hook) / idle → posts, so `#nexus` is a fleet activity feed.

**Bigger:** command surface (`@nexus status`, `@nexus <name> <msg>`, `@nexus pause <name>`); or a Slack Canvas dashboard.

→ Lead with **#1 (buttons) + #3 (status board)** — they transform UX and organization respectively.

### B. Slack as the inter-agent message bus (idea 28)

Reframe: **Slack becomes the agent transport.** `agent-send.sh` is local-tmux-only today. Routing it through Slack unlocks:
- **Cross-machine comms** — the Mac fleet ↔ the Linux "nexus" box can finally talk (impossible now; tmux is per-host).
- **Full observability** — every agent-to-agent message is visible/auditable; watch the mesh think, inject from a phone.
- **Durability** — Slack as the comms log.

**Delivery half already exists:** the bridge's inbound routing already does `name: text → deliver to that agent's pane`. So an agent posting `targetname: message` is *already* delivered. Missing pieces:
- **Bridge `/send` endpoint** (localhost): `agent-send.sh → curl :8788/send {to,from,msg}` → bridge posts an addressed message → every host's bridge sees it via Socket Mode → the one whose registry has `to` delivers locally. Loop-safe (bridges already ignore bot/own messages).
- **Dual-mode `agent-send.sh`:** `to` is local → direct send-keys (fast, no noise, default); else → route via Slack for a remote bridge. Optional `--via-slack` to force visibility. Local stays instant; Slack is the cross-host path.
- **Presence registry:** Slack itself can be the registry — bridges maintain an agent↔host map so the Linux box can join the mesh.
- **Hygiene:** agent chatter is noisy → a dedicated `#nexus-agents` channel or thread, separate from human-control `#nexus`.

**Decisions to settle:** globally-unique agent names across hosts; noise control (dedicated channel/thread); local latency (hence dual-mode); ordering (Slack best-effort — acceptable).

**Phasing**
1. Dual-mode `agent-send.sh` + `/send` endpoint, same-machine, posting to a `#nexus-agents` thread — proves the loop and gives instant observability.
2. Multi-host presence registry so the Linux box joins the mesh.

The status board (A3) + agent-comms feed (B) together make `#nexus` live mission-control for a distributed agent mesh.

## Gemini fallback when Claude is down (idea 31)

### Problem

When the Anthropic API has an outage (or is sustained-overloaded), every agent in the fleet stalls — they all route through `nexus-proxy` to Anthropic, with no alternative provider. Goal: when Claude is genuinely down, transparently fall back to **Gemini** (a key we already have) so agents keep running in a degraded "keep-the-lights-on" mode.

The core constraint: Claude Code emits **Anthropic Messages-API** requests (`/v1/messages` with tool-use blocks, streaming SSE, `cache_control`). Gemini speaks a different API. So this is not a URL swap — it needs an Anthropic ⇄ Gemini **translation layer** in the middle.

### Current state of relevant code

- **`proxy/main.py`** (`nexus-proxy`, port 4000) is a transparent Anthropic pass-through that also logs each `/v1/messages` call to Langfuse. Agents reach it via `ANTHROPIC_BASE_URL=http://localhost:4000/sess/<name>`.
  - `_request_with_failover()` (lines ~114-127) and `_stream_response()` (~130-167) implement the existing failover: `UPSTREAM = ANTHROPIC_API_BASE` (corp **the corporate gateway** at `http://host.docker.internal:54777/anthropic`, work-network only) → on `httpx.HTTPError` → `FALLBACK_UPSTREAM = https://api.anthropic.com`.
  - **Trigger gap:** failover only fires on `httpx.HTTPError` (dropped connection / timeout). A **5xx / 529 *response*** (the usual "Anthropic overloaded" signal) is returned to the agent verbatim — it does **not** trip failover. Any fallback work must fix this first.
- **`litellm/config.yaml`** exists but is **orphaned** — no compose service, Dockerfile, or script references it. (It's a single `anthropic/*` passthrough entry.) LiteLLM does **not** run anywhere today.
- The only proxy service in `docker-compose.work.yml` (`proxy:` ~L215) and `docker-compose.yml` (~L146) is `nexus-proxy`. No `litellm` service.
- Secrets: `nexus-proxy` forwards the Anthropic `Authorization`/`x-api-key` header verbatim; the key lives in `.env` (Mac runs vanilla `.env`, no Doppler). No Gemini/Google/Vertex references anywhere in the repo.

### Design — Topology A: LiteLLM as the last-resort Gemini leg

Keep `nexus-proxy` as the front door (so Langfuse tracing + `sess/<name>` tagging are untouched) and add a **third failover tier** that translates to Gemini via a new LiteLLM service. `nexus-proxy` owns the "is Claude down?" decision; only when **both** Anthropic tiers fail does it translate via LiteLLM → Gemini.

```
Claude Code → nexus-proxy:4000
   tier 1: the corporate gateway (corp)        verbatim Anthropic
   tier 2: api.anthropic.com     verbatim Anthropic
   tier 3: LiteLLM → Gemini      Anthropic ⇄ Gemini translation   ← NEW, last resort
```

Chosen over the alternatives (decisions made 2026-06-23):
- **vs. hand-rolling Anthropic⇄Gemini in `nexus-proxy`** — rejected; owning a robust translation of tool-use + streaming SSE + `cache_control` is brittle and a lot of code.
- **vs. claude-code-router** — rejected; a new service that bypasses `nexus-proxy`'s Langfuse tracing unless re-chained.
- **vs. LiteLLM as the brain** (Anthropic primary + Gemini fallback *inside* LiteLLM via its `fallbacks` config) — rejected for now; bigger rewire, would have to replicate the the corporate gateway-SSO corp path inside LiteLLM and reconcile double Langfuse logging. Topology A keeps the trigger logic in our own proxy where we can tune it.

### Concrete changes

1. **`proxy/main.py`** (the heart):
   - **Fix the trigger:** treat **5xx / 529 responses** (`{500, 502, 503, 529}`) from the Anthropic tiers as failures, not just `httpx.HTTPError`. Explicitly **exclude 429** — rate-limits stay on Claude and let its built-in backoff retry (decision: fail over on *hard outages only*, to avoid burning Gemini tokens during routine throttling).
   - **Add tier 3:** after both Anthropic tiers fail, POST to LiteLLM's `/v1/messages` with `body["model"]` rewritten to the Gemini fallback model and the Anthropic auth header **stripped** (LiteLLM holds the Gemini key, not the forwarded `sk-ant-…`). Streaming: only switch to Gemini if no Anthropic chunks have been yielded yet (read `r.status_code` before iterating bytes → clean cutover, no torn streams). Apply to `/v1/messages` and `/v1/messages/count_tokens`.
   - Gemini responses return in Anthropic format (LiteLLM's job) → pipe straight to Claude Code and log to Langfuse tagged with the Gemini model so cost is attributed correctly.
2. **`litellm/config.yaml`** — wire it up for real: one `gemini-fallback` model → `gemini/gemini-2.5-pro` (capability over flash — during an outage you want tool-driven coding to work), `api_key: os.environ/GEMINI_API_KEY`.
3. **`docker-compose.work.yml` + `docker-compose.yml`** — add a `litellm` service under the `proxy` profile (pin to current stable), mount `litellm/config.yaml`, consume `GEMINI_API_KEY`, internal-only; `nexus-proxy` reaches it at `http://litellm:4000`.
4. **`.env.example`** (+ live `.env`) — add `GEMINI_API_KEY=`, `GEMINI_FALLBACK_MODEL=gemini-2.5-pro`, `GEMINI_FALLBACK_ENABLED` (opt-in flag; nothing changes until flipped on with a key present).
5. **Docs** — short `docs/gemini-fallback.md`: the chain, trigger semantics, and a repro test (point both Anthropic tiers at a dead host → confirm a real Gemini answer comes back in Anthropic format and renders in the agent).

### Caveat

Claude Code's harness + tool-calling are tuned for Claude. Gemini-via-translation keeps agents *alive* during an Anthropic outage but expect degraded tool-use reliability and no prompt caching — a keep-the-lights-on cushion, not a transparent equal.

### Alternative worth noting

If the real goal is "agents keep working during an Anthropic outage" (not "use Gemini specifically"), falling back to **Claude on Bedrock or Vertex** avoids translation entirely and keeps model parity — Claude Code natively supports `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX`. More robust than Gemini, contingent on having that cloud access.

## Parked-draft content check for the human-typing guard (idea 32)

### Problem

The human-typing guard (`SLACK_BUS_HUMAN_GRACE_MS`, shipped) holds a bus message
while a human is **actively** typing into the recipient's focused pane — detected from
tmux `client_activity` (last-keystroke recency). It has one blind spot: a draft you
**type and then walk away from**. Once your last keystroke ages past the grace window
(e.g. 10s), `client_activity` no longer flags you as typing, so the bridge delivers the
bus message with `send-keys` — appending into your unsent draft and commingling it with
the injected text. The activity signal knows "typing right now," not "there is unsent
text sitting in the box."

### Design — content-based check (signal B)

Add an optional second predicate to the same two chokepoints (`handleBusMessage`,
`flushBusQueue` in `slack-bridge/index.js`) that `capture-pane -p -t <pane>`s the
recipient and inspects Claude Code's **input box**: if it contains non-empty,
non-placeholder user text, defer the delivery regardless of keystroke recency. This
catches the parked draft that the `client_activity` window misses, and composes with the
existing guard (defer if `@waiting != 2` **OR** actively typing **OR** input box
non-empty).

### Why it was deferred

Parsing the input box is **version-fragile** — it means locating the bordered prompt
region in the captured frame and distinguishing real user text from the greyed
placeholder / hint line, which drifts across Claude Code releases. The activity signal
is version-independent and covers the common case (you're at the keyboard). Ship the
content check only if parked-draft clobbering shows up in practice.

### Notes

- Gate behind its own flag (e.g. `SLACK_BUS_HUMAN_DRAFT_CHECK=1`) so it's independent of
  the recency guard and default-off, matching the fleet's opt-in convention.
- Fail-open on any capture/parse error (treat as "no draft"), same bias as the
  `client_activity` predicate — the `@waiting` gate remains the primary protection.
- Cost: one extra `capture-pane` per idle recipient per flush tick; only evaluated when
  `@waiting=2` and the recency guard has already passed, so it's cheap.
- Reference: guard shipped in `slack-bridge/index.js` (`humanTyping()`), documented under
  "Human-typing guard" in `docs/slack-bridge.md`.
