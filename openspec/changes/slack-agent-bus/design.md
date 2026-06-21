## Context

Agent-to-agent messaging today is `tmux/mac/tmux-scripts/agent-send.sh` (symlinked as `~/.tmux/agent-send.sh` on every host; Windows has its own near-identical copy). ~27 lines: resolve the target (`%pane` exact / bare slot number / name → registry → window index) to a `send-keys` destination, flatten the message to one line, `tmux send-keys -l -t DEST` + `Enter`. It is **local-only** — `send-keys` requires the caller and target to share one tmux server, i.e. the same host — and the message carries **no sender identity and no return path**. Agents invoke it via raw Bash; `open-claude.sh` injects the peer list + the messaging instruction into each agent's opening prompt (the README's `/msg <slot>` is shorthand for that, not a real slash command).

The slack-bridge (`slack-bridge/index.js`) already supplies most of the other half:
- **Receive half exists.** `handleMessage` already turns `name: text` in a watched channel into a pane delivery (`deliverToPane`/registry lookup). An agent posting `peer: msg` is *already* delivered — same host.
- **Outbound HTTP pattern exists.** `POST localhost:8788/notify` (the Notification hook) is the proven shape to mirror for `/send`.
- **Loop-safety exists.** The bridge ignores `bot_id` and its own user id, so its posts are not re-ingested.
- **Runs on every host** over Socket Mode, and the **spawn branch** (`slack-orchestrator-spawn`) just shipped behind `SLACK_SPAWN_ENABLED` — the same opt-in pattern to follow.

Hard constraints: each host has its own bridge and its own `~/.tmux/registry`; tmux is per-host; Slack is the only medium every host shares. The private-channel gotcha applies — a bot needs BOTH the `message.<type>` EVENT and the `<type>:history` SCOPE, configured separately (the `message.groups` lesson from `#nexus-lan`).

## Goals / Non-Goals

**Goals:**
- Cross-host A2A: any agent can message any other agent regardless of host.
- Keep same-host delivery instant and byte-for-byte unchanged (`send-keys`), so the bus adds zero latency to the common case.
- Sender identity + reply: the recipient sees who sent the message and can reply.
- Full observability: every inter-agent message is visible and auditable in Slack.
- Opt-in, off by default; `agent-send.sh` callers are unchanged until it is enabled.
- Reuse the existing bridge delivery (`name:`→pane), the `/notify` pattern, and the existing loop-safety.

**Non-Goals:**
- Replacing local `send-keys` — it stays the default fast path.
- Guaranteed ordering or exactly-once delivery — Slack best-effort is acceptable for A2A.
- The proactive agent→Slack lifecycle feed (IDEAS #27).
- Changing the human-control routing ladder or the spawn branch.
- Redesigning broadcast — `agent-registry.sh broadcast` already exists; this is addressed *unicast*.

## Decisions

### Dual-mode in agent-send.sh, local-first
Resolve the target against the LOCAL registry first; a local hit takes the existing `send-keys` path untouched. Only a non-local target (or `--via-slack`) routes through the bridge. This keeps local latency and channel noise at zero and makes Slack strictly the cross-host path. **Alternative considered:** route everything through Slack for uniform observability — rejected; it adds latency + noise to the overwhelmingly-common same-host case and couples local A2A to bridge/Slack uptime.

### /send mirrors /notify
Add `POST localhost:8788/send {to, from, msg}` to the bridge's existing HTTP server, a sibling of `/notify`. It posts an addressed message to `#nexus-agents`, reusing the same `WebClient`. **Alternative considered:** a separate daemon/port for the bus — rejected; it would duplicate the bridge's Slack client and registry access for no benefit.

### Delivery via the existing inbound routing, fanned out by Socket Mode
`/send` posts `to: msg` (with a `from` tag) into `#nexus-agents`; every host's bridge receives it over Socket Mode; the host whose registry contains `to` delivers locally; the others no-op. Cross-host delivery thus falls out of code that already exists — the only new logic is "post to the channel" and "is `to` mine?". **Alternative considered:** point-to-point HTTP between hosts — rejected; it needs host discovery + reachability (Tailscale) and re-implements the fan-out Socket Mode already gives us.

### Sender identity carried as `from`, delivered as a prefix
`agent-send.sh` resolves its own identity (`PROJECT_SLUG`, or its registry `NAME`) and passes `from`. The bridge prefixes the delivered text (e.g. `↩ from <from>: <msg>`) so the recipient can reply by addressing `from: …` back through the same bus. **Alternative considered:** a structured envelope with reply-correlation ids — deferred; a `from`-prefix is enough for a human-readable reply and needs no new state.

### Idle-gated delivery + same-host channel mode (the durable buffer)
The real failure mode of same-host A2A is **lost messages**: a `send-keys` into an agent that is mid-task is swallowed or interrupts its run. So delivery is gated on the recipient's `@waiting` window-option — the hook-maintained state the arbiter + reaper already use (`2` = idle at the prompt, `1` = permission prompt, `0`/unset = working). The bridge injects only at `@waiting=2`; otherwise it holds the message in a per-pane queue and a poll flushes it (one per idle window) when the agent next goes idle. `#nexus-agents` is the durable record; the queue makes delivery non-lossy and non-interrupting. To put same-host traffic through this path, `agent-send.sh` gains `SLACK_A2A_SAMEHOST=channel` (route same-host **name** targets via the bus; `%pane`/slot + bare digits stay local so the bridge's own digit deliveries never reroute or loop). Set only in the **agent** env, never the bridge's, to keep the bridge's deliveries local. **Alternatives considered:** (a) scrape pane content for a busy spinner — rejected, brittle vs. the existing `@waiting` signal; (b) have agents pull queued messages via a hook — rejected for v1, needs agent-side changes, whereas bridge-side idle-gating is self-contained. **Deferred:** the queue is in-memory (the channel is the cross-restart backstop) — a disk-persisted queue / channel-replay-on-restart can harden it later.

