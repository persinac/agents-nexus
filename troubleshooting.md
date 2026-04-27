# Troubleshooting

## Langfuse traces not appearing from mnemon MCP server

**Status:** Open — Langfuse initializes successfully (log shows "Langfuse tracing enabled") but no traces appear in the UI.

**What's been tried:**
- Env vars confirmed present in container: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
- Changed `LANGFUSE_HOST` from Tailscale IP (`http://100.75.154.84:3000`) to Docker-internal (`http://langfuse-web:3000`) — still no traces
- `_lf_trace()` calls fire inside each tool function (replaced `@observe` decorator which broke FastMCP schema introspection)
- Langfuse UI loads and works at `http://100.75.154.84:3000`
- Python 3.14 + Pydantic v1 warning from langfuse — may cause subtle issues

**Next steps to try:**
1. Check if langfuse-web and mnemon-mcp are on the same Docker network: `docker network inspect agents-nexus_default`
2. Test connectivity from inside the container: `docker compose exec mnemon-mcp curl -s http://langfuse-web:3000/api/public/health`
3. Check if the `_lf_trace()` context manager approach actually flushes (zero-duration spans may be dropped) — may need to switch to explicit `client.trace()` API
4. Try downgrading Dockerfile from Python 3.14 to 3.12 to avoid the Pydantic v1 incompatibility
5. Add `LANGFUSE_DEBUG=true` env var to get verbose SDK logging

---

## MCP agent-memory server fails to connect

**Symptom:** `/mcp` shows "Failed to reconnect to agent-memory."

**Cause:** The `agent_memory` package wasn't installed in the mnemon venv. The `pyproject.toml` was missing a `[build-system]` section, and setuptools couldn't auto-discover the package because `migrations/` sat alongside `agent_memory/` as a second top-level directory (flat-layout ambiguity).

**Fix:**

1. Add build-system config to `mnemon/pyproject.toml`:
   ```toml
   [build-system]
   requires = ["setuptools>=68.0"]
   build-backend = "setuptools.build_meta"

   [tool.setuptools.packages.find]
   include = ["agent_memory*"]
   ```

2. Install the package in editable mode with MCP extras:
   ```bash
   cd /home/persinac/repos/agents-nexus/mnemon
   uv pip install -e '.[mcp]' --python .venv/bin/python3
   ```

3. Verify the module imports:
   ```bash
   .venv/bin/python3 -c "from agent_memory.server.mcp_server import main; print('OK')"
   ```

4. Restart Claude Code so it re-establishes the MCP connection.
