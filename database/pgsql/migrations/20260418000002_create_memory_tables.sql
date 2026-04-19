-- migrate:up

-- Curated knowledge nodes: decisions, insights, checkpoints.
CREATE TABLE agents.memory_nodes (
    id              TEXT        PRIMARY KEY,
    content         TEXT        NOT NULL,
    title           TEXT,
    tags            TEXT[]      NOT NULL DEFAULT '{}',
    embedding       vector(1536),
    attributes      JSONB       NOT NULL DEFAULT '{}',
    source_job_id   TEXT,
    source_agent_role TEXT,
    project         TEXT        NOT NULL,
    access_count    INTEGER     NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed   TIMESTAMPTZ
);

CREATE INDEX idx_memory_nodes_project
    ON agents.memory_nodes (project, created_at DESC);

CREATE INDEX idx_memory_nodes_tags
    ON agents.memory_nodes USING GIN (tags);

-- Named entities referenced by nodes (files, modules, repos, concepts).
CREATE TABLE agents.memory_entities (
    id          TEXT        PRIMARY KEY,
    name        TEXT        NOT NULL,
    entity_type TEXT,
    project     TEXT        NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attributes  JSONB       NOT NULL DEFAULT '{}',
    UNIQUE (name, project)
);

CREATE INDEX idx_memory_entities_project
    ON agents.memory_entities (project);

-- Knowledge graph edges: node → entity references.
CREATE TABLE agents.memory_links (
    from_node   TEXT        NOT NULL REFERENCES agents.memory_nodes(id) ON DELETE CASCADE,
    to_entity   TEXT        NOT NULL,
    link_type   TEXT        NOT NULL DEFAULT 'reference',
    confidence  FLOAT       NOT NULL DEFAULT 1.0,
    reasoning   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_node, to_entity, link_type)
);

CREATE INDEX idx_memory_links_entity
    ON agents.memory_links (to_entity);

-- High-volume event log from tmux hooks (session start/stop, tool use, permission waits).
CREATE TABLE agents.memory_events (
    id          TEXT        PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    device      TEXT        NOT NULL DEFAULT '',
    project     TEXT        NOT NULL,
    repo        TEXT,
    branch      TEXT,
    agent_slot  TEXT,
    session_id  TEXT,
    event_type  TEXT        NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_memory_events_project_time
    ON agents.memory_events (project, timestamp DESC);

CREATE INDEX idx_memory_events_type
    ON agents.memory_events (event_type, timestamp DESC);

CREATE INDEX idx_memory_events_session
    ON agents.memory_events (session_id)
    WHERE session_id IS NOT NULL;

-- migrate:down
DROP TABLE IF EXISTS agents.memory_links;
DROP TABLE IF EXISTS agents.memory_entities;
DROP TABLE IF EXISTS agents.memory_nodes;
DROP TABLE IF EXISTS agents.memory_events;
