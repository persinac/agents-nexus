## ADDED Requirements

### Requirement: Deterministic reduction

The projector SHALL materialize a project's canonical state via a pure, deterministic reduction over that project's `memory_events`. Recomputing the projection from the same set of events MUST yield an equivalent projection. The reducer MUST ignore event types it does not recognize rather than fail.

#### Scenario: Idempotent recompute

- **WHEN** the projector reduces the same project's events twice with no new events in between
- **THEN** both runs produce an equivalent `state` for that project

#### Scenario: Unknown event type is ignored

- **WHEN** the event log contains an `event_type` the reducer does not handle
- **THEN** the reduction completes successfully and the unknown event contributes nothing to the projection

### Requirement: Rebuildable read-model, never the sole source of truth

The projection MUST be fully reconstructable from `agents.memory_events` at any time. Deleting or truncating the `memory_projections` table MUST NOT lose any information, and the append-only event log MUST remain the authoritative source.

#### Scenario: Rebuild after truncation

- **WHEN** the `memory_projections` table is truncated and the projector runs again
- **THEN** each active project's projection is regenerated from the event log with equivalent content to before the truncation

#### Scenario: Event log stays authoritative

- **WHEN** a projection row and the event log would imply different state
- **THEN** the system treats the event log as authoritative and the next projection cycle overwrites the row

### Requirement: Canonical current-state content

A project's projection SHALL capture, at minimum: currently-active sessions/agents, a repo-to-owner map resolved last-writer-wins by event timestamp, the set of recently-touched files, and the latest checkpoint for the project. It SHALL record an `updated_at` timestamp and a watermark of how many events were consumed.

#### Scenario: Ownership reflects the latest session

- **WHEN** two sessions touch the same repo and one is more recent by event timestamp
- **THEN** the projection's repo-to-owner map attributes that repo to the more recent session

#### Scenario: Latest checkpoint is surfaced

- **WHEN** multiple checkpoint events exist for a project
- **THEN** the projection exposes the most recent checkpoint

### Requirement: Scheduled freshness with visible staleness

The projector SHALL reproject on a fixed interval and stamp each projection with its `updated_at`. A projection older than the staleness threshold MUST be surfaced as stale rather than hidden or discarded.

#### Scenario: Fresh projection is labeled with its age

- **WHEN** a projection was updated within the staleness threshold and is read at boot
- **THEN** the emitted state section is shown with an age label

#### Scenario: Stale projection is still shown, marked stale

- **WHEN** a projection is older than the staleness threshold (e.g. the projector has stopped)
- **THEN** the state section is still emitted but explicitly marked stale

### Requirement: Recall precedence and fail-open

At agent boot, the canonical project-state section MUST be emitted ahead of the raw-notes section. When no projection exists for the project, or the store is unreachable, recall MUST fail open — omitting the state section without error and leaving the existing notes path unchanged.

#### Scenario: Projection present orders state before notes

- **WHEN** a projection exists for the project at boot
- **THEN** the `## Project State` section appears before the `## Prior Knowledge` notes section

#### Scenario: Projection absent degrades to notes only

- **WHEN** no projection row exists or the database cannot be reached
- **THEN** recall emits no state section, raises no error, and the notes-only behavior is identical to before this change

### Requirement: On-demand projection access

A running agent SHALL be able to fetch the current projection for a project through an MCP tool. Requesting a project with no projection MUST return an empty result, not an error.

#### Scenario: Tool returns the current projection

- **WHEN** an agent calls the project-state tool for a project that has a projection
- **THEN** the tool returns that project's current `state` and its `updated_at`

#### Scenario: Unknown project returns empty

- **WHEN** an agent calls the project-state tool for a project with no projection row
- **THEN** the tool returns an empty result without raising an error
