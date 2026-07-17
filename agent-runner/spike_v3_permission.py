#!/usr/bin/env python3
"""V3 spike — `can_use_tool` replaces the notify-classify permission hook.

Today: a Notification hook fires on a permission prompt, an out-of-process litellm
classifier categorizes the pending tool, and the harness either send-keys a `1`
(auto-approve read-only) or Slack-notifies a human, whose yes/no is mapped back to
a digit and send-key'd in. Fragile: exit-code signalling + screen-scrape + digit
injection into a TUI menu.

With the SDK it's one async callback:
  - read-only tool           -> PermissionResultAllow() inline (auto-approve)
  - mutating tool            -> "ask": write an out-of-band request and AWAIT an
                                external approver's decision (the Slack bus, minus
                                send-keys); fail-safe to deny on timeout.

Filesystem canaries make the outcome checkable: the approved touch must exist, the
denied touch must NOT (proving deny actually blocks execution).

Run: .venv/bin/python spike_v3_permission.py
"""
import asyncio
import os
import sys
import time

SESSION = "spike-v3-permission"
os.environ["ANTHROPIC_BASE_URL"] = f"http://localhost:4000/sess/{SESSION}"  # keep proxy+Langfuse

import anyio
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    PermissionResultAllow, PermissionResultDeny,
    AssistantMessage, UserMessage, ResultMessage,
    TextBlock, ToolUseBlock, ToolResultBlock,
)

HERE = os.path.dirname(os.path.abspath(__file__))
APPROVER = os.path.join(HERE, "spike_v3_approver.py")
ASK_DIR = "/tmp/spike-v3-ask"
ALLOWED_CANARY = "/tmp/spike-v3-ALLOWED-canary"
DENIED_CANARY = "/tmp/spike-v3-DENIED-canary"
PYEXE = sys.executable
t0 = time.time()

# Prefix rule standing in for the litellm notify-classify model — in the real gate
# this callback would POST the pending tool to the classifier and use its verdict.
READONLY_BASH = ("echo", "ls", "cat", "pwd", "git status", "git log", "git diff",
                 "grep", "head", "tail", "wc", "date", "which", "env")


def classify_read_only(name: str, inp: dict) -> bool:
    if name in ("Read", "Grep", "Glob"):
        return True
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        return any(cmd == p or cmd.startswith(p + " ") for p in READONLY_BASH)
    return False


async def wait_for_resp(path: str, timeout: float = 15.0):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            return open(path).read().strip()
        await asyncio.sleep(0.1)
    return None


async def can_use_tool(name, inp, ctx):
    el = time.time() - t0
    desc = inp.get("command", inp) if name == "Bash" else inp
    if classify_read_only(name, inp):
        print(f"[gate] +{el:4.1f}s AUTO-ALLOW (read-only) {name}: {desc!r}")
        return PermissionResultAllow()
    # mutating -> reach a human out-of-band (this write == the Slack notify)
    tid = ctx.tool_use_id or f"anon-{int(el*1000)}"
    req = os.path.join(ASK_DIR, f"{tid}.req")
    resp = os.path.join(ASK_DIR, f"{tid}.resp")
    with open(req, "w") as f:
        f.write(str(desc))
    print(f"[gate] +{el:4.1f}s HOLD — needs human; notified approver: {name}: {desc!r}")
    decision = await wait_for_resp(resp)
    el2 = time.time() - t0
    if decision == "allow":
        print(f"[gate] +{el2:4.1f}s human APPROVED -> allow")
        return PermissionResultAllow()
    print(f"[gate] +{el2:4.1f}s human {'DENIED' if decision == 'deny' else 'NO-RESPONSE -> FAIL-SAFE'} -> deny")
    return PermissionResultDeny(message="blocked by human approver (spike V3)")


async def drain(client, label):
    async for msg in client.receive_response():
        el = time.time() - t0
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    print(f"[{label}] +{el:4.1f}s assistant: {b.text.strip()[:120]!r}")
                elif isinstance(b, ToolUseBlock):
                    print(f"[{label}] +{el:4.1f}s tool_use {b.name}: {b.input}")
        elif isinstance(msg, UserMessage):
            for b in getattr(msg, "content", []) or []:
                if isinstance(b, ToolResultBlock):
                    txt = b.content if isinstance(b.content, str) else str(b.content)
                    print(f"[{label}] +{el:4.1f}s tool_result: {str(txt)[:70]!r} is_error={getattr(b, 'is_error', None)}")
        elif isinstance(msg, ResultMessage):
            print(f"[{label}] +{el:4.1f}s RESULT {msg.subtype} cost={getattr(msg, 'total_cost_usd', None)}")
            return


async def main() -> int:
    os.makedirs(ASK_DIR, exist_ok=True)
    for f in os.listdir(ASK_DIR):
        os.remove(os.path.join(ASK_DIR, f))
    for c in (ALLOWED_CANARY, DENIED_CANARY):
        try:
            os.remove(c)
        except FileNotFoundError:
            pass

    approver = await asyncio.create_subprocess_exec(PYEXE, APPROVER, ASK_DIR, "40")
    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        permission_mode="default",     # consult can_use_tool (do NOT allow-list Bash — that shadows the callback)
        can_use_tool=can_use_tool,
        setting_sources=[],
        max_turns=12,
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Use the Bash tool to run these three commands one at a time, in order, "
            "reporting the outcome of each (if one is blocked, note it and continue to the next):\n"
            "1. git status --short\n"
            f"2. touch {ALLOWED_CANARY}\n"
            f"3. touch {DENIED_CANARY}"
        )
        await drain(client, "run")
    try:
        approver.terminate()
    except ProcessLookupError:
        pass

    print("\n[v3] ── verdict ──")
    a_ok = os.path.exists(ALLOWED_CANARY)
    d_blocked = not os.path.exists(DENIED_CANARY)
    print(f"[v3] read-only  git status        -> auto-allowed inline (see gate log)")
    print(f"[v3] ask->APPROVE touch ALLOWED   -> file created: {a_ok}   {'✅' if a_ok else '❌'}")
    print(f"[v3] ask->DENY    touch DENIED    -> execution blocked: {d_blocked}  {'✅' if d_blocked else '❌'}")
    ok = a_ok and d_blocked
    print(f"[v3] V3 {'PASS ✅' if ok else 'FAIL ❌'} — can_use_tool gates read-only vs async human-hold, no send-keys")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
