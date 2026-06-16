-- 003_langfuse_cost_daily.sql
-- Durable daily LLM cost/usage rollup.
--
-- Langfuse stores per-trace cost in ClickHouse `observations`, which we TTL to
-- 10 days (see docs/langfuse-retention.md). This table is the long-term home for
-- the cost view: scripts/langfuse-cost-snapshot.py aggregates observations by
-- (day, project, model) and upserts here before the source rows age out, so
-- spend history survives indefinitely even after the traces are pruned.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS agents.langfuse_cost_daily (
    day                     DATE          NOT NULL,
    project_id              TEXT          NOT NULL,
    model                   TEXT          NOT NULL,   -- coalesced to 'unknown' upstream
    observations            BIGINT        NOT NULL DEFAULT 0,
    total_cost              NUMERIC(18,6) NOT NULL DEFAULT 0,
    input_tokens            BIGINT        NOT NULL DEFAULT 0,
    output_tokens           BIGINT        NOT NULL DEFAULT 0,
    cache_creation_tokens   BIGINT        NOT NULL DEFAULT 0,
    cache_read_tokens       BIGINT        NOT NULL DEFAULT 0,
    total_tokens            BIGINT        NOT NULL DEFAULT 0,
    cost_details            JSONB         NOT NULL DEFAULT '{}',  -- full per-key cost map (future-proof)
    usage_details           JSONB         NOT NULL DEFAULT '{}',  -- full per-key token map
    updated_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (day, project_id, model)
);

CREATE INDEX IF NOT EXISTS idx_langfuse_cost_daily_day
    ON agents.langfuse_cost_daily (day DESC);
