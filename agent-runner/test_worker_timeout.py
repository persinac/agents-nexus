"""Provisioning and worker-timeout tests for conductor.py; run with agent-runner/.venv/bin/python from agent-runner/ with AGENTS_NEXUS_DIR set."""
import asyncio
import os
import shutil
import tempfile
import types
import conductor as C


def _patch(**kw):
    saved = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    return lambda: [setattr(C, k, v) for k, v in saved.items()]


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeDb:
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.events = []

    def get_subtask(self, sid):
        return dict(self.rows[sid]) if sid in self.rows else None

    def list_subtasks(self, mid):
        return [dict(r) for r in self.rows.values()]

    def get_mission(self, mid):
        return {"goal": "ground the handoff path"}

    def update_subtask(self, sid, **fields):
        self.rows[sid].update(fields)

    def log_event(self, mid, kind, payload=None, subtask_id=None):
        self.events.append((kind, payload, subtask_id))

    def kinds(self):
        return [k for k, _, _ in self.events]


async def _nosleep(_):
    return None


def _fake_git(add_results):
    calls = []
    results = list(add_results)

    def run(args, **kw):
        calls.append(list(args))
        if "rev-parse" in args:
            return _R(0)
        if "worktree" in args and "add" in args:
            return results.pop(0) if results else _R(0)
        return _R(0)

    return types.SimpleNamespace(run=run), calls


def _ensure(add_results):
    tmp = tempfile.mkdtemp()
    fake_sp, calls = _fake_git(add_results)
    restore = _patch(subprocess=fake_sp, _repo_dir=lambda r: "/fake/rp", _is_git=lambda p: True,
                     workspace=lambda mid, r: os.path.join(tmp, "wt", r))
    try:
        return C.ensure_workspace("mid12345", "svc-chatbot", "cn-5169-validate"), calls
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def _worktree_verbs(calls):
    return [a[a.index("worktree") + 1] for a in calls if "worktree" in a]


def test_ensure_workspace_prunes_then_raises_when_git_still_refuses():
    stale = _R(128, err="fatal: 'cn-5169-validate' is already checked out at '/old/wt'")
    raised = None
    calls = None
    try:
        _, calls = _ensure([_R(128, err="fatal: a branch named 'cn-5169-validate' already exists"), stale])
    except C.WorkspaceError as e:
        raised = e
    assert raised is not None, "must raise, not return a path that does not exist"
    assert "already checked out" in str(raised)


def test_ensure_workspace_prune_sits_between_the_two_adds():
    fake_sp, calls = _fake_git([_R(128, err="exists"), _R(128, err="already checked out")])
    tmp = tempfile.mkdtemp()
    restore = _patch(subprocess=fake_sp, _repo_dir=lambda r: "/fake/rp", _is_git=lambda p: True,
                     workspace=lambda mid, r: os.path.join(tmp, "wt", r))
    try:
        try:
            C.ensure_workspace("mid12345", "svc-chatbot", "cn-5169-validate")
        except C.WorkspaceError:
            pass
        assert _worktree_verbs(calls) == ["add", "prune", "add"], _worktree_verbs(calls)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def test_ensure_workspace_attach_after_prune_succeeds():
    (ws, branch), calls = _ensure([_R(128, err="exists"), _R(0)])
    assert branch == "cn-5169-validate" and ws.endswith("wt/svc-chatbot")
    assert _worktree_verbs(calls) == ["add", "prune", "add"]


def test_ensure_workspace_first_add_success_is_unchanged():
    (ws, branch), calls = _ensure([_R(0)])
    assert _worktree_verbs(calls) == ["add"]


def test_workspace_error_is_a_runtime_error_but_base_failure_is_not_one():
    assert issubclass(C.WorkspaceError, RuntimeError)
    assert C.WorkspaceError is not RuntimeError


def test_execute_dag_records_workspace_error_and_skips_spawn():
    rows = [{"id": "a", "subtask_key": "s2", "profile": "investigation", "status": "pending", "attempt": 1,
             "depends_on": [], "repo": "svc-chatbot", "goal": "trace the handoff"}]
    db = FakeDb(rows)

    def refuse(*a, **k):
        raise C.WorkspaceError("ensure_workspace: git could not create /x on b: already checked out")

    def must_not_spawn(*a, **k):
        raise AssertionError("spawn_worker must not run for a subtask without a worktree")

    async def no_wait(db_, mid, subs):
        return None

    restore = _patch(ensure_workspace=refuse, spawn_worker=must_not_spawn, wait_terminal=no_wait)
    try:
        asyncio.run(C.execute_dag(db, "mid12345"))
    finally:
        restore()
    assert db.rows["a"]["status"] == "error"
    assert db.rows["a"]["result"]["summary"].startswith("ensure_workspace")
    assert db.kinds() == ["workspace_error"], db.kinds()


def test_worker_timeout_default_and_per_profile():
    restore = _patch(WORKER_TIMEOUT=1200, WORKER_TIMEOUTS={"data-analysis": 2700, "one-shot": "2400"})
    try:
        assert C.worker_timeout("investigation") == 1200
        assert C.worker_timeout("data-analysis") == 2700
        assert C.worker_timeout("one-shot") == 2400
        assert C.worker_timeout(None) == 1200
    finally:
        restore()


