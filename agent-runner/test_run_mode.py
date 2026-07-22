#!/usr/bin/env python3
"""Tests for the explicit, env-first run mode (replaces the live!/dry! goal-text prefixes) and
the triage-on-exhaust dry-run gate. pytest-free — run: python3 test_run_mode.py

Covers:
  - _resolve_run_mode(): default dry, env dry/live (case-insensitive), bogus -> dry (fail safe),
    legacy CONDUCTOR_DRY_RUN=1 alias -> dry.
  - _file_triage_tickets() files NOTHING under DRY_RUN (belt against the FC-1513..1519 leak, where
    a 'dry' distributed mission filed REAL Jira tickets because the mode never crossed the seam).
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conductor

_fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fails.append(name)


# ── run-mode resolution ──────────────────────────────────────────────────────
for _k in ("CONDUCTOR_RUN_MODE", "CONDUCTOR_DRY_RUN"):
    os.environ.pop(_k, None)
check("unset -> dry (safe default)", conductor._resolve_run_mode() == "dry")
os.environ["CONDUCTOR_RUN_MODE"] = "live"
check("env live -> live", conductor._resolve_run_mode() == "live")
os.environ["CONDUCTOR_RUN_MODE"] = "LIVE"
check("env LIVE (case-insensitive) -> live", conductor._resolve_run_mode() == "live")
os.environ["CONDUCTOR_RUN_MODE"] = "dry"
check("env dry -> dry", conductor._resolve_run_mode() == "dry")
os.environ["CONDUCTOR_RUN_MODE"] = "bogus"
check("bogus value -> dry (fail safe)", conductor._resolve_run_mode() == "dry")
os.environ.pop("CONDUCTOR_RUN_MODE", None)
os.environ["CONDUCTOR_DRY_RUN"] = "1"
check("legacy CONDUCTOR_DRY_RUN=1 -> dry", conductor._resolve_run_mode() == "dry")
os.environ.pop("CONDUCTOR_DRY_RUN", None)


# ── triage-on-exhaust files nothing under DRY_RUN ────────────────────────────
class FakeDb:
    def __init__(self):
        self.events = []

    def log_event(self, mid, etype, payload):
        self.events.append((etype, payload))


verdict = {"findings": [
    {"severity": "blocker", "where": "svc/foo.py:12", "what": "null deref", "lens": "correctness"},
    {"severity": "major", "where": "svc/bar.py:44", "what": "n+1 query", "lens": "perf"},
]}

conductor.DRY_RUN = True   # module global the triage path reads
db = FakeDb()
keys = asyncio.run(conductor._file_triage_tickets(db, "abcdef12mid", "goal", verdict, "FC-1", "T-1", "http://mr"))
check("triage dry-run returns no keys", keys == [])
check("triage dry-run creates no real ticket", not any(t == "triage_ticket" for t, _ in db.events))
check("triage dry-run logged would-file", any(t == "triage_dryrun" for t, _ in db.events))

if _fails:
    print("\n%d FAILED: %s" % (len(_fails), _fails))
    sys.exit(1)
print("\nall run-mode tests passed")
