"""--gate-before-report: a verified mission holds before it files anything.
Run: agent-runner/.venv/bin/python test_report_gate.py"""
import asyncio
import conductor as C


class FakeDb:
    def __init__(self):
        self.events = []
        self.status = None
        self.mission = {"replan_count": 0}

    def list_subtasks(self, mid):
        return [{"subtask_key": "s1", "repo": "r", "result": {"summary": "did x", "artifacts": []}}]

    def get_mission(self, mid):
        return dict(self.mission)

    def update_mission(self, mid, **f):
        self.mission.update(f)

    def finish_mission(self, mid, status):
        self.status = status

    def log_event(self, mid, kind, payload):
        self.events.append((kind, payload))

    def kinds(self):
        return [k for k, _ in self.events]

    def payload(self, kind):
        return next((p for k, p in self.events if k == kind), None)


def _run(coro):
    return asyncio.run(coro)


def _patch(**kw):
    saved = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    return lambda: [setattr(C, k, v) for k, v in saved.items()]


def _finalize(gate, ok=True):
    db = FakeDb()
    verdict = {"pass": ok, "findings": []}
    reported = {"called": False}

    async def fake_rv(db_, mid, goal, start_round=0):
        return verdict, ok

    async def fake_syn(db_, mid, goal, subs, v, verified=True):
        return "the writeup"

    async def fake_report(db_, mid, goal, art, subs, v, draft=False, triage=False):
        reported["called"] = True
        return ["db", "mr", "jira"]

    restore = _patch(GATE_BEFORE_REPORT=gate, run_and_verify=fake_rv,
                     _safe_synthesize=fake_syn, report=fake_report,
                     _commit_worktrees=lambda mid, subs, goal: ["feat-branch"],
                     _slack_relay=lambda m: None)
    try:
        _mid, status = _run(C.finalize(db, "mid1", "goal"))
        return db, status, reported["called"]
    finally:
        restore()


def test_gate_on_holds_before_filing():
    db, status, reported = _finalize(gate=True)
    assert status == "gated"
    assert db.status == "gated"
    assert reported is False, "gate must not reach report() — that is what files the MR/Jira"


def test_gate_on_still_commits_the_branch():
    db, _status, _reported = _finalize(gate=True)
    p = db.payload("report_gate")
    assert p is not None
    assert p["branches"] == ["feat-branch"], "the branch is the deliverable; only filing is held"
    assert p["artifact"] == "the writeup"


def test_gate_off_reports_as_before():
    db, status, reported = _finalize(gate=False)
    assert status == "done"
    assert db.status == "done"
    assert reported is True
    assert "report_gate" not in db.kinds()


def test_gate_does_not_apply_to_a_failed_mission():
    restore = _patch(ON_EXHAUSTED="escalate")
    try:
        db, status, reported = _finalize(gate=True, ok=False)
        assert status == "escalated", "a gate is for verified work, not a failure path"
        assert reported is False
        assert "report_gate" not in db.kinds()
    finally:
        restore()


if __name__ == "__main__":
    import sys
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    fails = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  \033[32mPASS\033[0m {name}")
        except Exception as e:
            fails += 1
            import traceback
            print(f"  \033[31mFAIL\033[0m {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
