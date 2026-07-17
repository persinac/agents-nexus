## ADDED Requirements

### Requirement: Spawnable-repo allowlist

The bridge SHALL spawn agents only in repositories present on a configured spawnable-repo allowlist. A resolved repo absent from the allowlist SHALL NOT be offered for spawning; the bridge SHALL fall back to the usage hint. The allowlist SHALL be configurable without code changes.

#### Scenario: Resolved repo is on the allowlist
- **WHEN** Spark resolves a repo that is present on the allowlist
- **THEN** the bridge proceeds to the confirmation step

#### Scenario: Resolved repo is not on the allowlist
- **WHEN** Spark resolves a repo that is absent from the allowlist
- **THEN** the bridge does not offer a spawn and posts the usage hint

### Requirement: Per-repo in-flight lock

The bridge SHALL hold a per-repo in-flight lock for the duration of a spawn (from confirmation through agent registration). While a repo's lock is held, the bridge SHALL NOT offer or perform another spawn for that same repo. The lock SHALL be released on success, on failure, and on confirmation timeout, so a failed attempt does not permanently block the repo. An agent already running for the repo SHALL count as holding the lock.

#### Scenario: Concurrent spawn for the same repo is blocked
- **WHEN** a spawn for a repo is in flight (or an agent for that repo is already running) and another message resolves to the same repo
- **THEN** the bridge does not start a second spawn and informs the user the repo already has an agent or a spawn in progress

#### Scenario: Lock releases after a failed spawn
- **WHEN** a spawn for a repo fails or its confirmation times out
- **THEN** the per-repo lock is released and a later message can trigger a new spawn for that repo

### Requirement: Global spawn rate-limit

The bridge SHALL enforce a global rate-limit on spawns: at most a configured number of spawns within a configured rolling time window, across all repos. A spawn request that would exceed the limit SHALL be rejected with a clear message and SHALL NOT launch an agent. The window and cap SHALL be configurable.

#### Scenario: Spawn within the rate-limit proceeds
- **WHEN** the number of spawns in the current window is below the configured cap
- **THEN** the spawn is permitted (subject to the other guardrails)

#### Scenario: Spawn exceeding the rate-limit is rejected
- **WHEN** a spawn would exceed the configured cap within the rolling window
- **THEN** the bridge rejects the spawn with a rate-limit message and launches no agent

### Requirement: Guardrails evaluated before launch

All guardrails (allowlist, per-repo lock, rate-limit) SHALL be evaluated before any `tmux new-window` is executed. A guardrail rejection SHALL leave no tmux window, no registry entry, and no orphaned lock.

#### Scenario: Rejection leaves no side effects
- **WHEN** any guardrail rejects a spawn
- **THEN** no tmux window is created, no registry entry is written, and any lock acquired for the attempt is released
