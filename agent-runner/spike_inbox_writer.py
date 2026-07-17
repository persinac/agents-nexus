#!/usr/bin/env python3
"""Tiny external process that simulates the bus/bridge delivering into an agent's
inbox. Sleeps `delay` seconds (so it lands MID-TURN), then appends one line:
    <write_epoch>\t<message>
The runner reads the write_epoch back to prove the message was buffered during a
turn and only consumed at the next turn boundary.
"""
import sys
import time

inbox, delay, message = sys.argv[1], float(sys.argv[2]), sys.argv[3]
time.sleep(delay)
with open(inbox, "a") as f:
    f.write(f"{time.time()}\t{message}\n")
print(f"[writer] wrote to {inbox} at {time.time():.3f}: {message!r}")
