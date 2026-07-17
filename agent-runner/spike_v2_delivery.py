#!/usr/bin/env python3
"""V2 spike — idle-gated inbox delivery WITHOUT tmux send-keys.

Proves the bus's existing delivery semantics map onto the SDK: a message pushed
into a per-agent inbox *mid-turn* is NOT injected mid-turn (the SDK can't do that
anyway) but is consumed at the *next turn boundary*. This replaces the whole
send-keys + @waiting screen-scrape + settle-delay-before-Enter machinery.

Harness loop = query -> drain receive_response (turn runs) -> idle -> read inbox
-> query next. A message that arrives during a turn just waits in the inbox file.

Sequence:
  - launch an EXTERNAL writer process (stands in for the slack-bridge) that appends
    a message to the inbox ~2s in — i.e. MID-TURN-A.
  - TURN A is deliberately long (~6s: model runs `sleep 6` via Bash).
  - after TURN A's ResultMessage (idle), read the inbox; the message is waiting.
  - deliver it as TURN B; confirm it's processed.
  - verdict compares written-offset vs turn-A-end vs delivered-offset.

Run: .venv/bin/python spike_v2_delivery.py
"""
import asyncio
import os
import sys
import time

SESSION = "spike-v2-delivery"
os.environ["ANTHROPIC_BASE_URL"] = f"http://localhost:4000/sess/{SESSION}"  # keep proxy+Langfuse

import anyio
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, UserMessage, ResultMessage,
    TextBlock, ToolUseBlock, ToolResultBlock,
)

HERE = os.path.dirname(os.path.abspath(__file__))
WRITER = os.path.join(HERE, "spike_inbox_writer.py")
INBOX = f"/tmp/spike-inbox-{SESSION}.txt"
PYEXE = sys.executable


async def drain_turn(client, label, t0) -> float:
    """Consume one turn's messages until (incl.) ResultMessage. Return its wall time."""
    async for msg in client.receive_response():
        el = time.time() - t0
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    print(f"[{label}] +{el:4.1f}s assistant: {b.text.strip()!r}")
                elif isinstance(b, ToolUseBlock):
                    print(f"[{label}] +{el:4.1f}s tool_use: {b.name}({b.input})")
        elif isinstance(msg, UserMessage):
            for b in getattr(msg, "content", []) or []:
                if isinstance(b, ToolResultBlock):
                    print(f"[{label}] +{el:4.1f}s tool_result received")
        elif isinstance(msg, ResultMessage):
            print(f"[{label}] +{el:4.1f}s RESULT subtype={msg.subtype} "
                  f"cost={getattr(msg, 'total_cost_usd', None)}")
            return time.time()
    return time.time()


async def main() -> int:
    # fresh inbox
    try:
        os.remove(INBOX)
    except FileNotFoundError:
        pass
    open(INBOX, "a").close()

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        allowed_tools=["Bash"],
        permission_mode="bypassPermissions",
        setting_sources=[],       # route through proxy; don't load settings.json hooks
        max_turns=6,
    )

    t0 = time.time()
    async with ClaudeSDKClient(options=options) as client:
        # External delivery MID-TURN: writer lands the message ~2s in.
        writer = await asyncio.create_subprocess_exec(
            PYEXE, WRITER, INBOX, "2.0",
            "Reply in 6 words or fewer: what shell command did you just run?",
        )
        print(f"[v2] +{time.time()-t0:4.1f}s launched external inbox writer (delivers at +2s, mid-turn)")

        # TURN A — long turn so the mid-turn delivery is unambiguous.
        print(f"[v2] +{time.time()-t0:4.1f}s query TURN A")
        await client.query(
            "Use the Bash tool to run exactly this command: sleep 6. "
            "After it finishes, reply with exactly: DONE_A"
        )
        tA = await drain_turn(client, "A", t0)
        await writer.wait()

        # IDLE: now read the inbox. Message should be waiting (written mid-turn).
        with open(INBOX) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        print(f"\n[v2] === IDLE after TURN A (+{tA-t0:4.1f}s) — inbox holds {len(lines)} buffered msg(s) ===")

        verdicts = []
        for ln in lines:
            wt_s, _, text = ln.partition("\t")
            wt_off = float(wt_s) - t0
            dl_off = time.time() - t0
            print(f"[v2] delivering buffered msg (written +{wt_off:4.1f}s, now +{dl_off:4.1f}s): {text!r}")
            await client.query(text)          # TURN B — deliver at the boundary
            await drain_turn(client, "B", t0)
            verdicts.append((wt_off, tA - t0, dl_off))

    print("\n[v2] ── verdict ──")
    ok = bool(verdicts)
    for wt_off, ta_off, dl_off in verdicts:
        gated = (wt_off < ta_off) and (dl_off >= ta_off - 0.5)
        ok = ok and gated
        print(f"[v2] written +{wt_off:.1f}s  <  turnA-end +{ta_off:.1f}s  <=  delivered +{dl_off:.1f}s   "
              f"=> {'GATED to turn boundary ✅' if gated else 'NOT gated ❌'}")
    print(f"[v2] V2 { 'PASS ✅' if ok else 'FAIL ❌' } — inbox delivery is idle-gated, no send-keys")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
