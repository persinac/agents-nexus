## ADDED Requirements

### Requirement: Pin guard exempts kept windows from reaping

The reaper SHALL never reap a tmux window marked as kept, even when invoked with `REAP_ALL=1`. A window is marked kept by a `@keep` window option (set to `1`). The pin guard SHALL take precedence over the `REAP_ALL` exemption-dropping behavior, alongside the existing `~/.tmux/overseer-exclude` and attached-window guards.

#### Scenario: Kept window survives a REAP_ALL sweep
- **WHEN** the reaper runs with `REAP_ALL=1` and an idle window has `@keep` set to `1`
- **THEN** the window is not reaped, regardless of its idle time

#### Scenario: Unkept window is still reapable
- **WHEN** the reaper runs and an idle window does not have `@keep` set
- **THEN** the window remains subject to the normal idle/exclude reaping rules

### Requirement: Durable agent ledger

The orchestrator SHALL maintain a durable, on-disk ledger of agents it spawns and of agents that are reaped. Each ledger entry SHALL record at least: repo, agent name, the originating seed prompt (for spawned agents), spawn timestamp, current state (`live` or `dormant`), and a pointer to the agent's last checkpoint (for dormant agents). The ledger SHALL survive a bridge restart and a reaper sweep.

#### Scenario: Spawn is recorded
- **WHEN** the orchestrator spawns an agent
- **THEN** a ledger entry is written with state `live`, the repo, the seed prompt, and the spawn timestamp

#### Scenario: Reap marks the entry dormant with a checkpoint pointer
- **WHEN** the reaper checkpoints-then-kills an agent that has a ledger entry
- **THEN** the entry is updated to state `dormant` with a pointer to the checkpoint produced by that reap

#### Scenario: Ledger survives bridge restart
- **WHEN** the bridge restarts
- **THEN** the ledger is re-read from disk and prior entries (including dormant ones) are still available

### Requirement: Lock-seeding from the ledger

On startup the orchestrator SHALL seed its per-repo in-flight lock set from the ledger's `live` entries in addition to the live tmux registry, so a repo with a known-live agent is treated as locked even if the registry read is incomplete.

#### Scenario: Live ledger entry seeds the lock
- **WHEN** the bridge starts and the ledger contains a `live` entry for a repo
- **THEN** that repo is treated as locked until the entry is resolved (reaped → dormant, or confirmed gone)

### Requirement: Restore a dormant agent from its checkpoint

The orchestrator SHALL provide a restore action that respawns a dormant agent in its recorded repo, seeded from its last checkpoint, reusing the spawn machinery. Restore SHALL be available on demand via a Slack command and via a Block Kit card. A restored agent's ledger entry SHALL return to state `live`. Restore SHALL be subject to the same guardrails as a spawn (allowlist, per-repo lock, rate-limit).

#### Scenario: On-demand restore respawns from checkpoint
- **WHEN** a user invokes restore for a repo (or a dormant agent) and the guardrails permit it
- **THEN** the orchestrator spawns an agent in that repo seeded from the dormant entry's checkpoint, and the entry returns to `live`

#### Scenario: Restore respects guardrails
- **WHEN** a restore would violate a guardrail (repo not on allowlist, repo already locked/live, or rate-limit exceeded)
- **THEN** the restore is rejected with the specific reason and no agent is spawned

#### Scenario: Restore of an already-live agent is a no-op
- **WHEN** a user requests restore for a repo whose agent is already `live`
- **THEN** the orchestrator does not spawn a duplicate and informs the user the agent is already running

### Requirement: Reconnect nudge for reaped agents

When agents were reaped while the user was away, the orchestrator SHALL be able to surface a nudge offering to restore them. The nudge SHALL report how many dormant agents exist and offer a one-action restore (individually or as a set). The nudge SHALL NOT restore anything automatically without an explicit action.

#### Scenario: Nudge offers restore of dormant agents
- **WHEN** there are dormant ledger entries that were reaped since the user's last presence
- **THEN** the orchestrator can post a nudge stating the count and offering a restore action

#### Scenario: Nudge never auto-restores
- **WHEN** the nudge is posted and the user takes no action
- **THEN** no agent is restored
