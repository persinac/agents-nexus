#!/usr/bin/env python3
"""External approver — stands in for a human answering a Slack approval prompt.

Watches ASK_DIR for `<tool_use_id>.req` files (written by the SDK `can_use_tool`
gate when a mutating tool needs sign-off) and writes `<tool_use_id>.resp` with
`allow`/`deny`. This is the out-of-band decision channel the real harness would
run over the Slack bus — the point is the gate reaches a human WITHOUT send-keys.

Policy (stand-in for a human's judgement): deny if the command mentions DENIED,
otherwise allow. Runs until the timeout then exits.
"""
import os
import sys
import time

ASK_DIR = sys.argv[1]
deadline = time.time() + float(sys.argv[2] if len(sys.argv) > 2 else 30)
seen = set()
while time.time() < deadline:
    for fn in sorted(os.listdir(ASK_DIR)):
        if fn.endswith(".req") and fn not in seen:
            seen.add(fn)
            cmd = open(os.path.join(ASK_DIR, fn)).read()
            decision = "deny" if "DENIED" in cmd else "allow"
            with open(os.path.join(ASK_DIR, fn[:-4] + ".resp"), "w") as f:
                f.write(decision)
            print(f"[approver] {fn}: {cmd!r} -> {decision}", flush=True)
    time.sleep(0.15)
