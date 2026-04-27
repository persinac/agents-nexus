# Troubleshooting

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
