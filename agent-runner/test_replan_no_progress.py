"""Tests for the no-progress stop in run_and_verify's re-plan loop.

Run: agent-runner/.venv/bin/python -m pytest test_replan_no_progress.py -q (from agent-runner/)."""
import asyncio

import conductor as C


class FakeDb:
    def __init__(self, keys):
        self.subs = [{"id": i, "subtask_key": k, "status": "pending", "attempt": 0, "goal": k, "result": {}}
                     for i, k in enumerate(keys)]
        self.events = []

    def list_subtasks(self, mid): return [dict(s) for s in self.subs]
    def update_subtask(self, sid, **f): self.subs[sid].update(f)
    def update_mission(self, mid, **f): pass
    def log_event(self, mid, kind, payload): self.events.append((kind, payload))
    def kinds(self): return [k for k, _ in self.events]
    def rounds(self): return sum(1 for k in self.kinds() if k == "round")


def run(db, failing_per_round, blockers_per_round, start_round=0, pass_when_clear=True):
    """Round n leaves failing_per_round[n] not done; verify then reports blockers_per_round[n] blockers."""
    state = {"dag": 0, "verify": 0}

    async def fake_execute(_db, _mid):
        failing = failing_per_round[min(state["dag"], len(failing_per_round) - 1)]
        for s in db.subs:
            s["status"] = "error" if s["subtask_key"] in failing else "done"
        state["dag"] += 1

    async def fake_verify(mid, goal, subs):
        n = blockers_per_round[min(state["verify"], len(blockers_per_round) - 1)]
        state["verify"] += 1
        return {"pass": pass_when_clear and n == 0, "findings": [{"severity": "blocker"}] * n + [{"severity": "major"}]}, []

    saved = {k: getattr(C, k) for k in ("execute_dag", "verify_mission", "MAX_REPLANS", "ESCALATE_AFTER")}
    C.execute_dag, C.verify_mission, C.MAX_REPLANS, C.ESCALATE_AFTER = fake_execute, fake_verify, 5, 2
    try:
        return asyncio.run(C.run_and_verify(db, "m1", "goal", start_round=start_round))
    finally:
        for k, v in saved.items():
            setattr(C, k, v)


def test_reviewer_blockers_not_shrinking_after_escalation_stop_the_loop():
    db = FakeDb(["s1"])
    verdict, ok = run(db, [set()], [2])
    assert not ok
    assert db.rounds() == 3
    assert ("no_progress", {"round": 2, "blockers": 2, "prev_blockers": 2}) in db.events


def test_shrinking_reviewer_blockers_keep_replanning_to_a_pass():
    db = FakeDb(["s1"])
    verdict, ok = run(db, [set()], [4, 3, 2, 1, 0])
    assert ok
    assert db.rounds() == 5
    assert "no_progress" not in db.kinds()


def test_no_stop_before_an_escalated_round():
    db = FakeDb(["s1"])
    verdict, ok = run(db, [set()], [2, 2, 0])
    assert ok
    assert db.rounds() == 3


def test_failing_subtasks_keep_the_full_replan_budget():
    db = FakeDb(["s1", "s2"])
    verdict, ok = run(db, [{"s1"}], [0])
    assert not ok
    assert db.rounds() == 6
    assert "no_progress" not in db.kinds()


def test_major_only_failures_run_to_the_cap():
    db = FakeDb(["s1"])
    verdict, ok = run(db, [set()], [0], pass_when_clear=False)
    assert not ok
    assert db.rounds() == 6
    assert "no_progress" not in db.kinds()


def test_resumed_mission_compares_only_rounds_it_ran():
    db = FakeDb(["s1"])
    verdict, ok = run(db, [set()], [2], start_round=3)
    assert not ok
    assert db.rounds() == 2
    assert ("no_progress", {"round": 4, "blockers": 2, "prev_blockers": 2}) in db.events
