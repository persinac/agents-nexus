## ADDED Requirements

### Requirement: Provider-agnostic command dispatch

The `/nexus` command surface SHALL be dispatched by a provider-agnostic core that
consumes a normalized `Command` (name, args, user, channel, reply token, provider) and
produces a normalized `Message` (text, ephemeral flag, optional interactive components),
with no platform SDK types in its signature. Each platform SHALL be integrated via a
`NexusProvider` adapter that translates the platform's commands/interactions into
`Command`/`Action` events and renders `Message`s to that platform. A `SlackAdapter` and a
`DiscordAdapter` SHALL both drive the same core.

#### Scenario: Same command handled identically across providers
- **WHEN** `/nexus status` is invoked from Slack and from Discord
- **THEN** both are dispatched through the same core and return the same normalized
  `Message`, rendered per platform (Block Kit for Slack, components for Discord)

#### Scenario: An adapter is inert without config
- **WHEN** a provider's credentials are unset
- **THEN** that adapter is not started and the other provider(s) operate unaffected

### Requirement: Behavior-preserving Slack adapter

Moving the Slack integration behind `SlackAdapter` SHALL NOT change observable Slack
behavior: commands still ack empty within 3s and deliver results over `response_url`, and
the rendered Block Kit for the panel and every reply SHALL be byte-identical to the
pre-refactor output.

#### Scenario: Block Kit output unchanged
- **WHEN** the panel (or any `/nexus` reply) is rendered after the refactor
- **THEN** its Block Kit JSON equals the pre-refactor snapshot

### Requirement: Discord HTTP interactions endpoint with signature verification

The Discord adapter SHALL receive interactions as HTTP POSTs to a public endpoint and
SHALL verify every request's Ed25519 signature over `timestamp + rawBody` against the
application public key, rejecting unverified requests. It SHALL answer Discord's `PING`
(type 1) with a `PONG` (type 2 response body `{type:1}`).

#### Scenario: Unsigned/invalid request rejected
- **WHEN** a POST arrives with a missing or invalid `X-Signature-Ed25519`
- **THEN** the endpoint responds 401 and does not dispatch a command

#### Scenario: PING answered
- **WHEN** Discord sends a `PING` (type 1) with a valid signature
- **THEN** the endpoint responds `{ "type": 1 }`

### Requirement: Deferred ack then follow-up (Discord)

For application-command and component interactions, the Discord adapter SHALL send a
deferred acknowledgement within Discord's 3-second window (interaction response type 5,
with the ephemeral flag when the reply is ephemeral), then deliver the actual result via
the interaction follow-up webhook. Slow command work SHALL NOT block the 3s ack.

#### Scenario: Slow command still responds
- **WHEN** a `/nexus` command's result takes longer than the ack window to compute
- **THEN** the adapter defers within 3s and posts the result via the follow-up webhook

### Requirement: Multi-provider fan-out

An outbound notification (e.g. a permission card, a completion ping) SHALL be sendable to
one or many providers via a `Notifier`, selecting the target provider set explicitly;
absent an explicit set, it goes to the originating provider only.

#### Scenario: Fan-out to selected providers
- **WHEN** a notification is sent with providers `[slack, discord]`
- **THEN** it is delivered on both Slack and Discord, each rendered natively
