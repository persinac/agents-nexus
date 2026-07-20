## ADDED Requirements

### Requirement: Send typed messages from agent-send.sh

`agent-send.sh` SHALL keep `agent-send.sh <to> <msg>` as `kind: msg`, unchanged. It SHALL add optional verbs `--request`, `--reply <corr-id>`, and `--event` (with `--reply-to <addr>`) that set the envelope `kind`, `corr`, and `reply_to`. These flags SHALL only add fields to the `/send` request.

#### Scenario: Bare send is unchanged
- **WHEN** `agent-send.sh <to> <msg>` is called with no kind flag
- **THEN** a `kind: msg` message is sent and its delivered text is identical to today's

#### Scenario: Request flag sends a request
- **WHEN** `agent-send.sh --request <to> <msg>` is called
- **THEN** a `kind: request` message is sent with a fresh `id` and `reply_to` set to the sender

#### Scenario: Reply flag correlates to a request
- **WHEN** `agent-send.sh --reply <corr-id> <to> <msg>` is called
- **THEN** a `kind: reply` message is sent with `corr` = `<corr-id>`

### Requirement: Bridge correlates a reply to its request with a deadline

The bridge SHALL record an outstanding `request` (`id → requester reply_to`, with a deadline) and, on receiving a `reply` whose `corr` matches, SHALL route the reply body to the request's `reply_to` and clear the entry. A request that is not answered by its deadline SHALL be resolved with a synthetic `reply` of `meta.status: timeout` (never left dangling).

#### Scenario: Reply routes back to the requester
- **WHEN** agent B sends a `reply` with `corr` = the `id` of a `request` from agent A
- **THEN** the bridge delivers the reply body to A (the request's `reply_to`)

#### Scenario: Unanswered request times out
- **WHEN** a `request` is not answered before its deadline
- **THEN** the requester receives a synthetic reply marked `timeout` and the correlation entry is cleared

### Requirement: Typed delivery rendering with a reply hint

The bridge SHALL render a delivered message by `kind`: `msg` uses the existing `↩ from <sender>:` form; `request` shows the sender, the request `id`, and a one-line hint of the exact reply command; `reply` and `event` are labelled with the sender (and, for a reply, the correlated id).

#### Scenario: A request tells the recipient how to reply
- **WHEN** a `kind: request` message is delivered to an agent
- **THEN** the delivered text identifies the request `id` and shows how to reply (an `agent-send.sh --reply <id> <sender> …` hint)

#### Scenario: msg rendering is unchanged
- **WHEN** a `kind: msg` message is delivered
- **THEN** the delivered text is exactly the current `↩ from <sender>: <body>`

### Requirement: Await-a-reply request endpoint

The bridge SHALL expose `POST /request { to, body, deadline_ms }` that publishes a `request` and resolves the HTTP response when the matching `reply` arrives, or with a `timeout` status when the deadline elapses — so a caller can ask an agent and await a structured answer.

#### Scenario: Request resolves on reply
- **WHEN** `POST /request` is called and the target agent replies before the deadline
- **THEN** the HTTP response resolves with the reply body

#### Scenario: Request resolves on timeout
- **WHEN** no reply arrives before `deadline_ms`
- **THEN** the HTTP response resolves with a `timeout` status rather than hanging
