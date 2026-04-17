# mnemon

Persistent memory and knowledge store for AI agents — notes, events, embeddings, and search via MCP.

Named after the memory implants in Iain M. Banks' *Culture* novels.

## What it does

Mnemon gives AI agents (Claude Code, autonomous agents, etc.) durable memory across sessions:

- **Events** — fire-and-forget log of what happened (tool calls, session starts, hook triggers)
- **Notes** — curated knowledge nodes: decisions, insights, checkpoints written intentionally by agents or humans
- **Embeddings** — pgvector-backed semantic search over notes (Ollama `nomic-embed-text`, 768-dim)
- **Entities** — automatic extraction of file paths, `[[wikilinks]]`, and `@mentions` with backlink tracking
- **Temporal edges** — notes within a session are linked chronologically for session replay

## MCP tools

Exposed via [FastMCP](https://github.com/jlowin/fastmcp) as a stdio or HTTP server:

| Tool | Description |
|------|-------------|
| `log_event` | Fire-and-forget event logging |
| `create_note` | Write a curated memory node |
| `query_notes` | Search notes by project + tags |
| `search_similar` | Semantic search using embeddings |
| `query_entity` | Look up an entity and all notes that reference it |
| `recent_events` | Query raw event log by project + time window |
| `query_session` | Retrieve all notes from a session in order |

## Setup

**Requirements:** Python 3.14+, Postgres with pgvector, Ollama (for embeddings)

```bash
# Install with MCP extras
uv sync --extra mcp

# Run migrations
psql $DATABASE_URL -f migrations/001_initial_schema.sql

# Start the MCP server
uv run agent-memory-serve
```

**Environment variables:**

```env
DATABASE_URL=postgresql://user:pass@host/db
# Optional — enables semantic search
OLLAMA_BASE_URL=http://localhost:11434
```

## Register with Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
"agent-memory": {
  "type": "stdio",
  "command": "/path/to/mnemon/.venv/bin/python3",
  "args": ["-m", "agent_memory.server.mcp_server"],
  "env": {
    "PYTHONPATH": "/path/to/mnemon"
  }
}
```

## Schema

Four tables in the `minions` schema:

- `memory_nodes` — curated notes with embeddings and tags
- `memory_events` — raw event log (session lifecycle, tool calls)
- `memory_links` — typed edges between nodes (`temporal`, `causal`, `semantic`)
- `memory_entities` — extracted entities and backlinks to notes

See `migrations/001_initial_schema.sql` for the full schema.

## Part of the mnemon ecosystem

Mnemon is the memory layer for a broader agent infrastructure including an A2A (agent-to-agent) task bus and pixel dashboard. See [claude-agents-tmux](https://github.com/persinac/claude-agents-tmux) for the full setup.
