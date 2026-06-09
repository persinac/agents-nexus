## ADDED Requirements

### Requirement: Consistent embedder across index- and query-time

Spark SHALL embed queries with the same embedder and output dimensionality used to build the
index being searched. Because embeddings are not portable across models, a mismatch produces
meaningless or erroring searches; therefore the configured embedder (`SPARK_EMBEDDER`) and its
dimensions MUST be identical for both `embed_texts` (index-time) and `embed_single` (query-time)
against a given index.

#### Scenario: Query embedder matches index embedder
- **WHEN** an index is built with `SPARK_EMBEDDER=bedrock` (Titan v2, 1024d)
- **THEN** every serving surface querying that index SHALL also run with `SPARK_EMBEDDER=bedrock`
  so query vectors are 1024d and comparable

#### Scenario: Dimension mismatch is never served
- **WHEN** an index's vector dimensionality differs from the configured query embedder's output
- **THEN** that index SHALL NOT be served for search (the cutover repoints index path and embedder
  together, never one without the other)

### Requirement: Canonical embedder is AWS Bedrock Titan v2 (1024d)

Spark SHALL default the served embedder to AWS Bedrock Titan Text Embeddings V2
(`amazon.titan-embed-text-v2:0`) at 1024 dimensions. The embedder SHALL remain selectable via
`SPARK_EMBEDDER` (`litellm` | `fastembed` | `bedrock`) so fastembed/Ollama remain available as
fallbacks, but the canonical served index is built and queried with `bedrock`.

#### Scenario: Default served index uses bedrock
- **WHEN** the canonical Spark serving config is loaded
- **THEN** `SPARK_EMBEDDER` SHALL resolve to `bedrock` and embedding dimensions to 1024

#### Scenario: Fallback embedder remains available
- **WHEN** an operator sets `SPARK_EMBEDDER=fastembed` (or `litellm`) and rebuilds
- **THEN** Spark SHALL build and query that index with the selected embedder without code changes

### Requirement: All serving surfaces resolve to one canonical index

Every Spark serving surface SHALL run the canonical `agents-nexus/spark` code and resolve to a
single canonical index path built with the canonical embedder. This applies to the local stdio
MCP (`/usr/local/bin/spark`), the container SSE server (`nexus-spark`), and the nightly
`spark sync`. The legacy standalone `guilty-spark` checkout SHALL NOT be the served code.

#### Scenario: Local MCP and container query the same index
- **WHEN** the local MCP and the container both serve search
- **THEN** both SHALL resolve to the same canonical index path and the same embedder/dimensions

#### Scenario: Config resolves unambiguously
- **WHEN** Spark loads configuration (env, `.env`, `config.yaml`)
- **THEN** the effective index path and embedder SHALL be sourced from one documented place, with
  no silent redirection by a stale lower-precedence layer

### Requirement: Query-time credential failures are surfaced, not silent

When the canonical embedder requires external credentials (Bedrock), Spark SHALL surface
non-retryable credential/authorization failures loudly (raising `BedrockAuthError`) rather than
silently returning zero vectors or empty results. Operating on short-lived SSO credentials is an
accepted near-term posture; the failure mode (token rotation) MUST be observable.

#### Scenario: Expired/again-unauthorized credentials abort loudly
- **WHEN** query-time embedding is attempted and the credentials are expired or unauthorized for
  `bedrock:InvokeModel`
- **THEN** Spark SHALL raise a clear `BedrockAuthError` (not return zero vectors), making the
  failure visible to the operator

#### Scenario: Index build aborts on fatal auth instead of writing zero vectors
- **WHEN** a reclaim/sync hits a fatal Bedrock auth error
- **THEN** the build SHALL abort (preflight + fatal-abort) rather than persist an index of zero
  vectors
