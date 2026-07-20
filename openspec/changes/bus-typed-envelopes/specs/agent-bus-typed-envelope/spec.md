## ADDED Requirements

### Requirement: Versioned typed A2A envelope

An A2A message SHALL be carried as a versioned envelope with fields `v`, `id`, `ts`, `from`, `to`, `kind`, and `body`, and optional `corr`, `reply_to`, and `meta`. `kind` SHALL be one of `msg`, `request`, `reply`, or `event`. `id` SHALL uniquely identify the message.

#### Scenario: A sent message carries a kind and id
- **WHEN** a message is published to the bus
- **THEN** it carries a `kind` and a unique `id` (defaulting to `kind: msg` when the sender specifies none)

#### Scenario: A reply carries the correlation id
- **WHEN** a message of kind `reply` is published
- **THEN** its `corr` field holds the `id` of the request it answers

### Requirement: Backward-compatible, version-tolerant parsing

Parsing SHALL normalize a legacy message that has no `v`/`kind` — the flat NATS record `{to, from, msg, ts}` and the bare Slack addressed line `token: ↩ from <sender>: <body>` — to an envelope of `kind: msg`. A bridge SHALL ignore envelope fields it does not understand, so an upgraded and a non-upgraded bridge interoperate.

#### Scenario: Legacy record parses as msg
- **WHEN** a bridge receives a `{to, from, msg}` record with no `kind`
- **THEN** it is treated as an envelope of `kind: msg` with `body` = `msg`

#### Scenario: Bare addressed line parses as msg
- **WHEN** a bridge receives a bare `name: ↩ from x: hello` line
- **THEN** it is treated as an envelope of `kind: msg`

#### Scenario: Unknown fields are ignored
- **WHEN** a bridge receives an envelope with fields it does not recognize
- **THEN** it processes the known fields and does not error

### Requirement: Envelope carried unchanged across transports

The same envelope SHALL be carried by both the Slack and NATS transports. A `kind: msg` envelope SHALL serialize on Slack as the existing human-readable addressed line; `request`, `reply`, and `event` MAY use a sentinel-prefixed serialization that never parses as an addressed delivery.

#### Scenario: msg stays human-readable on Slack
- **WHEN** a `kind: msg` envelope is published on the Slack transport
- **THEN** it appears as the existing `to: ↩ from <sender>: <body>` line

#### Scenario: A typed envelope never parses as a delivery address
- **WHEN** a `request`/`reply`/`event` envelope is serialized on Slack with its sentinel
- **THEN** the addressed-line parser does not treat it as a `name: body` delivery
