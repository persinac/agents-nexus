#!/usr/bin/env python3
"""Does a hermetic SDK session keep memory (mnemon) + spark MCP — and still proxy?

The SDK is hermetic by default (setting_sources=[] loads NO filesystem config), so
you don't inherit MCP/plugins/hooks unless you opt in. This proves the "explicit"
path: pass the exact server configs (agent-memory from ~/.claude.json, spark from
.mcp.json) via mcp_servers and confirm they connect — no shell hooks, no env clobber.
"""
import os
SESSION = "spike-mcp-probe"
os.environ["ANTHROPIC_BASE_URL"] = f"http://localhost:4000/sess/{SESSION}"

import anyio
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, SystemMessage, ResultMessage

REPO = os.environ.get("AGENTS_NEXUS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP = {
    "agent-memory": {
        "type": "stdio",
        "command": f"{REPO}/mnemon/.venv/bin/python3",
        "args": ["-m", "agent_memory.server.mcp_server"],
        "env": {"PYTHONPATH": f"{REPO}/mnemon"},
    },
    "spark": {"type": "sse", "url": "http://localhost:8343/sse"},
}


async def main():
    opts = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        setting_sources=[],                 # hermetic — nothing from disk
        mcp_servers=MCP,                     # ...except what we hand it
        allowed_tools=["mcp__agent-memory__search_similar", "mcp__spark__spark"],
        permission_mode="bypassPermissions",
        max_turns=1,
        cwd=REPO,
    )
    async with ClaudeSDKClient(options=opts) as client:
        await client.query("Reply with exactly: MCP_PROBE_OK")
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage) and getattr(msg, "subtype", "") == "init":
                data = msg.data or {}
                print("[probe] init mcp_servers:", data.get("mcp_servers"))
                tools = data.get("tools", [])
                mcp_tools = [t for t in tools if str(t).startswith("mcp__")]
                print(f"[probe] total tools: {len(tools)}; MCP tools exposed: {mcp_tools}")
            elif isinstance(msg, ResultMessage):
                print(f"[probe] RESULT {msg.subtype} cost={getattr(msg,'total_cost_usd',None)}")
        try:
            print("[probe] get_mcp_status():", await client.get_mcp_status())
        except Exception as e:
            print("[probe] get_mcp_status err:", type(e).__name__, e)


if __name__ == "__main__":
    anyio.run(main)
