## Why

The slack-bridge already routes inbound Slack messages to *running* agents (thread-reply, `name: text`, and a haiku classifier over the live registry). But when a message matches no running agent — or classifies below confidence — the bridge dead-ends with a usage hint (`slack-bridge/index.js:586`). The human then has to manually find the right repo, open a tmux window, and start an agent. This change closes that gap: turn the dead-end into a confirm-gated "spin up the right agent for this" branch, so Slack becomes the single front door for dispatching work to the fleet.

## What Changes

- **New spawn branch in the bridge.** When routing falls through (no agent matched, or below `SLACK_ROUTE_MIN_CONFIDENCE`), the bridge resolves which repo the message concerns via the Spark MCP, then offers to spawn a fresh agent there instead of replying with a usage hint.
- **Block Kit confirmation gate.** Spawning is never automatic. The bridge posts a "Spin up an agent in `repo`? [Yes / No]" card (reusing the shipped action-handler pattern at `index.js:620`). The agent is launched only on explicit approval.
- **Seed-prompt passthrough.** The approved spawn launches `open-claude.sh` with the originating Slack message injected as the agent's opening prompt (via a new `SEED_PROMPT` env var), avoiding any tmux send-keys readiness race.
- **Guardrails (the safety surface).** Three controls gate spawning: a per-repo in-flight lock (no duplicate concurrent spawns for the same repo), a global rate-limit (cap spawns per rolling window), and a spawnable-repo allowlist (only sanctioned repos can be auto-spawned).
- **Session resilience.** Agents you care about must survive an AFK gap. Today a PuTTY/network drop is harmless (the tmux server is systemd-persistent), but the overseer reaper runs `REAP_ALL=1` every 15 min and checkpoints-then-kills any agent idle past the threshold — so while you're away, your working set disappears. This change makes that recoverable in two ways: (1) a **pin guard** — windows tagged `@keep` are never reaped, even under `REAP_ALL=1`; and (2) a **durable agent ledger + restore** — every spawned and every reaped agent is recorded with a pointer to its last checkpoint, so a reaped agent can be respawned from that exact checkpoint via a Slack command or a reconnect nudge ("N agents were reaped while you were away — restore?"). Restore reuses the spawn machinery: `open-claude.sh` already seeds a new agent from checkpoint context, so a reaped agent's checkpoint *is* its restore point.
- **Explicitly out of scope:** the proactive agent→Slack lifecycle feed (an agent posting its result back to `#nexus` unprompted). That is IDEAS #27/#28 and is tracked separately; this change only covers inbound dispatch + spawn + resilience.

## Capabilities

### New Capabilities
- `slack-agent-spawn`: When inbound Slack routing finds no suitable running agent, resolve the target repo, confirm with the human via Block Kit, and spawn a new tmux agent in that repo seeded with the originating message.
- `slack-spawn-guardrails`: Safety controls that bound the spawn branch — per-repo in-flight lock, global rate-limit, and a spawnable-repo allowlist — to prevent runaway, duplicate, or unsanctioned agent creation.
- `agent-session-resilience`: Survive an AFK reap — a `@keep` pin guard the reaper honors even under `REAP_ALL=1`, a durable ledger of spawned/reaped agents with checkpoint pointers, and restore-from-checkpoint (on-demand Slack command + reconnect nudge) that respawns dormant agents seeded from their last checkpoint.

### Modified Capabilities
<!-- None. The existing routing ladder is not specced; this adds new behavior at its fall-through point without changing the existing routing requirements. -->

## Impact

- **Code:** `slack-bridge/index.js` (new fall-through spawn branch in `handleMessage`, a new spawn helper, Block Kit confirm + restore cards and their action handlers, in-memory guardrail state, ledger reads/writes, reconnect-nudge logic); `tmux/linux/tmux-scripts/open-claude.sh` (honor `SEED_PROMPT`; honor a restore mode that seeds from a named checkpoint); `scripts/overseer-reap.sh` (honor the `@keep` pin tag even under `REAP_ALL=1`; append a ledger entry when an agent is reaped).
- **New artifact:** a durable agent ledger (on-disk, e.g. under `~/.tmux/`) recording spawned and reaped agents with repo, seed, timestamps, and last-checkpoint pointer. Also lets the guardrail in-flight lock survive a bridge restart.
- **Dependencies:** Spark MCP (`mcp__spark__spark`) becomes a runtime dependency of the bridge for repo resolution. The bridge already controls tmux from systemd (as the reaper and arbiter do), so `tmux new-window` from the service is proven.
- **Config:** new env — spawnable-repo allowlist, rate-limit window/cap, confidence/behavior toggles for the spawn branch, and ledger path.
- **Operational:** spawned agents register normally and are therefore reapable by the existing overseer reaper, so cleanup composes for free; `@keep`-pinned and ledgered agents make that cleanup non-destructive to your working set. No change to the outbound `/notify` path or the arbiter.
