#!/usr/bin/env python3
"""One-off: add MCP servers to ~/.claude.json"""

import json
from pathlib import Path

claude_json = Path.home() / ".claude.json"
cfg = json.loads(claude_json.read_text())

cfg["mcpServers"] = {
    "spark": {
        "type": "sse",
        "url": "http://localhost:8343/sse",
    },
    "agent-memory": {
        "command": str(Path.home() / "repos/agents-nexus/mnemon/.venv/bin/python3"),
        "args": ["-m", "agent_memory.server.mcp_server"],
        "cwd": str(Path.home() / "repos/agents-nexus/mnemon"),
    },
}

claude_json.write_text(json.dumps(cfg, indent=2) + "\n")
print("Done — mcpServers added to ~/.claude.json")
