#!/usr/bin/env python3
"""V1 spike — does the Claude Agent SDK route through the local litellm/nexus proxy?

This is the kill-switch validation for the SDK harness migration. The whole
observability story is ANTHROPIC_BASE_URL -> localhost:4000 (litellm) -> Langfuse,
with per-agent trace naming via a `sess/<name>/` path prefix (see proxy/main.py).

The SDK spawns the `claude` binary under the hood, so it *should* inherit the
base URL like the CLI does. But two unknowns make this worth testing empirically:
  1. Does the SDK-spawned `claude` load ~/.claude/settings.json's `env` block?
     That block currently pins ANTHROPIC_BASE_URL=https://api.anthropic.com, which
     would clobber our proxy URL. Recent SDKs default to NOT loading filesystem
     settings unless `setting_sources` is set — but we verify, not assume.
  2. Does OAuth creds (~/.claude/.credentials.json) auth cleanly through the proxy?

Run:  uv run --python .venv/bin/python spike_proxy_test.py
Then verify:  docker logs nexus-proxy --since <printed START epoch>
"""

import anyio
import os
import sys
import time

SESSION = os.environ.get("SPIKE_SESSION", "spike-sdk-proxy")
PROXY = os.environ.get("SPIKE_PROXY_BASE", "http://localhost:4000")
# "" (default) -> pass setting_sources=[] so the SDK does NOT load ~/.claude/settings.json,
# whose env block pins ANTHROPIC_BASE_URL=api.anthropic.com and clobbers our proxy URL.
# Set SPIKE_LOAD_SETTINGS=1 to reproduce the default-bypass behavior.
LOAD_SETTINGS = os.environ.get("SPIKE_LOAD_SETTINGS", "") == "1"
BASE_URL = f"{PROXY.rstrip('/')}/sess/{SESSION}"

# Force the proxy base URL into the child process env. If the SDK loads
# settings.json's env on top of this and clobbers it, the proxy logs will be
# empty and we'll know (and fall back to ClaudeAgentOptions(env=...) / setting_sources).
os.environ["ANTHROPIC_BASE_URL"] = BASE_URL

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock


async def main() -> int:
    start = int(time.time())
    print(f"[spike] START epoch={start}  (docker logs nexus-proxy --since {start})")
    print(f"[spike] ANTHROPIC_BASE_URL={os.environ['ANTHROPIC_BASE_URL']}")
    print(f"[spike] claude-agent-sdk version check:")
    try:
        import claude_agent_sdk as _s
        print(f"[spike]   claude_agent_sdk {getattr(_s, '__version__', '?')}")
    except Exception as e:
        print(f"[spike]   (version introspection failed: {e})")

    opt_kwargs = dict(
        model="claude-haiku-4-5-20251001",   # cheap model — we only need a trace with cost
        max_turns=1,
        allowed_tools=[],                     # no tools; pure text turn
        permission_mode="bypassPermissions",  # nothing to approve anyway
    )
    if not LOAD_SETTINGS:
        opt_kwargs["setting_sources"] = []    # don't load settings.json -> no env clobber
    print(f"[spike] setting_sources={'<default: loads settings.json>' if LOAD_SETTINGS else '[]'}")
    options = ClaudeAgentOptions(**opt_kwargs)

    result: ResultMessage | None = None
    text_seen = []
    try:
        async for message in query(
            prompt="Reply with exactly this token and nothing else: PROXY_OK",
            options=options,
        ):
            cls = type(message).__name__
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_seen.append(block.text)
                        print(f"[spike] assistant: {block.text!r}")
            elif isinstance(message, ResultMessage):
                result = message
                print(f"[spike] ResultMessage: subtype={message.subtype} "
                      f"session_id={message.session_id} "
                      f"cost_usd={getattr(message, 'total_cost_usd', None)} "
                      f"usage={getattr(message, 'usage', None)}")
            else:
                print(f"[spike] ({cls})")
    except Exception as e:
        print(f"[spike] ERROR during query: {type(e).__name__}: {e}")
        return 2

    print("\n[spike] ── summary ──")
    print(f"[spike] text: {' '.join(text_seen).strip()!r}")
    if result is not None:
        print(f"[spike] cost_usd={getattr(result, 'total_cost_usd', None)}")
    print(f"[spike] Now verify passthrough:")
    print(f"[spike]   docker logs nexus-proxy --since {start} 2>&1 | grep sess/{SESSION}")
    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
