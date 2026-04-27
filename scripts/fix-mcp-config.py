#!/usr/bin/env python3
"""One-off: add MCP servers to ~/.claude.json.

Reads DATABASE_URL from agents-nexus/.env so the agent-memory
MCP server can connect to Postgres.
"""

import json
import os
from pathlib import Path

home = Path.home()
nexus_dir = home / "repos" / "agents-nexus"

# Read DATABASE_URL from .env
db_url = ""
env_file = nexus_dir / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL=") and not line.startswith("#"):
            db_url = line.split("=", 1)[1].strip()
            break

if not db_url:
    print("WARNING: DATABASE_URL not found in .env — agent-memory will fail to connect")

claude_json = home / ".claude.json"
cfg = json.loads(claude_json.read_text())

cfg["mcpServers"] = {
    "spark": {
        "type": "sse",
        "url": "http://localhost:8343/sse",
    },
    "agent-memory": {
        "command": str(home / "repos/agents-nexus/mnemon/.venv/bin/python3"),
        "args": ["-m", "agent_memory.server.mcp_server"],
        "cwd": str(nexus_dir / "mnemon"),
        "env": {
            "DATABASE_URL": db_url,
        },
    },
}

claude_json.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"Done — mcpServers added to ~/.claude.json (DATABASE_URL: {'set' if db_url else 'MISSING'})")
