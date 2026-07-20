## MODIFIED Requirements

### Requirement: Globally-unique agent identity

Across all hosts in the fleet, each active agent SHALL be addressable by an identity that is unique fleet-wide. The identity is `host/workspace/name`; where a `workspace/name` is still shared by two panes, the pane handle SHALL be the instance-exact discriminator. A bare `name` remains a valid address only while it is unique fleet-wide.

Previously this requirement assumed a bare `name` was unique fleet-wide by convention. It is not: two agents may share a name on one host, and the previous presence model (a per-host set of bare names) could neither represent nor address them, silently dropping the message.

#### Scenario: Unique name resolves to one agent
- **WHEN** a message is addressed to a fleet-unique bare name
- **THEN** exactly one agent across the fleet is the intended recipient

#### Scenario: Same name on one host is disambiguated, not dropped
- **WHEN** two agents share a name on the same host and a message is addressed to the bare name
- **THEN** the bus SHALL NOT deliver ambiguously and SHALL surface the qualified addresses (`host/workspace/name` or `host/pane`) needed to reach each one

#### Scenario: Qualified address resolves to exactly one instance
- **WHEN** a message is addressed to `host/workspace/name` (or `host/pane`) that matches one live instance
- **THEN** exactly that instance is the intended recipient, regardless of same-named agents elsewhere

### Requirement: Agent-to-host presence map

The bridges SHALL maintain a shared map of the fleet's live agent **instances**, each carrying its `name`, `workspace`, `pane`, and owning `host`, updated as agents register and deregister. The map SHALL represent two same-named instances on one host as two distinct entries.

Previously the map stored a per-host **set of bare names**, which collapsed same-named instances into one entry.

#### Scenario: Presence reflects a newly registered instance
- **WHEN** an agent registers on host H with workspace W and name N
- **THEN** the presence map contains an instance `H/W/N` distinct from any other same-named instance

#### Scenario: Two same-named instances on one host are both present
- **WHEN** host H has two live agents named N in workspaces W1 and W2
- **THEN** the presence map contains both `H/W1/N` and `H/W2/N` as distinct, individually addressable instances

#### Scenario: Presence reflects a deregistered instance
- **WHEN** an instance's pane closes or it deregisters on host H
- **THEN** the presence map no longer contains that instance, while other same-named instances remain

### Requirement: Single-owner remote delivery

When the presence map is available, a bus message SHALL be delivered by the single host that owns the target **instance**. Owner election operates on the full `host/workspace/name` identity so that a stale or duplicate registry row for the same bare name on another host does not cause double delivery.

#### Scenario: Only the owning host delivers under presence
- **WHEN** the presence map names host H as the owner of `H/W/N` and a message for that instance is published
- **THEN** only host H delivers it, regardless of other hosts' registry state

#### Scenario: Ambiguous bare name defers rather than double-delivers
- **WHEN** a bare name matches more than one instance and no owner can be uniquely elected
- **THEN** no host delivers ambiguously and the non-delivery is logged with the qualified addresses to retry

### Requirement: Reachability discovery

An agent or operator SHALL be able to discover which agent **instances** are currently reachable across the fleet, each reported with its `name`, `workspace`, `pane`, owning `host`, and collision state — one row per instance, not one per name-per-host.

#### Scenario: List reachable instances
- **WHEN** reachability is queried
- **THEN** every currently-live instance is returned, including multiple same-named instances on one host, each with its qualified identity

## ADDED Requirements

### Requirement: Presence wire schema versioning

Presence announcements SHALL carry a schema version. A v2 announcement lists agents as `{name, workspace, pane}` records; a v1 announcement lists bare name strings. A bridge SHALL consume both, folding a v1 bare name into an unqualified instance (`workspace` empty), so bridges of different versions interoperate on one channel.

#### Scenario: v2 consumer reads a v1 peer
- **WHEN** a v2 bridge consumes a v1 presence announcement
- **THEN** each bare name is recorded as an unqualified instance and remains reachable by unique name

#### Scenario: v1 consumer tolerates a v2 peer
- **WHEN** a v1 bridge consumes a v2 presence announcement
- **THEN** it reads the agent names and ignores the extra fields without error

### Requirement: Consistent workspace at registration

Every agent registration SHALL record the agent's workspace, so presence and resolution have a stable discriminator. A registration missing a workspace SHALL be treated as the unqualified (empty) workspace, addressable only by a unique bare name.

#### Scenario: Registration records workspace
- **WHEN** an agent registers via the launcher or the session-start fallback
- **THEN** its registry entry and its presence record both carry its workspace
