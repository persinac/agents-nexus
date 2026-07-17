## 1. Bridge `/send` endpoint (Phase 1)

- [x] 1.1 Add `SLACK_BUS_ENABLED` (default off) + `SLACK_AGENTS_CHANNEL` config reads at bridge startup in `slack-bridge/index.js`, logging the bus state like the spawn branch does
- [x] 1.2 Add `POST /send` to the existing HTTP server (sibling of `/notify`): parse `{ to, from, msg }`, validate `to` + `msg`, return 400 on missing fields; respond disabled (and post nothing) when `SLACK_BUS_ENABLED` is off
- [x] 1.3 On a valid `/send`, post a sender-tagged addressed message to `SLACK_AGENTS_CHANNEL` via `chat.postMessage` (`to: ↩ from <from>: <msg>` — receive side keys on the leading `to:`)
- [x] 1.4 Smoke: `/send` returns 409 when disabled; the script reaches it; `/notify` still returns its normal 400 (no regression). Valid-200 post verifies at enablement (→ 5.2)

## 2. Inbound delivery on the agents channel

- [x] 2.1 Scope `handleMessage` to also accept the `#nexus-agents` channel (a dedicated bus branch ahead of the human-message filters); event + scope documented in `docs/slack-bridge.md`
- [x] 2.2 Deliver an addressed bus message (`to: text`) on the agents channel to the local registry — deliver to `to`'s pane when present, otherwise ignore with no error (`handleBusMessage`, the owning-host rule)
- [x] 2.3 Prefix delivered text with the sender (`↩ from <from>: <msg>`) so the recipient sees who sent it and can reply by addressing back (baked in at post time, delivered verbatim)
- [x] 2.4 Loop-safety on the agents channel — the bus branch runs ahead of the bot/self filters but `handleBusMessage` only ever delivers (never re-posts), so there is no feedback loop

## 3. Dual-mode `agent-send.sh`

- [x] 3.1 Resolve the caller's own identity for `from` (`AGENT_FROM` → `PROJECT_SLUG` → caller pane's registry `NAME`)
- [x] 3.2 Local-first: when the target resolves in the local registry, deliver via `tmux send-keys` exactly as today (path unchanged)
- [x] 3.3 Non-local target → `curl localhost:8788/send {to,from,msg}`, attempted ONLY when `SLACK_BUS_ENABLED=1` in the agent env (no curl-per-miss when the bus is off)
- [x] 3.4 Add `--via-slack` to force the bus path for a local target (publishes to the channel; the owning host delivers — no direct send-keys, so no double delivery)
- [x] 3.5 Preserve current behavior when the bus is disabled: a non-local target produces the existing "Agent not found" failure, with no network call (verified)
- [ ] 3.6 Keep the Windows `agent-send.sh` variant in parity (dual-mode) — DEFERRED: gap documented in `docs/slack-bridge.md` (Windows copy remains local-only for now)
- [x] 3.7 Same-host channel mode: `SLACK_A2A_SAMEHOST=channel` routes same-host messages through the bus (buffered + idle-gated, not blasted into a busy pane); added `--local` to force the fast path. Flag lives in the AGENT env only. **Revised after a field report (intent: ALL comms through Slack):** a `slot`/`%pane` target is now reverse-resolved to its registry NAME so it round-trips the name-keyed bus too — only a bare control digit (permission-menu input) and an unregistered window stay local. The bridge forces `SLACK_A2A_SAMEHOST=local` on its own delivery calls so a final hop can never re-route/loop. Verified end-to-end: name/slot/%pane → 1 channel post + idle-gated (held while `@waiting≠2`, flushed on idle), no loop on flush; digit + unregistered window stay local.

## 4. Permissions, config & rollout (Phase 1)

- [x] 4.1 Auto-allow the bus call on the Linux box — already covered: `~/.claude/settings.json` wildcard `Bash($HOME/.tmux/agent-send.sh *)` includes the script's internal `curl`
- [x] 4.2 Document the new env (`SLACK_BUS_ENABLED`, `SLACK_AGENTS_CHANNEL`) + the `#nexus-agents` app setup in `docs/slack-bridge.md`, including the `message.<type>` event + `<type>:history` scope gotcha
- [x] 4.3 Enable on the Linux box: `#nexus-agents` created (private, `C0BC1QLBVJ6`); event+scope already covered by the existing private-channel (`#nexus-lan`) setup — `message.groups`/`groups:history` fire for every private channel the bot is in, so nothing new to add; `SLACK_BUS_ENABLED=1` set in Doppler `nexus/prd` (bridge) and exported in `~/.tmux/env.sh` (agents, installer seeds it default-off); bridge restarted; `/health` confirmed `"bus":true`

## 5. Phase 1 verification (same-host + cross-host)

