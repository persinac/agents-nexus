"""Tests for best-effort triage on mission exhaustion (docs/spec-triage-on-exhaust.md).

Stubs the SDK/DB/subprocess boundary and exercises the pure logic (dedupe, cap, gate) plus the
finalize() branch flow. Run: agent-runner/.venv/bin/python -m pytest test_triage_on_exhaust.py -q
(from agent-runner/, with AGENTS_NEXUS_DIR set so conductor.py finds .env)."""
import asyncio
import types
import conductor as C


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeDb:
    def __init__(self, replans=5):
        self.events = []       # (kind, payload)
        self.status = None
        self.mission = {"replan_count": replans}
    def list_subtasks(self, mid): return [{"subtask_key": "s1", "result": {"summary": "did x", "artifacts": []}}]
    def get_mission(self, mid): return dict(self.mission)
    def update_mission(self, mid, **f): self.mission.update(f)
    def finish_mission(self, mid, status): self.status = status
    def log_event(self, mid, kind, payload): self.events.append((kind, payload))
    def kinds(self): return [k for k, _ in self.events]
    def payload(self, kind): return next((p for k, p in self.events if k == kind), None)


def _run(coro): return asyncio.run(coro)   # py3.14 removed the implicit get_event_loop() loop


def _patch(monkeypatch=None, **kw):
    """Set module globals + return a restore fn (no pytest.monkeypatch dependency)."""
    saved = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    return lambda: [setattr(C, k, v) for k, v in saved.items()]


# ── D4 pure logic: dedupe, cap, false-positive gate ────────────────────────────
def test_norm_where_collapses_locus():
    a = C._norm_where("chatbot/service.py:42")
    b = C._norm_where("chatbot/service.py: line 42 in chat()")
    # both resolve to the same file, so coupled findings dedupe
    assert a.startswith("chatbot/service.py")
    assert C._norm_where("foo.py:chat()") == C._norm_where("foo.py:chat()")


def test_false_positive_gate():
    assert C._triage_rejected({"what": "use of except A, B: here", "fix_hint": ""})
    assert C._triage_rejected({"what": "x", "fix_hint": "the formatter would revert this"})
    assert not C._triage_rejected({"what": "missing APITimeoutError handler", "fix_hint": "add except"})


def _find_triage(cap=6, findings=None, dry=False, enabled=True):
    db = FakeDb()
    restore = _patch(DRY_RUN=dry,
                     REPORTING={"jira": {"enabled": enabled, "project": "FC",
                                         "assignee": "acct-1"}})
    # stub reporter_agent → returns a fake key per call
    calls = {"n": 0}
    async def fake_reporter(instr, mcp):
        calls["n"] += 1
        return {"key": f"FC-{9000 + calls['n']}"}
    restore2 = _patch(reporter_agent=fake_reporter)
    try:
        verdict = {"findings": findings}
        keys = _run(C._file_triage_tickets(db, "mid1", "goal FC-1", verdict, "FC-1", "FC-100", "http://mr", cap=cap))
        return db, keys, calls["n"]
    finally:
        restore2(); restore()


def test_severity_floor_and_filing():
    findings = [
        {"severity": "blocker", "where": "a.py:1", "what": "boom", "lens": "correctness"},
        {"severity": "minor",   "where": "b.py:2", "what": "nit",  "lens": "style"},      # dropped
        {"severity": "major",   "where": "c.py:3", "what": "leak", "lens": "safety"},
    ]
    db, keys, ncreate = _find_triage(findings=findings)
    assert len(keys) == 2          # blocker + major; minor dropped
    assert ncreate == 2
    assert set(db.payload("triaged")["tickets"]) == set(keys)


def test_dedupe_and_cap_rolls_remainder():
    # 8 major findings; 3 share one locus (collapse to 1) → 6 unique loci; cap=3 → 3 head + roll
    fs = []
    for i in range(3):
        fs.append({"severity": "major", "where": "same.py:chat()", "what": f"coupled {i}", "lens": f"l{i}"})
    for i in range(5):
        fs.append({"severity": "major", "where": f"u{i}.py:{i}", "what": f"uniq {i}", "lens": "x"})
    # unique loci = 1 (coupled) + 5 = 6; cap=3 → head 3, overflow 3 → +1 "N more" ticket = 4 tickets
    db, keys, ncreate = _find_triage(cap=3, findings=fs)
    assert db.payload("triage_capped") is not None
    assert db.payload("triage_capped")["overflow"] == 3
    assert len(keys) == 4          # 3 individual + 1 rolled-remainder
    # nothing silently dropped: capped event records the overflow count
    assert db.payload("triaged")["capped"] == 3


def test_false_positive_rejected_not_filed():
    fs = [
        {"severity": "blocker", "where": "z.py:1", "what": "except A, B: syntax", "lens": "py"},   # rejected
        {"severity": "blocker", "where": "z.py:2", "what": "real null deref", "lens": "correctness"},
    ]
    db, keys, ncreate = _find_triage(findings=fs)
    assert len(keys) == 1
    assert db.payload("triage_rejected") is not None
    assert db.payload("triaged")["rejected"] == 1


def test_triage_dry_run_files_nothing():
    fs = [{"severity": "blocker", "where": "a.py:1", "what": "x", "lens": "c"}]
    db, keys, ncreate = _find_triage(findings=fs, dry=True)
    assert keys == []
    assert ncreate == 0
    assert db.payload("triage_dryrun") is not None
    assert db.payload("triage_dryrun")["would_file"][0]["parent"] == "FC-1239"


# ── D2 branch flow: exhaust → partial vs escalate ──────────────────────────────
def _run_finalize(on_exhausted, ok):
    db = FakeDb()
    verdict = {"pass": ok, "findings": [{"severity": "blocker", "where": "a.py:1", "what": "x", "lens": "c"}]}
    restore = _patch(ON_EXHAUSTED=on_exhausted)
    async def fake_rv(db_, mid, goal, start_round=0): return verdict, ok
    async def fake_safe_syn(db_, mid, goal, subs, v, verified=True): return "art"
    report_calls = {}
    async def fake_report(db_, mid, goal, art, subs, v, draft=False, triage=False):
        report_calls.update(draft=draft, triage=triage); return ["db", "mr", "triage"]
    r2 = _patch(run_and_verify=fake_rv, _safe_synthesize=fake_safe_syn, report=fake_report)
    try:
        _mid, status = _run(C.finalize(db, "mid1", "goal"))
        return db, status, report_calls
    finally:
        r2(); restore()


def test_exhaust_partial_opens_draft_and_triages():
    db, status, rc = _run_finalize("partial", ok=False)
    assert status == "partial"
    assert db.status == "partial"
    assert rc == {"draft": True, "triage": True}
    assert db.payload("partial") is not None


def test_exhaust_default_stays_escalate():
    db, status, rc = _run_finalize("escalate", ok=False)
    assert status == "escalated"
    assert db.status == "escalated"
    assert rc == {}                       # report() never called
    assert db.payload("escalated") is not None


def test_pass_path_untouched():
    db, status, rc = _run_finalize("partial", ok=True)   # even with partial enabled, a pass is a pass
    assert status == "done"
    assert db.status == "done"
    assert rc == {"draft": False, "triage": False}       # non-draft, no triage


# ── pytest-free runner (the venv has no pytest) ────────────────────────────────
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