### Dedicated #nexus-agents channel
Agent chatter is noisy and would bury human-control prompts. A separate channel keeps `#nexus`/`#nexus-lan` clean and gives the mesh its own auditable log. The bridge subscribes to both (each with its `message.<type>` event + `<type>:history` scope). **Alternative considered:** a thread under the human channel — rejected; threads don't fan out cleanly across hosts and clutter the control channel.

### Loop-safety reuses the existing self/bot ignore
The bridge already ignores `bot_id` and its own user id, so a posted bus message is not re-ingested as a new human message; the keystroke delivered into a pane is plain text, not a command that re-triggers a send. **Alternative considered:** explicit message-dedup ids — unnecessary for v1 given self-ignore + the single-owner addressed model.

### Phase 2: presence registry for cross-host identity
Phase 1 assumes target names are unique fleet-wide (true today by convention). Phase 2 has the bridges maintain an agent↔host map so names are disambiguated, exactly one host owns each `to`, and senders can discover who is reachable. **Alternative considered:** enforce globally-unique names at spawn time only — simpler but brittle, and it gives no discovery; a presence map also answers "who is up across the fleet?".

## Risks / Trade-offs

- **Same agent name on two hosts → double delivery** → both registries match `to`. Mitigation: Phase 1 relies on unique-by-convention names + a logged warning; Phase 2 presence map assigns a single owner per name.
- **Slack down / bridge restarting → cross-host messages lost** (best-effort). Mitigation: the local path is independent of Slack; document the bus as best-effort; a future ack/retry can harden it. Local A2A is unaffected.
- **Channel noise from a chatty mesh** → floods `#nexus-agents`. Mitigation: dedicated channel (mutable by humans); optional per-sender rate-limit; humans never need to watch it.
- **Auto-allow prompt stalls the send** → on the Linux box `agent-send.sh`/`curl` is not auto-approved, so the call blocks on a permission prompt. Mitigation: add the `curl :8788/send` invocation to `.claude/settings.local.json` as part of rollout (mac/windows already allow `agent-send.sh *`).
- **`from` is self-reported → impersonation** → a misbehaving agent could spoof a sender. Mitigation: acceptable in a trusted single-operator fleet; Phase 2 presence can validate `from`↔host.
- **Delivery into a busy pane** → `send-keys` injects into whatever the agent is doing. Mitigation: identical to today's local A2A; the receiver treats it as an interrupt message.
- **Reply loops (A→B→A…)** → Mitigation: messages are addressed + visible in `#nexus-agents`, so an operator can see and stop a loop; same exposure as human-in-the-loop today.

## Migration Plan

1. Add `/send` to the bridge behind a flag (`SLACK_BUS_ENABLED`, default off). With it off, `/send` is disabled and `agent-send.sh` never routes remotely — zero behavior change.
2. Create `#nexus-agents`; add its `message.<type>` event + `<type>:history` scope; subscribe the bridge.
3. Make `agent-send.sh` dual-mode (local-first; remote only when the bus is enabled and the target isn't local, or `--via-slack`); resolve `from`.
4. Auto-allow the `curl :8788/send` call on the Linux box (`settings.local.json`).
5. Phase 1 validation: same-host A2A still `send-keys` (unchanged); a `--via-slack` send round-trips through `#nexus-agents` and is delivered + sender-tagged; then a genuine Mac↔Linux cross-host send once both bridges are joined.
6. Phase 2: bridges publish presence; remote sends resolve the owner via the presence map.
7. Rollback: `SLACK_BUS_ENABLED=0` + restart → `agent-send.sh` is purely local `send-keys` again.

## Open Questions

- **Gating in the script:** does `agent-send.sh` read a bus env to decide whether to attempt `/send`, or always try `/send` on a local miss and let the bridge reject when off? Leaning: only attempt `/send` when a bus env is set, to avoid a `curl` on every local miss when the bus is off.
- **`from` identity source:** is `PROJECT_SLUG` always the agent's canonical name and equal to the registry `NAME` the other side resolves? Must confirm slug == registry name, or replies won't address back correctly.
- **Channel privacy:** `#nexus-agents` public or private? Private needs the `message.groups` event (the known gotcha); public is simpler but workspace-visible.
- **`--via-slack` to a local target:** post to the channel for the audit trail but still let the local bridge deliver, or short-circuit to `send-keys`? Leaning: honor `--via-slack` (publish + deliver).
- **Presence transport (Phase 2):** ~~announce-on-channel vs a shared store~~ **RESOLVED: announce-on-channel.** A shared store (agent-memory stack / Tailscale-synced file) reintroduces exactly the cross-host dependency Phase 1 rejected when it chose Slack fan-out over point-to-point HTTP. Each bridge posts a `::nexus-presence:: {v,host,agents[],ts}` full-state snapshot on `#nexus-agents`; peers fold it into an in-memory `host→{agents,ts}` map. Owner = lexically-smallest claiming host (every bridge agrees → exactly one delivers). Behind its own `SLACK_PRESENCE_ENABLED` flag (default off). Trade-off accepted: the slow heartbeat adds modest control-line noise to the agent channel — revisit a dedicated `#nexus-presence` channel if it bites (parsing keys on the sentinel either way).
- **Reply correlation:** is a `from`-prefix enough, or do multi-turn agent dialogs need thread/correlation ids? v1: `from`-prefix.
- **Delivery guarantees:** confirm best-effort is acceptable, or do we want a delivering-host ack back to the sender?
