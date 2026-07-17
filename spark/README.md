# 127 Guilty Spark

> *"I am 127 Guilty Spark, the Monitor of Installation 04. I can show you where the Index is."*

AI-powered semantic search index for your repositories. Chunks, embeds, and indexes every installation (repo) so you can query them instantly — from the CLI, Claude Code, or any MCP-compatible agent.

## What It Does

- **Indexes** every repo by reading READMEs, CLAUDE.md, config files, and source code
- **Embeds** content using a vendor-agnostic model (default: `nomic-embed-text` via Ollama)
- **Stores** vectors in LanceDB (local, zero-config, no server needed)
- **Serves** results via FastMCP so Claude Code can search repos mid-conversation

## Naming Convention

| Concept | Halo Name |
|---|---|
| This project | **guilty-spark** |
| The vector index | **the-index** |
| Individual repos | **installations** |
| The MCP search tool | **spark** |
| Full index rebuild | **reclaim** |
| Single repo re-index | **activate** |
| Incremental delta re-index | **sync** |
| Repo summaries | **monitor-logs** |
| A search query | **querying the installation** |

## Quick Start

```bash
# Native macOS (recommended — uses Apple Silicon for embeddings)
./quickstart.sh

# Or fully containerized
./quickstart-docker.sh
```

See [QUICKSTART.md](QUICKSTART.md) for detailed setup instructions.

## Usage

### CLI

```bash
# Semantic search across all repos
spark query "EKS cluster provisioning"

# Filter by team
spark query "ingestion monitoring" --team "Client"

# Filter by chunk type (summary = repo overviews, file = source code)
spark query "terraform VPC" --type summary

# View index stats
spark status

# Re-index a single repo after changes
spark activate example-service

# Incremental: re-index only repos whose origin/HEAD has moved since last run.
# This is what the nightly cron uses. First run is a full reclaim (no prior
# metadata); steady state runs in seconds-to-minutes.
spark sync
spark sync --dry-run        # classify only, no writes

# Full rebuild — use after a schema migration or to recover a wiped index
spark reclaim

# Synthesize decision records from historical MRs (requires GitLab + decisions_enabled: true)
spark synthesize --all --days 180
spark synthesize --repo example-service
spark synthesize --team "Platform - Infrastructure" --days 90
```

### Claude Code (slash command)

```
/spark which terraform repo manages the VPC?
/spark how does example-service handle zendesk handoffs?
/spark list all Platform - Infrastructure repos
```

### Claude Code (MCP tools — used automatically)

When configured as an MCP server, Claude Code gains these tools:

- **`spark`** — Semantic search with optional team/type filters
- **`spark_deep`** — Two-stage search: finds the right repos, then digs into their files
- **`list_installations`** — List all indexed repos, optionally by team
- **`installation_summary`** — Full monitor-log for a specific repo
- **`recent_changes`** — Recent merged MRs for a specific repo
- **`search_decisions`** — Search synthesized decision records; use for "why" questions

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Claude Code │────>│  FastMCP      │────>│   LanceDB    │
│  (you chat)  │<────│  Server       │<────│  (the-index) │
└─────────────┘     └──────────────┘     └──────────────┘
                           ^
                    ┌──────┴───────┐
                    │   Indexer    │
                    │  chunker    │
                    │  + LiteLLM  │
                    └──────────────┘
```

### Chunking Strategy

- **Layer 1 — Monitor logs**: One summary chunk per repo (README, CLAUDE.md, pyproject.toml, main.tf, etc.)
- **Layer 2 — File-level**: Key source files indexed individually with repo/team metadata

### Tech Stack

| Component | Technology |
|---|---|
| Vector store | LanceDB (embedded, file-based) |
| Embeddings | LiteLLM (vendor-agnostic) → Ollama/nomic-embed-text |
| MCP server | FastMCP |
| CLI | Click |
| Package manager | uv |

## Configuration

Edit `config.yaml` to customize:

- **`embedding_model`** — Switch models by changing one line (e.g., `voyage/voyage-code-3` for API-based)
- **`teams`** — Maps directory prefixes to team names
- **`include_patterns`** — File globs to index
- **`exclude_dirs`** — Directories to skip
- **`max_file_size`** — Skip files larger than this (default 32KB)
- **`max_files_per_installation`** — Cap per repo (default 100)
- **`chat_model`** — LLM for decision synthesis (default `ollama/llama3.2`; any LiteLLM model works)
- **`decisions_enabled`** — Enable auto-synthesis of decision records on MR merge (default `false`)
- **`symbol_chunking_enabled`** — Use AST-aware symbol chunks instead of blind sliding windows (default `true`). Requires `spark reclaim` after changing.

Environment overrides for Docker:
- `SPARK_INSTALLATIONS_PATH` — Override repos directory
- `SPARK_INDEX_PATH` — Override index location
- `SPARK_EMBEDDING_MODEL` — Override model
- `OLLAMA_API_BASE` — Point to remote/containerized Ollama

## Project Structure

```
guilty-spark/
├── quickstart.sh              # Native macOS setup
├── quickstart-docker.sh       # Docker setup
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── config.yaml
├── data/the-index/            # LanceDB vector store (gitignored)
└── src/spark/
    ├── cli.py                 # CLI entry point
    ├── config.py              # Config loader
    ├── indexer/
    │   ├── chunker.py         # Chunk generation (summaries + files)
    │   ├── embedder.py        # LiteLLM embedding wrapper
    │   └── builder.py         # Index build/update orchestration
    └── server/
        └── mcp_server.py      # FastMCP server
```

## Roadmap

- **Phase 2**: GitLab webhook receiver — auto-reindex on merge to main
- **Reranking**: BGE-Reranker for improved precision on similar repos (e.g., 55 tf-* modules)
- ~~**Layer 3 chunking**~~: Tree-sitter symbol-level parsing — shipped
- **Model upgrade path**: BGE-M3 hybrid search (dense + sparse + ColBERT)