def test_worker_timeout_survives_a_bad_policy_value():
    restore = _patch(WORKER_TIMEOUT=1200, WORKER_TIMEOUTS={"one-shot": "forty minutes"})
    try:
        assert C.worker_timeout("one-shot") == 1200
    finally:
        restore()


def test_wait_terminal_stops_the_pane_and_marks_error_on_timeout():
    rows = [{"id": "a", "subtask_key": "s4", "profile": "data-analysis", "status": "running", "attempt": 2}]
    db = FakeDb(rows)
    stopped = []

    def fake_stop(mid, st):
        stopped.append((mid, st["subtask_key"]))
        return "w9:p1"

    restore = _patch(asyncio=types.SimpleNamespace(sleep=_nosleep), WORKER_TIMEOUT=0, WORKER_TIMEOUTS={},
                     stop_worker=fake_stop)
    try:
        asyncio.run(C.wait_terminal(db, "mid12345", rows))
    finally:
        restore()
    assert stopped == [("mid12345", "s4")]
    assert db.rows["a"]["status"] == "error" and db.rows["a"]["result"]["summary"] == "worker timeout"
    assert db.kinds() == ["worker_stopped"]
    payload = db.events[0][1]
    assert payload["pane"] == "w9:p1" and payload["attempt"] == 2 and payload["subtask"] == "s4"


def test_wait_terminal_returns_without_stopping_when_the_worker_finishes():
    rows = [{"id": "a", "subtask_key": "s1", "profile": "one-shot", "status": "running", "attempt": 1}]
    db = FakeDb(rows)
    ticks = {"n": 0}

    async def finish_on_second_poll(_):
        ticks["n"] += 1
        if ticks["n"] == 2:
            db.rows["a"]["status"] = "done"

    def must_not_stop(*a, **k):
        raise AssertionError("a finished worker must not be stopped")

    restore = _patch(asyncio=types.SimpleNamespace(sleep=finish_on_second_poll), WORKER_TIMEOUT=3600,
                     WORKER_TIMEOUTS={}, stop_worker=must_not_stop)
    try:
        asyncio.run(C.wait_terminal(db, "mid12345", rows))
    finally:
        restore()
    assert db.events == [] and db.rows["a"]["status"] == "done"


def test_wait_terminal_uses_each_subtask_own_deadline():
    rows = [{"id": "fast", "subtask_key": "s2", "profile": "investigation", "status": "running", "attempt": 1},
            {"id": "slow", "subtask_key": "s4", "profile": "data-analysis", "status": "running", "attempt": 1}]
    db = FakeDb(rows)
    ticks = {"n": 0}

    async def finish_slow_later(_):
        ticks["n"] += 1
        if ticks["n"] == 3:
            db.rows["slow"]["status"] = "done"

    restore = _patch(asyncio=types.SimpleNamespace(sleep=finish_slow_later), WORKER_TIMEOUT=0,
                     WORKER_TIMEOUTS={"data-analysis": 3600}, stop_worker=lambda mid, st: "")
    try:
        asyncio.run(C.wait_terminal(db, "mid12345", rows))
    finally:
        restore()
    assert db.rows["fast"]["status"] == "error" and db.rows["slow"]["status"] == "done"
    assert [e[2] for e in db.events] == ["fast"]


def test_result_is_stale():
    started = {"attempt": 1}
    assert not C.result_is_stale(started, {"attempt": 1, "status": "running", "result": None})
    assert C.result_is_stale(started, {"attempt": 2, "status": "running"}), "a retry owns the row now"
    assert C.result_is_stale(started, {"attempt": 1, "status": "error", "result": {"summary": "worker timeout"}})
    assert not C.result_is_stale(started, {"attempt": 1, "status": "error", "result": {"summary": "boom"}})
    assert C.result_is_stale(started, None)


def test_worker_pane_picks_the_newest_matching_entry():
    tmp = tempfile.mkdtemp()
    reg = os.path.join(tmp, "registry")
    os.makedirs(reg)
    entries = {"w1:p1": ("cw-s4-9673", 100), "w2:p3": ("cw-s4-9673", 200), "w3:p1": ("cw-s5-9673", 300)}
    for slot, (name, at) in entries.items():
        with open(os.path.join(reg, slot), "w") as fh:
            fh.write(f"SLOT={slot}\nNAME={name}\nAT={at}\nPANE_ID={slot}\n")
    prev = os.environ.get("NEXUS_TMUX_DIR")
    os.environ["NEXUS_TMUX_DIR"] = tmp
    try:
        assert C.worker_pane("cw-s4-9673") == "w2:p3"
        assert C.worker_pane("cw-s9-0000") == ""
    finally:
        if prev is None:
            del os.environ["NEXUS_TMUX_DIR"]
        else:
            os.environ["NEXUS_TMUX_DIR"] = prev
        shutil.rmtree(tmp, ignore_errors=True)


def test_worker_pane_without_a_registry_dir_is_empty():
    prev = os.environ.get("NEXUS_TMUX_DIR")
    os.environ["NEXUS_TMUX_DIR"] = "/nonexistent/for/test"
    try:
        assert C.worker_pane("cw-s4-9673") == ""
    finally:
        if prev is None:
            del os.environ["NEXUS_TMUX_DIR"]
        else:
            os.environ["NEXUS_TMUX_DIR"] = prev


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
