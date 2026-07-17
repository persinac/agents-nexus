## ADDED Requirements

### Requirement: Globally-unique agent identity

Across all hosts in the fleet, each active agent SHALL be addressable by a name that is unique fleet-wide, so a bus message addressed to that name has exactly one intended recipient.

#### Scenario: Unique name resolves to one agent
- **WHEN** a message is addressed to a fleet-unique name
- **THEN** exactly one agent across the fleet is the intended recipient

#### Scenario: Name collision is detected
- **WHEN** two hosts would register an agent under the same name
- **THEN** the collision is surfaced (and disambiguated by host) rather than silently double-delivering

### Requirement: Agent-to-host presence map

The bridges SHALL maintain a shared map of which host currently owns each active agent, updated as agents register and deregister, so a remote-addressed message can be delivered by exactly one host.

#### Scenario: Presence reflects a newly registered agent
- **WHEN** an agent registers on host H
- **THEN** the presence map associates that agent with host H

#### Scenario: Presence reflects a deregistered agent
- **WHEN** an agent's window closes or it deregisters on host H
- **THEN** the presence map no longer associates that agent with host H

### Requirement: Single-owner remote delivery

When the presence map is available, a bus message SHALL be delivered by the single host that owns the target agent, even if a stale registry on another host also matches the name.

#### Scenario: Only the owning host delivers under presence
- **WHEN** the presence map names host H as the owner of `X` and a message for `X` is published
- **THEN** only host H delivers it, regardless of other hosts' registry state

### Requirement: Reachability discovery

An agent or operator SHALL be able to discover which agents are currently reachable across the fleet, derived from the presence map.

#### Scenario: List reachable agents
- **WHEN** reachability is queried
- **THEN** the set of currently-active agents and their owning hosts is returned
