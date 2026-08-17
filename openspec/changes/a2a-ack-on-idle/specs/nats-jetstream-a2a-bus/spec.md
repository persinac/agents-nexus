## MODIFIED Requirements

### Requirement: Ack-based idle-gated delivery

A message SHALL NOT be acknowledged until it has been delivered to the recipient at an idle
prompt (`@waiting=2`). While the recipient is busy, the message SHALL remain held
(unacknowledged) and its lease SHALL be extended periodically so it is not prematurely
redelivered; if the bridge restarts while a message is held, that message SHALL be redelivered
and re-held rather than lost.

Every inbound message SHALL be settled exactly once — acknowledged when injected, negatively
acknowledged when it should be retried later, or terminated when redelivery cannot help. A
message that is held SHALL NOT be held indefinitely by one process: after a bounded number of
lease extensions it SHALL be negatively acknowledged and returned to the stream for
rescheduling.

Where the bridge bounds its in-memory hold (a per-recipient queue cap, or a recipient whose pane
has died), it SHALL release the affected messages by negative acknowledgement so they return to
the stream. It SHALL NOT discard a held message.

#### Scenario: Busy recipient holds the message
- **WHEN** a message is addressed to an agent that is mid-task
- **THEN** it is not injected and not acknowledged; it remains held

#### Scenario: Idle recipient is delivered and acknowledged
- **WHEN** that agent returns to an idle prompt
- **THEN** the held message is delivered, sender-tagged, and then acknowledged

#### Scenario: Restart mid-hold does not lose the message
- **WHEN** the bridge restarts while a message is held for a busy recipient
- **THEN** the message is redelivered from the stream and held again until the recipient is idle

#### Scenario: A long hold is not redelivered underneath the holder
- **WHEN** a recipient stays busy for longer than the consumer's ack-wait lease
- **THEN** the held message's lease is extended and it is not redelivered while still held

#### Scenario: A hold that exceeds its ceiling returns to the stream
- **WHEN** a message has been held past the bounded number of lease extensions
- **THEN** it is negatively acknowledged and rescheduled by the stream, not held further

#### Scenario: Queue-cap eviction returns the message to the stream
- **WHEN** the per-recipient hold is at its cap and another message arrives
- **THEN** the evicted message is negatively acknowledged and redelivered later, not discarded

#### Scenario: A recipient whose pane dies releases its held messages
- **WHEN** the recipient's pane is gone while messages are held for it
- **THEN** those messages are negatively acknowledged rather than dropped

#### Scenario: A message this host does not own is terminated
- **WHEN** an inbound message resolves to a recipient owned by another host
- **THEN** it is terminated rather than left unsettled, since redelivery to this host cannot help

### Requirement: Single-owner delivery for duplicate names

An FQDN-qualified target SHALL route to exactly one host's subject. A bare (unqualified) name
claimed by more than one host SHALL be delivered to exactly one owner — resolved to an FQDN via
the presence registry before publication, with a shared queue group as a race safety net — never
to more than one. A bare name SHALL NOT be resolved by defaulting to the publishing host.

#### Scenario: Qualified target bypasses election
- **WHEN** a message targets `hostH/agentA`
- **THEN** it is delivered only by host H, with no election

#### Scenario: Bare duplicate name delivers once
- **WHEN** a message targets bare `agentA` and two hosts claim `agentA`
- **THEN** exactly one host delivers it and the other does not

#### Scenario: Bare duplicate name delivers once even while held
- **WHEN** a bare name is claimed by two hosts and both recipients are busy
- **THEN** exactly one host holds and later injects the message; the other never delivers it
