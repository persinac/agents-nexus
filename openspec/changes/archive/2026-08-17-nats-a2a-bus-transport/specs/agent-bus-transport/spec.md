## ADDED Requirements

### Requirement: Pluggable bus transport selected by configuration

The bridge SHALL route A2A publish, subscribe, and presence through a transport interface selected by `NEXUS_BUS_TRANSPORT`. The default SHALL be `slack`, preserving current behavior exactly. Addressing (FQDN parsing), the delivery layer (send-keys / SDK inbox), and the `@waiting` idle-gate SHALL NOT depend on the selected transport.

#### Scenario: Default transport is Slack
- **WHEN** `NEXUS_BUS_TRANSPORT` is unset or `slack`
- **THEN** the bridge publishes and subscribes A2A traffic exactly as it does today, with no dependence on any other broker

#### Scenario: NATS transport is selected
- **WHEN** `NEXUS_BUS_TRANSPORT=nats`
- **THEN** A2A publish, subscribe, and presence go through the NATS transport while routing and delivery behave identically

#### Scenario: Delivery is transport-agnostic
- **WHEN** a bus message is delivered to a local agent under either transport
- **THEN** it is delivered through the same `deliverToName/Slot/Pane` path (send-keys or SDK inbox) with the same sender-tagged text

### Requirement: agent-send.sh is unchanged by transport selection

The A2A client SHALL continue to POST `{ to, from, msg }` to the bridge `/send` endpoint. Transport selection SHALL be entirely bridge-side; no NATS client or credential SHALL be required in an agent shell.

#### Scenario: The client posts to /send regardless of transport
- **WHEN** `agent-send.sh` routes a message through the bus
- **THEN** it POSTs `:8788/send` and the bridge publishes it via the active transport

### Requirement: Human notify/reply leg stays on Slack

The transport seam SHALL cover only the A2A path (`/send`, presence, inbound A2A delivery). The human notify/reply leg — `/notify`, thread-tracking, the human control channel round-trip, and `/relay` — SHALL remain on Slack irrespective of `NEXUS_BUS_TRANSPORT`.

#### Scenario: Notify stays on Slack under NATS A2A
- **WHEN** `NEXUS_BUS_TRANSPORT=nats` and an agent hits `/notify`
- **THEN** the notification is posted to the human Slack channel and tracked in a thread as before

### Requirement: Dual-run migration and flag rollback

The bridge SHALL support running NATS as the active A2A transport on a host while the human notify/reply leg remains on Slack, and SHALL revert to the Slack A2A transport by setting `NEXUS_BUS_TRANSPORT=slack` and restarting, with no other change.

#### Scenario: Rollback restores the Slack bus
- **WHEN** a host running the NATS transport sets `NEXUS_BUS_TRANSPORT=slack` and restarts the bridge
- **THEN** A2A traffic is published and delivered over the Slack bus again, using the in-memory idle-gate
