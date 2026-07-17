#!/usr/bin/env python3
"""Headless Conductor worker — runs ONE subtask and writes the result to the DB, then
exits. The Conductor spawns one of these per subtask (own tmux window / repo cwd), and
gathers by polling the subtask row. This is the cross-process dispatch node (slice C):
the worker is a separate, fleet-visible process, not an in-process call.

Args: <mission_id> <subtask_id>
"""
import os
import socket
import sys

import anyio

from conductor import PROFILES, run_worker, _set_sess, _pane_self, _register_self, _deregister_self, SUBSTRATE
from conductor_db import Db


async def main() -> int:
    if len(sys.argv) < 3:
        print("usage: conductor_worker.py <mission_id> <subtask_id>")
        return 2
    mid, sid = sys.argv[1], sys.argv[2]
    db = Db()
    st = db.get_subtask(sid)
    if not st:
        print(f"[worker] subtask {sid} not found")
        db.close()
        return 2

    profile = PROFILES.get(st["profile"], PROFILES["one-shot"])
    _set_sess(f"mission-{mid[:8]}")
    worker_id = f"{socket.gethostname().split('.')[0]}:{os.environ.get('TMUX_PANE', 'headless')}"

    # Register this worker in the fleet registry so the reaper/peers/name-resolution see it
    # (register-always, docs/herdr-workflow.md #8) — seam-spawned workers bypass open-claude.sh.
    # Tag it into the mission cohort so a worker idling between DAG waves isn't reaped mid-flight;
    # deregister + drop the cohort in the finally (a headless pane has no tmux pane-died hook).
    ws = os.environ.get("CONDUCTOR_MISSION_WS", "")
    pane = _pane_self()
    _register_self(f"cw-{st['subtask_key']}-{mid[:4]}", cwd=os.getcwd(), ws=ws or None)
    if pane and ws:
        try:
            import subprocess
            subprocess.run([SUBSTRATE, "cohort", pane, ws], check=False, capture_output=True, timeout=5)
        except Exception:
            pass

    db.update_subtask(sid, status="running", worker=worker_id)
    db.log_event(mid, "worker_started",
                 {"subtask": st["subtask_key"], "effort": st["effort"], "worker": worker_id}, subtask_id=sid)
    print(f"[worker] {st['subtask_key']} @ {st['effort']} → {st['goal'][:80]}")

    try:
        wr = await run_worker(st, profile, st["effort"])
    except Exception as e:
        wr = {"subtask_id": sid, "status": "error", "summary": f"{type(e).__name__}: {e}",
              "artifacts": [], "handoff": None}
    finally:
        _deregister_self()   # self-clean: no pane-died hook for a headless python pane

    wr["subtask_id"] = sid
    db.update_subtask(sid, status=wr["status"], result=wr)
    db.log_event(mid, "worker_done", wr, subtask_id=sid)
    db.close()
    print(f"[worker] {st['subtask_key']} → {wr['status']} · artifacts={wr.get('artifacts')}")
    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
