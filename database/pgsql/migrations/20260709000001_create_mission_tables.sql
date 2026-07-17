-- migrate:up

-- A mission: a task the Conductor decomposes, executes, verifies, and reports.
-- Local source of truth (resumable + audit); Jira mirrors it for team standup surfacing.
CREATE TABLE agents.missions (
    id                  TEXT        PRIMARY KEY,
    goal                TEXT        NOT NULL,
    type                TEXT        NOT NULL,                       -- building | investigation | analysis
    route               TEXT        NOT NULL DEFAULT 'conductor',   -- one-shot | conductor
    status              TEXT        NOT NULL DEFAULT 'pending',     -- pending|planning|dispatched|verifying|synthesizing|done|failed|escalated
    repos               TEXT[]      NOT NULL DEFAULT '{}',
    datasources         TEXT[]      NOT NULL DEFAULT '{}',
    jira_key            TEXT,                                       -- Jira mirror (standup surface)
    model               TEXT        NOT NULL DEFAULT 'claude-opus-4-8',
    orchestrator_effort TEXT        NOT NULL DEFAULT 'max',         -- Conductor's own judgment nodes
    plan                JSONB       NOT NULL DEFAULT '{}',          -- the MissionPlan
    verdict             JSONB       NOT NULL DEFAULT '{}',          -- last VerifyVerdict
    replan_count        INTEGER     NOT NULL DEFAULT 0,
    max_replans         INTEGER     NOT NULL DEFAULT 5,
    created_by          TEXT,
    device              TEXT        NOT NULL DEFAULT '',
    project             TEXT        NOT NULL DEFAULT 'agents-nexus',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
CREATE INDEX idx_missions_status  ON agents.missions (status, updated_at DESC);
CREATE INDEX idx_missions_project ON agents.missions (project, created_at DESC);
CREATE INDEX idx_missions_jira    ON agents.missions (jira_key) WHERE jira_key IS NOT NULL;

-- One unit of work within a mission: a profile + an (escalating) effort.
CREATE TABLE agents.mission_subtasks (
    id              TEXT        PRIMARY KEY,
    mission_id      TEXT        NOT NULL REFERENCES agents.missions(id) ON DELETE CASCADE,
    subtask_key     TEXT        NOT NULL,                       -- the plan's subtask id
    goal            TEXT        NOT NULL,
    repo            TEXT,
    profile         TEXT        NOT NULL,                       -- name from the profile library
    depends_on      TEXT[]      NOT NULL DEFAULT '{}',          -- DAG edges
    status          TEXT        NOT NULL DEFAULT 'pending',     -- pending|running|done|blocked|error
    effort          TEXT        NOT NULL DEFAULT 'high',        -- high -> xhigh after escalate_after_fails
    attempt         INTEGER     NOT NULL DEFAULT 0,
    worker          TEXT,                                       -- agent name / pane
    result          JSONB       NOT NULL DEFAULT '{}',          -- the WorkerResult
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (mission_id, subtask_key)
);
CREATE INDEX idx_mission_subtasks_mission ON agents.mission_subtasks (mission_id, status);

-- Mission lifecycle / progress audit log — the source for "logs progress" and, via
-- mission_id below, for stitching the knowledge graph to the mission that produced it.
CREATE TABLE agents.mission_events (
    id          TEXT        PRIMARY KEY,
    mission_id  TEXT        NOT NULL REFERENCES agents.missions(id) ON DELETE CASCADE,
    subtask_id  TEXT        REFERENCES agents.mission_subtasks(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type  TEXT        NOT NULL,   -- created|classified|planned|dispatched|gathered|verify_started|verdict|replan|synthesized|reported|escalated
    payload     JSONB       NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_mission_events_mission ON agents.mission_events (mission_id, ts DESC);
CREATE INDEX idx_mission_events_type    ON agents.mission_events (event_type, ts DESC);

-- Tie the knowledge graph to missions: soft-attribute notes + events to the mission
-- that produced them (nullable, no FK — notes/events can exist outside a mission).
ALTER TABLE agents.memory_nodes  ADD COLUMN mission_id TEXT;
ALTER TABLE agents.memory_events ADD COLUMN mission_id TEXT;
CREATE INDEX idx_memory_nodes_mission  ON agents.memory_nodes  (mission_id) WHERE mission_id IS NOT NULL;
CREATE INDEX idx_memory_events_mission ON agents.memory_events (mission_id) WHERE mission_id IS NOT NULL;

-- migrate:down
DROP INDEX IF EXISTS agents.idx_memory_events_mission;
DROP INDEX IF EXISTS agents.idx_memory_nodes_mission;
ALTER TABLE agents.memory_events DROP COLUMN IF EXISTS mission_id;
ALTER TABLE agents.memory_nodes  DROP COLUMN IF EXISTS mission_id;
DROP TABLE IF EXISTS agents.mission_events;
DROP TABLE IF EXISTS agents.mission_subtasks;
DROP TABLE IF EXISTS agents.missions;
