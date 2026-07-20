## ADDED Requirements

### Requirement: Qualified cross-host instance addressing

A bus message MAY be addressed to a specific remote instance using `host/workspace/name` or `host/pane`. The named host SHALL deliver it to exactly that instance; every other host SHALL ignore it. This replaces the prior behavior where a specific remote instance could not be targeted at all (pane handles were host-local, and a bare `host/name` collided).

#### Scenario: Workspace-qualified address delivers to one instance
- **WHEN** `host/workspace/name` names one live instance
- **THEN** that host delivers the message to exactly that instance's pane and no other agent receives it

#### Scenario: Pane-qualified address is instance-exact
- **WHEN** `host/pane` names a live pane on that host
- **THEN** that host delivers to exactly that pane, even if the pane's agent shares a name and workspace with another

#### Scenario: Non-owning host ignores a qualified address
- **WHEN** a qualified address names host H
- **THEN** hosts other than H take no delivery action

### Requirement: Most-specific-first address resolution

The delivery handler SHALL resolve an addressed token most-specific-first: a bare local pane handle (`wN:pN`), then `host/pane`, then `host/workspace/name`, then `host/name` or a bare `name` via the name index. A more specific match SHALL win over a broader one.

#### Scenario: Bare pane handle stays local
- **WHEN** a message is addressed to a bare `wN:pN`
- **THEN** it resolves to that local pane without consulting cross-host presence

#### Scenario: Qualified beats bare
- **WHEN** both a qualified `host/workspace/name` and a bare `name` could apply
- **THEN** the qualified identity determines the recipient

### Requirement: No silent drop on an unresolved address

An addressed bus message that cannot be resolved to exactly one local instance SHALL be logged with the reason and, when the failure is ambiguity, the qualified retry candidates. It SHALL NOT be silently discarded.

#### Scenario: Ambiguous bare name is logged, not dropped silently
- **WHEN** a bare name matches more than one local instance
- **THEN** the handler logs the ambiguity and the qualified addresses that would disambiguate, and delivers to none

#### Scenario: Unknown qualified target is logged
- **WHEN** a `host/pane` or `host/workspace/name` names no live local instance on this host
- **THEN** the handler logs the miss rather than returning silently
