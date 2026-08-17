## MODIFIED Requirements

### Requirement: Bridge /send endpoint

The bridge SHALL expose a localhost HTTP endpoint `POST /send` accepting `{ to, from, msg }`. On receipt it SHALL publish a single addressed, sender-tagged message through the **active bus transport** selected by `NEXUS_BUS_TRANSPORT`: under Slack it posts to the dedicated agent channel (as today); under NATS it publishes to the target's JetStream subject. It SHALL validate required fields, and it SHALL NOT deliver via `send-keys` directly from the HTTP path — delivery happens through the transport so the owning host delivers exactly once.

#### Scenario: Valid send is published via the active transport
- **WHEN** the bridge receives `POST /send` with `to`, `from`, and `msg`
- **THEN** it publishes an addressed, sender-tagged message through the active transport and returns success

#### Scenario: Missing fields are rejected
- **WHEN** `POST /send` is missing `to` or `msg`
- **THEN** the bridge returns an error and publishes nothing

#### Scenario: Slack transport behavior is unchanged
- **WHEN** `NEXUS_BUS_TRANSPORT` is `slack` (default)
- **THEN** `/send` posts to the dedicated Slack agent channel exactly as before

### Requirement: Cross-host delivery by the owning host

A bus message SHALL be delivered to the target only by the host whose registry owns it; other hosts SHALL neither deliver nor error. Under the Slack transport, every host observes the agent channel and the owning host delivers. Under the NATS transport, the broker routes the message to the owning host's consumer directly — non-owning hosts do not receive it at all (no fleet-wide fan-out).

#### Scenario: Owning host delivers
- **WHEN** a bus message addressed to `X` is published and host H owns `X`
- **THEN** host H's bridge delivers the message to `X`

#### Scenario: Non-owning host does not deliver
- **WHEN** a bus message addressed to `X` is published and host H does not own `X`
- **THEN** host H's bridge does not deliver and does not error (under NATS it does not receive the message)

### Requirement: Non-interrupting, buffered delivery

A bus message SHALL NOT be injected into a recipient that is actively working or awaiting a permission decision; the bridge SHALL hold it and deliver it when the recipient is next idle at its prompt, so a running task is never interrupted and the message is not lost. The held message SHALL survive a bridge restart. Under the Slack transport the durable record is the dedicated channel plus an in-memory hold; under the NATS transport the hold is expressed as a delayed JetStream acknowledgement so the message persists in the stream until delivered. Delivery MAY be configurably immediate (idle-gating off).

#### Scenario: Message to a busy recipient is buffered
- **WHEN** a bus message is addressed to an agent that is mid-task
- **THEN** it is not injected immediately; it is held and remains durably recorded

#### Scenario: Buffered message is delivered when the recipient goes idle
- **WHEN** that agent returns to an idle prompt
- **THEN** the held message is delivered, identifying its sender

#### Scenario: Held message survives a bridge restart
- **WHEN** the bridge restarts while a message is held for a busy recipient
- **THEN** the message is not lost and is delivered once the recipient is idle
