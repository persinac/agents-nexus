## ADDED Requirements

### Requirement: Subject-based FQDN addressing with per-host subscription

The NATS transport SHALL publish an A2A message to a deterministic subject derived from the target FQDN (`<host>/<workspace>/<name>`), under a configured prefix (default `nexus.a2a`). Each bridge SHALL subscribe only to its own host's subject subtree, so the broker routes a message to the owning host without fan-out to other hosts. The FQDN↔subject encoding SHALL be reversible and collision-free.

#### Scenario: Message publishes to the target's host subject
- **WHEN** an A2A message is sent to `hostH/wsX/agentA`
- **THEN** it is published to the subject encoding `nexus.a2a.hostH.wsX.agentA`

#### Scenario: A bridge receives only its host's messages
- **WHEN** bridge H subscribes to its host subtree and a message for a different host is published
- **THEN** bridge H does not receive that message from the broker

### Requirement: Durable inbox for offline recipients

A JetStream stream SHALL persist A2A messages. A recipient host whose bridge is offline SHALL, on reconnect, receive the messages addressed to it that were published while it was down, delivered to the intended agent, without loss and without re-delivering already-acknowledged messages.

#### Scenario: Message survives a recipient outage
- **WHEN** a message is sent to an agent whose host bridge is offline, and that bridge later reconnects
- **THEN** the bridge drains the buffered message from the stream and delivers it to the agent

#### Scenario: Acknowledged messages are not redelivered
- **WHEN** a bridge reconnects after having acknowledged earlier messages
- **THEN** it resumes from its consumer cursor and does not receive those acknowledged messages again

### Requirement: Ack-based idle-gated delivery

A message SHALL NOT be acknowledged until it has been delivered to the recipient at an idle prompt (`@waiting=2`). While the recipient is busy, the message SHALL remain held (unacknowledged) and its lease extended so it is not prematurely redelivered; if the bridge restarts while a message is held, that message SHALL be redelivered and re-held rather than lost.

#### Scenario: Busy recipient holds the message
- **WHEN** a message is addressed to an agent that is mid-task
- **THEN** it is not injected and not acknowledged; it remains held

#### Scenario: Idle recipient is delivered and acknowledged
- **WHEN** that agent returns to an idle prompt
- **THEN** the held message is delivered, sender-tagged, and then acknowledged

#### Scenario: Restart mid-hold does not lose the message
- **WHEN** the bridge restarts while a message is held for a busy recipient
- **THEN** the message is redelivered from the stream and held again until the recipient is idle

### Requirement: Single-owner delivery for duplicate names

An FQDN-qualified target SHALL route to exactly one host's subject. A bare (unqualified) name claimed by more than one host SHALL be delivered to exactly one owner — resolved to an FQDN via presence, with a shared queue group as a race safety net — never to more than one.

#### Scenario: Qualified target bypasses election
- **WHEN** a message targets `hostH/agentA`
- **THEN** it is delivered only by host H, with no election

#### Scenario: Bare duplicate name delivers once
- **WHEN** a message targets bare `agentA` and two hosts claim `agentA`
- **THEN** exactly one host delivers it and the other does not

### Requirement: Presence via TTL KV

Live-agent presence SHALL be maintained in a JetStream KV bucket keyed by FQDN, each entry carrying a TTL of roughly twice the heartbeat interval. Each bridge SHALL upsert its live local agents on startup, on a heartbeat, and on a registry change. Any bridge SHALL build the reachability directory and resolve bare names from the bucket. A departed agent's entry SHALL expire by TTL without an explicit tombstone.

#### Scenario: Heartbeat refreshes presence
- **WHEN** a bridge heartbeats
- **THEN** its live agents' KV entries are upserted with a fresh TTL

#### Scenario: Departed agent expires
- **WHEN** an agent's host stops refreshing its entry
- **THEN** the entry expires by TTL and the agent no longer appears in the reachability directory

#### Scenario: Reachability is read from KV
- **WHEN** `/agents` is queried
- **THEN** the directory is assembled from the KV bucket, including any name collisions across hosts

### Requirement: Scale without per-participant application provisioning

Adding a fleet participant SHALL require only issuing a transport credential, not creating or approving a messaging application. The broker SHALL accept the whole fleet's bridge connections without a per-application concurrent-connection cap, and a credential MAY be scoped so a connection can only claim its own host's subjects.

#### Scenario: A new participant joins with a credential
- **WHEN** a new host is added to the fleet
- **THEN** it connects with an issued credential and participates in A2A without a new messaging application being provisioned

#### Scenario: Connection count exceeds the Slack per-app cap
- **WHEN** more bridges connect than a single Slack app's Socket Mode connection cap allows
- **THEN** all bridges remain connected and reachable over the NATS transport
