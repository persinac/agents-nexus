## Context

The slack-bridge (`slack-bridge/index.js`, ~692 lines) is a Socket Mode Slack app that routes inbound messages to running tmux agents and posts agent notifications back to `#nexus`. Its `handleMessage` routing ladder has three rungs: (1) thread-reply → that agent, (2) `name:`/`slot:` prefix → registry lookup, (3) unaddressed → `classifyTarget()` haiku call over the live registry → deliver if `confidence ≥ SLACK_ROUTE_MIN_CONFIDENCE` (default 0.6). When all three miss, `index.js:586` posts a usage hint — a dead end.

This change replaces that dead end with a spawn branch. The infrastructure already exists:
- **Spawn primitive**: `tmux/linux/tmux-scripts/open-claude.sh`, launched via `tmux new-window -d -n NAME -c DIR`, registers the agent and execs `claude "$prompt"` (`open-claude.sh:129`).
- **tmux-from-systemd**: the reaper and arbiter already drive tmux from systemd units, so the bridge spawning windows is a proven pattern (same unit env: `TMUX_SESSION=agents`, PATH including the fnm bin).
- **Block Kit action handler**: the permission-prompt buttons (`index.js:620`) already demonstrate the post-card → receive-action → act loop to reuse.
- **Spark MCP**: `mcp__spark__spark` answers "which repo handles X" — the missing repo-resolution step.

The arbiter (`arbiter/index.js`) is a read-only tmux→WebSocket monitor; it is the wrong layer for control logic. Orchestration belongs in the bridge.

**Resilience context (diagnosed, not assumed).** A user-reported symptom — "after my PuTTY session drops while AFK, none of my windows are up" — was traced to the reaper, not the disconnect. The tmux server runs in cgroup `user@1000.service/.../tmux-agents-session.service` (a systemd user *service*) with `Linger=yes` and `KillUserProcesses=no`, so a client disconnect merely detaches; the server and windows persist and the login bootstrap reattaches via `tmux new-session -A -s agents`. The actual loss comes from `scripts/overseer-reap.sh`, which runs `REAP_ALL=1` every 15 min; under `REAP_ALL=1` it drops the `overseer`/`orchestrator`/`@orchestrator` exemptions, leaving only `~/.tmux/overseer-exclude` and the *attached-and-viewed* guard. While AFK nothing is attached, so any agent idle past `REAP_IDLE_SECS` (4h) is checkpointed and killed. Crucially, the reaper **checkpoints before killing**, so a reaped agent's state is preserved — which is what makes restore-from-checkpoint viable rather than just prevention.

## Goals / Non-Goals

**Goals:**
- Turn the routing fall-through into a confirm-gated "spawn the right agent" flow.
- Resolve the target repo automatically (Spark) so the human only confirms, not searches.
- Make spawning safe by default: nothing launches without explicit approval and three guardrails.
- Seed the new agent with the originating message with no send-keys race.
- Compose with existing lifecycle: spawned agents register normally and are reapable.
- Make the working set survive an AFK reap: pin what's active, and make the rest restorable from its checkpoint.

**Non-Goals:**
- Proactive agent→Slack lifecycle feed (agent posting results back unprompted) — IDEAS #27/#28, separate change.
- Changing the existing routing rungs (thread/addressed/classifier) — untouched.
- Cross-host spawning — spawns happen on the box running the bridge (Linux), in the local `agents` tmux session.
- Weakening the reaper globally — it still reaps idle, unpinned agents on schedule; resilience makes reaping recoverable, not absent.
- Reconstructing full agent *runtime* state — restore replays the last checkpoint as a seed prompt, not a live process/memory snapshot.

## Decisions

### Extend the bridge, not the arbiter
The bridge already owns inbound classification, outbound posting, and the action-handler loop. Adding the spawn branch there keeps one orchestration brain. **Alternative considered:** a separate orchestrator service — rejected as premature; it would duplicate the registry read, Slack client, and tmux access the bridge already has.

