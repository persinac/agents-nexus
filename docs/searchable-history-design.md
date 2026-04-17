# Searchable History & Agent Memory — Design

**Date:** 2026-03-29
**Status:** Design
**IDEAS.md item:** #11 (Searchable history)
**Related:** [minions tiered-memory-implementation-plan](https://github.com/persinac/minions-suite/blob/main/notes/tiered-memory-implementation-plan.md), [minions distributed-memory-knowledge-graphs](https://github.com/persinac/minions-suite/blob/main/notes/distributed-memory-knowledge-graphs.md)

---

## Problem

Agent history today lives in two places that can't answer the questions we actually ask:

| Source | What it captures | What's missing |
|--------|-----------------|----------------|
| `~/.tmux/apm.log` | Event type + timestamp + window slot | No "what" or "why", auto-pruned after 24h |
| `~/.claude/projects/*/session.jsonl` | Full transcript (every message, tool call, result) | Massive, noisy, not queryable across sessions, no agent identity metadata |

**The gap:** There's no session-level summary layer that captures: which agent, which repo, which branch, what it worked on, what files it touched, what the outcome was — queryable across devices and over time.

### Target queries

- "What was recently changed in storefront-api?"
- "When did we add X to project Y?"
- "Which agent worked on Z?"
- Agents querying this themselves for context before starting work

---

## Architecture

### Central store, multi-device

All devices (Windows, Mac, K8s pods) write to a single Postgres instance, reachable via Cloudflare tunnel.

```
┌─ Windows ─────────────────────────┐     ┌─ Mac ──────────────────────────┐
│                                    │     │                                │
│  tmux hooks ──► local wrapper ─────┼─┐   │  tmux hooks ──► local wrapper ──┼─┐
│  agents ──► MCP (via tunnel) ──────┼─┤   │  agents ──► MCP (via tunnel) ──┼─┤
│                                    │ │   │                                │ │
└────────────────────────────────────┘ │   └────────────────────────────────┘ │
                                       │                                     │
                              ┌────────┴─────────────────────────────────────┘
                              │
                              ▼
                     ┌─────────────────┐
                     │  CF Tunnel       │
                     └────────┬────────┘
                              │
                     ┌────────┴────────┐
                     │  MCP Server +   │
                     │  HTTP ingest    │
                     │  (K8s pod)      │
                     └────────┬────────┘
                              │
                     ┌────────┴────────┐
                     │  Postgres       │
                     │  (+ pgvector)   │
                     └─────────────────┘
```

### Hybrid communication

- **Hooks → HTTP ingest** — fire-and-forget event logging. Local wrapper buffers if tunnel is down.
- **Agents → MCP server** — interactive queries. Claude Code connects to the MCP server as a tool provider.
- **Minions agents → direct import** — `agent-memory` Python package used in-process.

### Two data streams: raw events + curated notes

Separate tables, separate indexes, linked via `source_event_id`.

**Raw events** (`memory_events`) — high-volume, machine-generated, append-only:
- Auto-captured by tmux hooks (session start/stop, tool use, permission waits)
- Every agent action gets a row
- Indexed for time-range + project + device queries
- Retention: configurable (weeks/months)

**Curated notes** (`memory_nodes`) — low-volume, meaningful, agent/human-created:
- Agents decide what's noteworthy (Zettelkasten-style)
- Humans create via `/checkpoint` or a new `/note` command
- Tagged, linked, embeddable
- Retention: indefinite

```
Events:  "show me everything that happened in storefront-api last Tuesday"
         → time range + project filter, aggregate/group

Notes:   "what do we know about the auth module?"
         → semantic search, graph traversal, entity backlinks
```

---

## Schema (Phase 0-1)

### memory_events

```sql
CREATE TABLE memory_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device TEXT NOT NULL,              -- 'windows-desktop', 'mac-laptop', 'k8s-pod-xyz'
    project TEXT NOT NULL,             -- repo/project name
    repo TEXT,                         -- full repo path or URL
    branch TEXT,
    agent_slot TEXT,                   -- tmux window index or minions agent ID
    session_id TEXT,                   -- Claude session ID or minions job ID
    event_type TEXT NOT NULL,          -- 'session_start', 'session_end', 'tool_use',
                                       -- 'permission_wait', 'file_read', 'file_write',
                                       -- 'commit', 'error', 'idle'
    payload JSONB DEFAULT '{}'         -- event-specific data (tool name, file paths,
                                       -- commit SHA, error message, etc.)
);

CREATE INDEX idx_events_project_time ON memory_events (project, timestamp DESC);
CREATE INDEX idx_events_device ON memory_events (device, timestamp DESC);
CREATE INDEX idx_events_session ON memory_events (session_id);
CREATE INDEX idx_events_type ON memory_events (event_type, timestamp DESC);
CREATE INDEX idx_events_payload ON memory_events USING GIN (payload);
```

### memory_nodes

```sql
CREATE TABLE memory_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content TEXT NOT NULL,
    title TEXT,                           -- short summary
    tags TEXT[] DEFAULT '{}',             -- #auth #jwt #constraint
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1536),              -- pgvector (Phase 11d)
    attributes JSONB DEFAULT '{}',
    source_event_id UUID REFERENCES memory_events(id),
    source_agent_role TEXT,              -- 'human', 'backend_engineer', 'code_reviewer'
    source_device TEXT,
    project TEXT NOT NULL,
    access_count INT DEFAULT 0,
    last_accessed TIMESTAMPTZ
);

CREATE INDEX idx_nodes_project ON memory_nodes (project, created_at DESC);
CREATE INDEX idx_nodes_tags ON memory_nodes USING GIN (tags);
-- Phase 11d: CREATE INDEX idx_nodes_embedding ON memory_nodes USING ivfflat (embedding vector_cosine_ops);
```

### memory_entities

```sql
CREATE TABLE memory_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,                  -- 'auth-module', 'users-table', 'storefront-api'
    entity_type TEXT,                    -- 'file', 'module', 'repo', 'api_endpoint', 'branch'
    project TEXT NOT NULL,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    attributes JSONB DEFAULT '{}',
    UNIQUE (name, project)
);
```

### memory_links

```sql
CREATE TABLE memory_links (
    from_node UUID REFERENCES memory_nodes(id) ON DELETE CASCADE,
    to_entity TEXT NOT NULL,             -- entity name (bidirectional via backlink queries)
    link_type TEXT DEFAULT 'reference',  -- 'reference', 'causal', 'temporal', 'entity'
    confidence FLOAT DEFAULT 1.0,
    reasoning TEXT,                      -- for causal links: why this connection exists
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (from_node, to_entity, link_type)
);

CREATE INDEX idx_links_entity ON memory_links (to_entity);
CREATE INDEX idx_links_type ON memory_links (link_type);
```

---

## Phased Roadmap

### 11a — Central Postgres schema + MCP server (Priority: 1)

- Create `memory_events`, `memory_nodes`, `memory_entities`, `memory_links` tables
- Thin MCP server (FastMCP) exposing:
  - `log_event(device, project, event_type, ...)` — write to memory_events
  - `create_note(content, tags, links, project, ...)` — write to memory_nodes + memory_links
  - `query_events(project?, time_range?, event_type?, limit?)` — read memory_events
  - `query_notes(project?, tags?, search_text?, limit?)` — read memory_nodes
- HTTP ingest endpoint (POST /events) for hook scripts
- Deploy alongside existing minions infra (K8s pod, same Postgres)
- Shared `agent-memory` Python package from minions-suite monorepo

### 11b — Local wrapper + CF tunnel + tmux hook integration (Priority: 1)

- Local wrapper script that buffers events and forwards to central HTTP endpoint
  - Write-ahead to local file if tunnel is unreachable, flush on reconnect
- CF tunnel configuration for the memory MCP server + HTTP ingest
- Update tmux hooks (`hook-stop.sh`, `hook-pretooluse.sh`, `hook-notification.sh`) to POST events
- Auto-capture: session start/end, tool use counts, permission waits, files touched
- Extract project/repo/branch from hook context (already available in hook JSON)

### 11c — Entity extraction + backlinks (Priority: 2)

- On note creation: extract entity references from content (file paths, module names, repo names)
- Auto-create `memory_entities` entries and `memory_links`
- Backlink queries: "everything that references auth-module" traverses `memory_links`
- MCP tool: `query_entity(name, project?)` — returns the entity + all backlinked notes
- Tag taxonomy: domain tags (#auth, #database), action tags (#discovery, #decision, #bug), outcome tags (#merged, #failed)

### 11d — pgvector embeddings + semantic search (Priority: 3)

- Embed note content on creation (via LiteLLM or direct API)
- Semantic search MCP tool: `search_similar(query_text, project?, limit?)`
- MAGMA-style anchor identification via Reciprocal Rank Fusion
- Index: ivfflat on embedding column

### 11e — Temporal edges + causal inference (Priority: 4)

- Auto-create temporal edges between events/notes in the same session
- Slow-path causal inference: LLM batch job over 2-hop neighborhoods
  - "Why did this change happen?" → infer causal links
  - Run on job/session completion
  - Anthropic Message Batches API for cost efficiency
- Causal backtrace queries: follow the "why" chain backward from any node

### 11f — Knowledge graph web UI (Priority: 5)

- Interactive web visualization (like pixel dashboard, but for the knowledge graph)
- Orthogonal views: switch between temporal, causal, semantic, entity lenses
- Click an entity → see all connected notes across all dimensions
- Filter by project, time range, tags, device
- Could extend pixel-dashboard or be a standalone app
- WebSocket for live updates as new events/notes arrive

### 11g — Auto-inject knowledge into agent prompts (Priority: 5)

- `/recall` slash command for Claude Code agents to manually query the knowledge store
- Automatic context injection: on session start, query relevant notes for the current project
  - MAGMA-style retrieval: embed the task description, find anchors, traverse graphs
  - Inject as "Prior Knowledge" section in prompt
  - Token-budgeted (configurable, default ~2000 tokens)
- Obsidian-style formatting: tags, wikilinks, local graph view
- Works for both tmux agents and minions agents

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Raw events vs curated notes | Separate tables | Different volume, retention, query patterns. Linked via `source_event_id` |
| Central vs local store | Central Postgres | Multi-device requirement. Same DB already deployed for minions |
| Hook → store communication | HTTP ingest via local wrapper | Fire-and-forget, buffer on disconnect, don't block hooks |
| Agent → store communication | MCP server via CF tunnel | Proper tool semantics for interactive queries |
| Shared package | `agent-memory` from minions monorepo | One implementation, two consumers (minions + tmux agents) |
| Graph DB | Postgres + pgvector (no AGE initially) | Simpler. Recursive CTEs handle graph traversal. AGE can be added later if needed |
| Embeddings | Deferred to Phase 11d | Phase 0-1 works with text search + tags. Embeddings are additive, not blocking |

---

## Open Questions

- **Event retention policy** — how long to keep raw events? Weeks? Months? Indefinite with partitioning?
- **Device identity** — how to name devices consistently? Hostname? Manual config?
- **Auth on the tunnel** — CF Access policy? API key in the local wrapper?
- **Minions integration order** — implement `agent-memory` package first (for minions), then wire the MCP server? Or build the MCP server standalone first for tmux agents?
