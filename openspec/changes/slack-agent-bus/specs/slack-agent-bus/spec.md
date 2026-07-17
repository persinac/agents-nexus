## ADDED Requirements

### Requirement: Local-first dual-mode delivery

`agent-send.sh` SHALL resolve the target against the local host's registry first. When the target is a local agent, it SHALL deliver via `tmux send-keys` exactly as today, with no dependence on Slack or the bridge. Only when the target does not resolve locally (or `--via-slack` is given) SHALL it route the message through the bridge bus.

#### Scenario: Local target uses send-keys unchanged
- **WHEN** `agent-send.sh` is called with a target that resolves in the local registry
- **THEN** it delivers via `tmux send-keys` and does not contact the bridge

#### Scenario: Non-local target routes through the bus
- **WHEN** the target does not resolve locally and the bus is enabled
- **THEN** `agent-send.sh` posts the message to the bridge `/send` endpoint instead of failing with "Agent not found"

#### Scenario: --via-slack forces the bus for a local target
- **WHEN** `agent-send.sh` is called with `--via-slack` and a local target
- **THEN** the message is routed through the bus for visibility and the local agent still receives it

### Requirement: Bus disabled by default

The bus SHALL be opt-in. When the bus feature is disabled, `agent-send.sh` SHALL behave exactly as before — local `send-keys` only — and a non-local target SHALL produce the existing "Agent not found" failure rather than routing through Slack.

#### Scenario: Disabled bus preserves current behavior
- **WHEN** the bus feature is disabled and a target does not resolve locally
- **THEN** `agent-send.sh` reports the target not found and does not contact the bridge

### Requirement: Bridge /send endpoint

The bridge SHALL expose a localhost HTTP endpoint `POST /send` accepting `{ to, from, msg }`. On receipt it SHALL post a single addressed, sender-tagged message to the dedicated agent channel. It SHALL validate required fields, and it SHALL NOT deliver via `send-keys` directly from the HTTP path — delivery happens through the channel round-trip so every host has equal opportunity to deliver.

#### Scenario: Valid send is published to the channel
- **WHEN** the bridge receives `POST /send` with `to`, `from`, and `msg`
- **THEN** it posts an addressed message tagged with the sender to the agent channel and returns success

#### Scenario: Missing fields are rejected
- **WHEN** `POST /send` is missing `to` or `msg`
- **THEN** the bridge returns an error and posts nothing

### Requirement: Cross-host delivery by the owning host

Each host's bridge SHALL observe the agent channel and SHALL deliver a bus message to the target only when the target resolves in that host's local registry. A bridge whose registry does not contain the target SHALL ignore the message — no delivery, no error.

#### Scenario: Owning host delivers
- **WHEN** a bus message addressed to `X` appears in the channel and host H's registry contains `X`
- **THEN** host H's bridge delivers the message to `X`'s pane

#### Scenario: Non-owning host ignores
- **WHEN** a bus message addressed to `X` appears and host H's registry does not contain `X`
- **THEN** host H's bridge does not deliver and does not error

### Requirement: Sender identity and reply-by-address

A delivered bus message SHALL be prefixed with the sender's identity so the recipient can see who sent it and reply by addressing the sender back through the bus.

#### Scenario: Delivered message shows the sender
- **WHEN** a bus message from `A` is delivered to `B`
- **THEN** the text delivered to `B` identifies `A` as the sender

#### Scenario: Recipient can reply to the sender
- **WHEN** `B` replies by addressing `A` (e.g. `A: ...`) through `agent-send.sh`
- **THEN** the reply is routed back to `A` by the same bus rules

### Requirement: Dedicated agent channel isolation

Inter-agent bus traffic SHALL use a dedicated Slack channel separate from the human control channel, so agent chatter does not interleave with human-facing prompts. The bridge SHALL subscribe to the agent channel with the matching `message.<type>` event and `<type>:history` scope, and SHALL ignore its own and other bots' posts to avoid re-processing.

#### Scenario: Bus traffic stays out of the human channel
- **WHEN** agents exchange bus messages
- **THEN** those messages appear only in the dedicated agent channel, not in the human control channel

#### Scenario: Loop-safety on the agent channel
- **WHEN** the bridge posts a bus message and then observes its own post
- **THEN** it ignores its own / bot messages and does not re-process them as new sends

### Requirement: Non-interrupting, buffered delivery

A bus message SHALL NOT be injected into a recipient that is actively working or awaiting a permission decision; the bridge SHALL hold it and deliver it when the recipient is next idle at its prompt, so a running task is never interrupted and the message is not lost. The dedicated channel SHALL serve as the durable record of held messages. Delivery MAY be configurably immediate (idle-gating off) for environments that prefer it.

#### Scenario: Message to a busy recipient is buffered
- **WHEN** a bus message is addressed to an agent that is mid-task
- **THEN** it is not injected immediately; it is held and remains visible in the dedicated channel

#### Scenario: Buffered message is delivered when the recipient goes idle
- **WHEN** that agent returns to an idle prompt
- **THEN** the held message is delivered, identifying its sender

#### Scenario: Idle recipient is delivered promptly
- **WHEN** a bus message is addressed to an agent already idle at its prompt
- **THEN** it is delivered without being held
