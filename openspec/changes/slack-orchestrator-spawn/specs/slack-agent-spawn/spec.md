## ADDED Requirements

### Requirement: Spawn branch on routing fall-through

When inbound Slack routing finds no suitable running agent, the bridge SHALL attempt to spawn a new agent instead of replying with only a usage hint. The spawn attempt SHALL be triggered exactly when the existing routing ladder falls through: the message is unaddressed (no `name:`/`slot:` prefix and not a tracked thread reply) AND the classifier either returns no agent or returns a confidence below `SLACK_ROUTE_MIN_CONFIDENCE`.

#### Scenario: Unaddressed message with no running match triggers spawn flow
- **WHEN** an unaddressed Slack message arrives and the classifier returns no agent (or confidence below threshold)
- **THEN** the bridge enters the spawn flow (repo resolution → confirmation) rather than posting the usage-hint reply

#### Scenario: Addressed and high-confidence messages are unaffected
- **WHEN** a message is addressed (`name: text`), is a tracked thread reply, or classifies to a running agent at or above threshold
- **THEN** the bridge routes to the existing agent as before and does NOT enter the spawn flow

### Requirement: Repo resolution via Spark

Before offering to spawn, the bridge SHALL resolve which repository the message concerns by querying the Spark MCP (`mcp__spark__spark`). If Spark returns no repo with sufficient confidence, the bridge SHALL fall back to the existing usage hint and SHALL NOT offer a spawn.

#### Scenario: Spark resolves a repo
- **WHEN** the spawn flow queries Spark and a repository is returned above the resolution threshold
- **THEN** the bridge proceeds to the confirmation step targeting that repo

#### Scenario: Spark cannot resolve a repo
- **WHEN** Spark returns no repo, or only below-threshold candidates
- **THEN** the bridge posts the usage hint and does not offer a spawn

### Requirement: Confirmation gate before spawning

The bridge SHALL NOT spawn an agent automatically. It SHALL post a Block Kit confirmation card naming the resolved repo with explicit Yes/No actions, and SHALL spawn only upon an explicit Yes from a user. A No, a timeout, or no response SHALL result in no spawn.

#### Scenario: User approves the spawn
- **WHEN** the bridge has posted the confirmation card and a user clicks Yes
- **THEN** the bridge spawns the agent in the resolved repo and acknowledges in-thread

#### Scenario: User declines the spawn
- **WHEN** a user clicks No on the confirmation card
- **THEN** no agent is spawned and the bridge acknowledges the cancellation

#### Scenario: No spawn without explicit approval
- **WHEN** the confirmation card receives no Yes action
- **THEN** no agent is spawned

### Requirement: Spawn with seeded prompt

On approval, the bridge SHALL launch a new tmux agent in the resolved repo using the existing launch primitive (`open-claude.sh` via `tmux new-window`), passing the originating Slack message to the agent as its opening prompt through a `SEED_PROMPT` environment variable. The launch SHALL NOT rely on `tmux send-keys` to deliver the message, to avoid a terminal-readiness race.

#### Scenario: Seeded agent starts working on the request
- **WHEN** the bridge spawns the agent after approval
- **THEN** the new agent is launched with `SEED_PROMPT` set to the originating message and begins from that prompt

#### Scenario: Launch primitive honors SEED_PROMPT
- **WHEN** `open-claude.sh` runs with `SEED_PROMPT` set
- **THEN** it execs `claude` with that prompt as the opening message instead of (or in addition to) the checkpoint context, without using send-keys

### Requirement: Spawn acknowledgement and failure reporting

After an approved spawn, the bridge SHALL report the outcome in the originating Slack thread: on success, the new agent's name/slot; on failure (launch error, or a guardrail rejection), a clear reason. A guardrail rejection SHALL NOT appear to the user as a successful spawn.

#### Scenario: Successful spawn is acknowledged with identity
- **WHEN** the spawn succeeds
- **THEN** the bridge replies in-thread with the spawned agent's name and slot

#### Scenario: Failed spawn reports the reason
- **WHEN** the spawn fails to launch or is rejected by a guardrail
- **THEN** the bridge replies in-thread with the specific reason and no agent is left registered
