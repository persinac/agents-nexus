# Quickstart

> *"Greetings! I am 127 Guilty Spark. Someone has released the Index!"*

## Prerequisites

- **macOS** with Apple Silicon (Intel works too, just slower)
- **Ollama** — `brew install ollama`
- **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Docker** (optional, only for containerized setup)

## Option A: Native Setup (Recommended)

Best performance on macOS — Ollama runs natively on Apple Silicon.

```bash
cd /path/to/repos/guilty-spark
./quickstart.sh
```

The script will:

1. **Check prerequisites** — verifies `ollama` and `uv` are installed
2. **Start Ollama** — launches the server if not already running
3. **Pull the model** — downloads `nomic-embed-text` (~274MB, one-time)
4. **Install dependencies** — `uv sync` in the guilty-spark directory
5. **Build the Index** — chunks and embeds all 331 repos (takes a few hours)
6. **Install global CLI** — creates `/usr/local/bin/spark` (may prompt for sudo)
7. **Configure Claude Code** — adds guilty-spark as an MCP server in `~/.claude/claude_code_config.json`
8. **Install slash command** — creates `/spark` command in `~/.claude/commands/spark.md`

### After Setup

```bash
# Verify it worked
spark status

# Try a search
spark query "EKS cluster provisioning"

# Open Claude Code and use the slash command
# /spark which terraform repo manages S3 buckets?
```

## Option B: Docker Setup

Everything runs in containers. Note: Ollama in Docker does **not** use Apple Silicon GPU, so embedding is slower (~3-5x).

```bash
cd /path/to/repos/guilty-spark
./quickstart-docker.sh
```

This will:

1. Build the `guilty-spark` Docker image
2. Start Ollama and Spark containers via Docker Compose
3. Pull the embedding model inside the Ollama container
4. Build the full index
5. Install a global `spark` CLI that delegates to the container

### Managing Docker Services

```bash
# View logs
docker compose logs -f spark

# Stop everything
docker compose down

# Restart
docker compose up -d

# Rebuild after code changes
docker compose up -d --build
```

## Verifying the Installation

### 1. Check index status

```bash
spark status
```

Expected output:
```
[127 Guilty Spark] Index Status
  Location: /path/to/guilty-spark/data/the-index
  Model: ollama/nomic-embed-text
  Installations: 331
  File chunks: ~15000
  Total chunks: ~15331
```

### 2. Test a search

```bash
spark query "chatbot handoff to zendesk"
```

### 3. Test Claude Code integration

Start Claude Code from your repos directory and run:

```
/spark how does the claims adjudication pipeline work?
```

Claude will call the `spark` MCP tool, get ranked results, and summarize the relevant repos for you.

## Keeping the Index Fresh

### Manual

```bash
# Re-index a single repo after making changes
spark activate example-service

# Full rebuild (after large-scale changes like repo reorganization)
spark reclaim
```

### Automatic (Webhook)

The GitLab webhook receiver auto-reindexes repos on merge to main and triggers decision synthesis if enabled. See the webhook setup guide at [docs/gitlab-webhook-setup.md](docs/gitlab-webhook-setup.md).

### Existing indexes are forward-compatible

When new fields are added to the index schema (such as the `decision_date`, `decision_author`, and `mr_url` columns added in the decision synthesis feature), they are sparse — existing chunks receive empty-string defaults. A full `spark reclaim` is **not** required after upgrading. New columns are added automatically on the next write.

**Exception — symbol chunking:** Enabling `symbol_chunking_enabled` (introduced in the tree-sitter symbol chunking feature) changes chunk IDs and chunk granularity. Existing window-based file chunks are not automatically replaced. Run `spark reclaim` after enabling this feature to rebuild the index with symbol-level chunks.

## Switching Embedding Models

Edit `config.yaml`:

```yaml
# Local (free, fast on Apple Silicon)
embedding_model: ollama/nomic-embed-text

# Voyage AI (best code embeddings, needs API key)
# export VOYAGE_API_KEY=your-key
embedding_model: voyage/voyage-code-3

# OpenAI (needs API key)
# export OPENAI_API_KEY=your-key
embedding_model: text-embedding-3-small
```

After changing models, rebuild the full index:

```bash
spark reclaim
```

## Troubleshooting

### "Ollama not responding"

```bash
# Check if Ollama is running
curl http://localhost:11434/api/tags

# Start it manually
ollama serve
```

### "The Index has not been constructed yet"

```bash
spark reclaim
```

### "Model not found"

```bash
ollama pull nomic-embed-text
```

### Claude Code doesn't see the MCP tools

Check `~/.claude/claude_code_config.json` has the guilty-spark entry:

```json
{
  "mcpServers": {
    "guilty-spark": {
      "command": "/usr/local/bin/spark",
      "args": ["serve"]
    }
  }
}
```

Then restart Claude Code.

### Slow embeddings in Docker

This is expected — Ollama in Docker can't use Apple Silicon GPU. Use the native setup (`quickstart.sh`) instead for ~3-5x faster indexing.
