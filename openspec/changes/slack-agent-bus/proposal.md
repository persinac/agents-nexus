## Why

Agents coordinate through `~/.tmux/agent-send.sh`, which delivers a peer message with `tmux send-keys` — so two agents can only talk if they share one tmux server, i.e. the same host. The Mac fleet and the Linux "nexus" box cannot message each other at all, and even same-host messages carry no sender identity and no reply path. Slack is already the fleet's two-way control plane (the bridge runs on every host over Socket Mode), so routing agent-to-agent traffic through it makes the mesh cross-host, observable, and replyable — reusing delivery machinery that already exists, without slowing down the common same-host case.

## What Changes

- **Dual-mode `agent-send.sh`.** A local target (found in this host's registry) keeps the current `tmux send-keys` fast path, unchanged. A non-local target routes through the bridge instead. An optional `--via-slack` forces the Slack path even for a local target, for visibility.
- **New bridge `/send` endpoint.** A localhost HTTP sibling of `/notify`: it accepts `{ to, from, msg }` and posts a sender-tagged, addressed message to a dedicated `#nexus-agents` channel. Every host's bridge sees it over Socket Mode; the host whose registry holds `to` delivers it locally, reusing the existing inbound `name: text` → pane routing.
- **Sender identity + reply.** Messages carry `from`; the bridge prefixes the delivered text (e.g. `↩ from <agent>: …`) so the recipient knows who asked and can reply by addressing back. A2A carries no sender today.
- **Dedicated `#nexus-agents` channel.** Keeps agent chatter out of the human `#nexus` control channel while making every inter-agent message visible and auditable.
- **Cross-host presence (Phase 2).** Globally-unique agent names plus an agent↔host map maintained by the bridges, so a remote-addressed message is delivered by exactly one host and senders can discover who is reachable.
- **Opt-in, off by default.** A bus enable flag (mirroring `SLACK_SPAWN_ENABLED`); with it unset, `agent-send.sh` behaves exactly as today.
- **Out of scope:** the proactive agent→Slack lifecycle feed (IDEAS #27), and any change to the human-control routing or the spawn branch.

## Capabilities

### New Capabilities
- `slack-agent-bus`: Dual-mode agent-to-agent transport — local delivery stays `tmux send-keys`; a remote (or `--via-slack`-forced) message routes through the bridge's `/send` endpoint into a dedicated Slack channel and is delivered by whichever host owns the target agent, carrying sender identity so the recipient can reply.
- `agent-presence-registry`: Globally-unique cross-host agent identity and an agent↔host presence map maintained by the bridges, so a remote-addressed message is delivered by exactly one host (and senders can tell who is reachable across the fleet).

### Modified Capabilities
<!-- None. The inbound routing ladder and the spawn branch are unchanged; this adds a new /send entrypoint that reuses the existing name:->pane delivery. -->

## Impact

- **Code:** `tmux/mac/tmux-scripts/agent-send.sh` (dual-mode: registry check → local `send-keys` vs `curl :8788/send`; `--via-slack`; resolve own `from` identity) — Linux/Windows use this same file via the `~/.tmux/agent-send.sh` symlink; `slack-bridge/index.js` (new `/send` HTTP handler that reuses the `name:`→pane delivery; sender-tag prefix on delivery; subscribe to the `#nexus-agents` channel; loop-safety already present); `.claude/settings.local.json` on the Linux box (auto-allow the new `curl :8788/send` call — `agent-send.sh` is not auto-approved there today, unlike mac/windows).
- **Config:** new env — bus enable flag, `#nexus-agents` channel id, the bridge port (reuse `8788`), and Phase-2 presence/host-id settings.
- **Slack app:** a new `#nexus-agents` channel plus the matching `message.*` event + `*:history` scope (the private-channel `message.groups` event/scope gotcha applies — adding the scope without the event delivers nothing).
- **Dependencies:** none new — reuses the running bridge, Socket Mode, and the `~/.tmux/registry`. Cross-host delivery requires each host to run a bridge joined to the same workspace + channel.
- **Operational:** same-host latency is unchanged (local stays `send-keys`); the Slack path is best-effort ordering, acceptable for A2A. The bus is inert when the flag is off, so `agent-send.sh` callers see zero behavior change until it is enabled.