### Confirm-gate every spawn (no auto-spawn)
Auto-spawning tmux agents from a public channel is a cost/runaway/sprawl risk. A Block Kit Yes/No card makes spawning a deliberate human action while keeping it one click. **Alternative considered:** auto-spawn above a high confidence threshold — rejected for v1; the failure mode (a misclassified message spinning up the wrong repo's agent and burning tokens) is worse than one extra click. The threshold path can be added later behind a flag.

### Repo resolution via Spark, gated by its own confidence
`mcp__spark__spark` returns ranked repos for a natural-language query. Use the top hit only if it clears a resolution threshold; otherwise fall back to the usage hint (don't guess). **Alternative considered:** reuse the existing `classifyTarget` haiku call — rejected because that classifier scores against *running agents*, not the repo universe; Spark indexes all repos and is purpose-built for "which repo."

### Seed via SEED_PROMPT env, not send-keys
`open-claude.sh` already execs `claude "$prompt"`. Threading the Slack message in as `SEED_PROMPT` and having the script prefer/merge it into the opening prompt means the agent starts on the task immediately, with zero dependence on terminal readiness. **Alternative considered:** `tmux send-keys` after launch — rejected; it races the claude TUI startup and is the known-flaky path.

### Guardrails as in-memory bridge state
The per-repo lock (a `Set<repo>`), the rate-limit (a timestamp ring/array), and the allowlist (config-loaded) all live in the bridge process. Evaluated in order **allowlist → lock → rate-limit**, all before any `tmux new-window`. **Alternative considered:** a file/Redis-backed lock for cross-restart durability — deferred; a bridge restart is rare and clears in-flight state safely (a half-spawned window would already be registered and visible).

### Lock keyed by repo, and "already-running" counts as locked
The registry already lists running agents by repo/name. The in-flight lock set is seeded by checking the registry **and the durable ledger's `live` entries**: if an agent for the repo is already running, treat the repo as locked (offer to route to it, not spawn a duplicate). This unifies "don't double-spawn" and "don't spawn when one exists," and the ledger makes the seed robust across a bridge restart.

### Resilience: pin + ledger + restore (not reaper removal)
The reaper exists for cost/sprawl control and stays. Resilience is layered on top:
- **Pin guard** — a `@keep` window option the reaper honors even under `REAP_ALL=1`, sitting beside the existing `overseer-exclude`/attached guards. This protects the *active* working set during short/medium AFK without weakening reaping globally. **Alternative considered:** raise `REAP_IDLE_SECS` — rejected as too blunt (delays all cleanup, still loses things on long AFK).
- **Durable ledger** — the single source of truth for "what agents exist and where their last checkpoint is." Spawn writes a `live` entry; the reaper updates it to `dormant` + checkpoint pointer when it kills. **Alternative considered:** derive everything from checkpoint files on disk — rejected; a ledger gives intent (seed prompt, repo, spawn time) and a clean live/dormant state machine that checkpoints alone don't.
- **Restore = spawn seeded from a checkpoint.** `open-claude.sh` already builds its opening prompt from recent checkpoints; restore is the same spawn path with the seed pointed at the dormant entry's checkpoint instead of a fresh Slack message. So restore costs almost no new machinery — it's the spawn branch with a different seed source, run through the same guardrails. **Alternative considered:** a bespoke "rehydrate" path — rejected as redundant with spawn.

### Reap is non-destructive because checkpoint precedes kill
The reaper's existing checkpoint-then-kill ordering is the linchpin: the ledger's dormant pointer is only meaningful because the checkpoint is guaranteed to exist before the window dies. This is also why the known reaper fnm-PATH silent-skip bug matters here — a skipped checkpoint would yield a dormant entry with nothing to restore from. Restore SHALL tolerate a missing checkpoint by falling back to a plain spawn (repo + a note that prior state was unrecoverable).

## Risks / Trade-offs

- **In-memory guardrail state lost on bridge restart** → a restart mid-spawn could allow a duplicate. Mitigation: seed the lock set from the live registry **and the durable ledger's `live` entries** on startup; the rate-limit window resetting on restart is acceptable (restarts are infrequent and operator-initiated).
- **Reaped agent has no usable checkpoint** (skipped checkpoint, e.g. the fnm-PATH silent-skip class of bug) → dormant entry points at nothing. Mitigation: restore falls back to a plain spawn and tells the user prior state was unrecoverable; the ledger entry still captures repo + original seed prompt as a weaker restore basis.
- **Ledger drifts from reality** (manual `tmux kill-window`, crash, external reap) → ledger says `live` but the agent is gone, falsely locking the repo. Mitigation: reconcile the ledger against the live registry on startup and before each lock decision — an entry with no matching registry/pane is downgraded (to `dormant` if a checkpoint exists, else cleared).
- **Pin guard hides a genuinely runaway agent** → a `@keep` agent never gets reaped even if stuck. Mitigation: `@keep` is opt-in and user-applied to the working set; it does not change idle reporting, so the agent is still visible as idle/long-running and can be killed manually.
- **Spark misresolves the repo** → human sees the wrong repo on the confirm card and clicks No; no agent spawned. The confirm gate is the mitigation — the card MUST name the repo plainly so a wrong guess is obvious.
- **Runaway spawns from a chatty channel** → global rate-limit caps blast radius; per-repo lock prevents duplicates; allowlist bounds which repos are even eligible.
- **Spawned agent inherits cost** → every spawn is a running Claude session billing tokens. Mitigation: allowlist + confirm + rate-limit, and the existing reaper auto-cleans idle spawns.
- **tmux/PATH env drift from systemd** → the known fnm-PATH class of bug (claude not found). Mitigation: replicate the reaper/arbiter unit env exactly; `open-claude.sh` already prepends the fnm bin defensively.
- **Confirmation card orphaned (never clicked)** → the per-repo lock must release on confirmation timeout, or the repo is blocked forever. Mitigation: lock acquired at confirm-post is released on Yes-complete, No, OR timeout.

## Migration Plan

1. Ship `SEED_PROMPT` support in `open-claude.sh` first (backward-compatible: unset → current behavior). Verify a manual `SEED_PROMPT=... tmux new-window ... open-claude.sh` seeds correctly.
2. Add the spawn branch + guardrails to the bridge behind a feature flag (e.g. `SLACK_SPAWN_ENABLED`, default off). With the flag off, the fall-through still posts the usage hint — zero behavior change.
3. Configure the allowlist with one or two safe repos, set conservative rate-limit (e.g. small cap per 10 min), enable the flag, and exercise end-to-end in `#nexus`.
4. Rollback: set `SLACK_SPAWN_ENABLED=0` and restart the bridge — instantly reverts to the usage-hint dead end.

## Open Questions

- **Allowlist source**: env var (comma-separated) vs a small JSON/file vs Doppler secret? Leaning env/Doppler for parity with the bridge's existing secret sourcing.
- **Who may approve?** Any channel member, or a restricted set? v1 assumption: any member of the channel where the card was posted; revisit if abuse appears.
- **Rate-limit defaults**: starting values for cap and window need an operational sanity pass (proposal leaves them configurable).
- **Spark resolution threshold**: needs calibration against real messages — start strict (only spawn on a clear top hit) and loosen if it's too conservative.
- **Ledger format/location**: a single JSON file vs JSONL append-log under `~/.tmux/`? Append-log is crash-friendlier; a compacted JSON is simpler to query. Leaning JSONL with periodic compaction.
- **Reconnect-nudge trigger**: how does the bridge detect "the user just came back"? Options — a tmux client-attached hook, a presence signal, or simply surfacing the nudge on the next inbound message in `#nexus`. Leaning the last (no presence plumbing) for v1.
- **Pin UX**: how does the user tag `@keep` — a tmux keybinding, a helper script, or a Slack command (`keep <name>`)? At least one ergonomic path beyond raw `tmux set-option`.
