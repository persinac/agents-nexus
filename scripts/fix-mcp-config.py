#!/usr/bin/env python3
"""One-off: add MCP servers to ~/.claude.json.

agent-memory runs as an always-on SSE service via Docker Compose.
"""

import json
from pathlib import Path

home = Path.home()

claude_json = home / ".claude.json"
cfg = json.loads(claude_json.read_text())

cfg["mcpServers"] = {
    "agent-memory": {
        "type": "sse",
        "url": "http://localhost:8330/sse",
    },
}

claude_json.write_text(json.dumps(cfg, indent=2) + "\n")
print("Done — mcpServers added to ~/.claude.json (agent-memory:8330)")