- [x] 5.1 Same-host A2A unchanged: verified with a throwaway `a2a-sink` agent — `agent-send.sh a2a-sink <tok>` delivered via `send-keys` (token in the sink pane) and the token was absent from `#nexus-agents` (no bus post)
- [x] 5.2 `--via-slack` round-trip: `agent-send.sh --via-slack a2a-sink <tok>` posted `a2a-sink: ↩ from agents-nexus: <tok>` to `#nexus-agents`, and the bridge delivered it back to the sink pane with the `from agents-nexus:` tag (full path: script → /send → chat.postMessage → socket event → handleBusMessage → send-keys)
- [x] 5.3 Disabled-bus regression: `SLACK_BUS_ENABLED=0 agent-send.sh <non-local> <tok>` printed the old "Agent not found", attempted no bus/curl, exited 1, and posted nothing to the channel (re-confirmed post-enable)
- [ ] 5.4 Cross-host: NEEDS a second host's bridge on this code joined to `#nexus-agents` (the Mac). Mechanism already proven same-host: the owning host delivers (5.2) and a non-owning host delivers nothing (5.5) — cross-host is those two on separate machines. Pending the Mac bridge running this code + joined.
- [x] 5.5 Negative: `agent-send.sh --via-slack <unowned-name> <tok>` is observable in `#nexus-agents` (the addressed post appears) but delivered nowhere (sink untouched) and errors nowhere (exit 0; bridge logged no delivery for the name)

## 6. Presence registry (Phase 2)

- [x] 6.1 Source of truth: **announce-on-channel** (reuses the bus Socket Mode fan-out — no shared store, consistent with Phase 1 rejecting point-to-point HTTP). Record format: a `::nexus-presence:: {v,host,agents[],ts}` sentinel line (full-state snapshot, not deltas, so a missed beat self-heals). Documented in `design.md` (open question resolved) + `docs/slack-bridge.md`. Own opt-in flag `SLACK_PRESENCE_ENABLED` (default off) so Phase 1 is unchanged.
- [x] 6.2 Publish: `publishPresence` announces this host's LIVE local agents (registry ∩ live tmux panes — dead `%16/infrastructure` correctly excluded) on startup + a 5-min heartbeat + on `fs.watch` registry change (spawn/reap propagates in ~2s). Consume: `consumePresence` folds peer snapshots into `presenceMap` (host→{agents,ts,seen}); verified self-publish and consumption of injected peers via `/agents`.
- [x] 6.3 Collision detection: `presenceCollisions` flags any name claimed by >1 host; logged (`[presence] name collision: …`) and surfaced in `/agents.collisions`. Verified: injecting `aaa-collide` claiming `agents-nexus` produced the collision + warning.
- [x] 6.4 Single-owner delivery: deterministic owner = lexically-smallest claiming host (`ownerOf`); `handleBusMessage` defers when presence names another owner, even on a local registry match. Verified: with `aaa-owner` (< nexus) claiming `a2a-sink`, nexus deferred (no delivery, logged `deferring`); after draining `aaa-owner` (deregister), ownership returned to nexus and it delivered.
- [x] 6.5 Reachability: `GET /agents` returns the live set — each `{name, host, owner, collided}` + the collisions list + `self`/`hosts`/`presence`. `/health` also gained `presence` + `host`.
- [x] 6.6 Verified single-host via synthetic peers (above): collision fires on a duplicate; single-owner deferral holds under a stale/duplicate claim and delivery resumes when the competitor drains; `/agents` lists the live set (and excludes a dead pane). True 2-host presence (two real bridges) is by-construction the same and pends the Mac, like 5.4. Presence reverted to OFF in prod after verification (bus stays on); `SLACK_PRESENCE_ENABLED=1` is the cross-host rollout step.

## 7. Idle-gated delivery — the durable buffer (raised in review)

Motivation: a `send-keys` into an agent that is mid-task is lost or interrupts it. Use Slack as a buffer and deliver only when the recipient is idle.

- [x] 7.1 Read the recipient's `@waiting` window-option (hook-maintained; arbiter/reaper-shared): deliver a bus message only at `@waiting=2` (idle at the prompt); hold otherwise (`paneWaiting`/idle-gate in `handleBusMessage`, behind `SLACK_BUS_DEFER`, default on when the bus is on).
- [x] 7.2 Per-pane in-memory queue (`busQueue`) with a cap (`SLACK_BUS_QUEUE_MAX`, default 50; oldest dropped beyond — still in `#nexus-agents`); `enqueueBus` holds a message for a busy recipient.
- [x] 7.3 Flush poll (`flushBusQueue`, every `SLACK_BUS_FLUSH_MS`=4s): deliver ONE queued message per now-idle recipient (each message gets its own turn); drop queues for dead panes (still recoverable from the channel); retry on a failed send.
- [x] 7.4 `#nexus-agents` is the durable record (replay/audit). DEFERRED: a disk-persisted queue / channel-replay-on-restart to survive a bridge restart mid-buffer — in-memory + channel backstop for v1.
- [x] 7.5 Verify: a message to a BUSY sink (`@waiting=0`) is held (not injected) and logged `queued`; flipping the sink to `@waiting=2` flushes it within ~4s, sender-tagged; an already-idle recipient delivers promptly; `%pane`/digit targets stay local (no reroute → no loop on the bridge's own deliveries).
