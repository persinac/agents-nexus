## MODIFIED Requirements

### Requirement: agent-send.sh is unchanged by transport selection

The A2A client SHALL continue to POST to the bridge `/send` endpoint, and transport selection SHALL remain bridge-side. `/send` SHALL additionally accept optional `kind`, `corr`, and `reply_to` fields that populate the typed envelope; when they are absent the message is `kind: msg` and behaves exactly as before. No NATS client or credential SHALL be required in an agent shell.

#### Scenario: The client posts to /send regardless of transport
- **WHEN** `agent-send.sh` routes a message through the bus
- **THEN** it POSTs `:8788/send` and the bridge publishes the envelope via the active transport

#### Scenario: A kind-less send is msg (unchanged)
- **WHEN** `/send` receives `{ to, from, msg }` with no `kind`
- **THEN** the bridge publishes a `kind: msg` envelope and delivers it exactly as today

#### Scenario: A typed send carries kind through the transport
- **WHEN** `/send` receives a `kind`/`corr`/`reply_to`
- **THEN** the published envelope carries those fields across the active transport (Slack or NATS) and is rendered by kind on delivery
