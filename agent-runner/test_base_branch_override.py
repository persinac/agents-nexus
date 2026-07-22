"""Tests for the opt-in, fail-closed base-branch override (conductor.py).

Motivation: a mission whose work stacks on an *unmerged* feature branch (e.g. the demo-shim P0
SDK, which sits on `demo-architecture`) must branch off that ref, not the hardcoded origin/main —
otherwise the worker lands in a tree without the files it's meant to edit. `base=<ref>` in the
goal opts in; absent the token, behaviour is byte-identical to before.

Stubs the git/subprocess + fs boundary and exercises the pure parse + the worktree-add wiring.
Run: agent-runner/.venv/bin/python test_base_branch_override.py
(from agent-runner/, with AGENTS_NEXUS_DIR set so conductor.py finds .env)."""
import os
import shutil
import tempfile
import types
import conductor as C


def _patch(**kw):
    """Set module globals + return a restore fn (mirrors test_triage_on_exhaust.py)."""
    saved = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    return lambda: [setattr(C, k, v) for k, v in saved.items()]


class _R:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _fake_sp(resolvable):
    """A fake `subprocess` whose `git rev-parse --verify <ref>` succeeds only for refs in
    `resolvable`; every `worktree add` succeeds. Records all argv lists in `calls`."""
    calls = []

    def run(args, **kw):
        calls.append(args)
        if "rev-parse" in args:
            return _R(0 if args[-1] in resolvable else 1)
        return _R(0)

    return types.SimpleNamespace(run=run), calls


def _call_ensure(base, resolvable, repo="svc-chatbot"):
    tmp = tempfile.mkdtemp()
    fake_sp, calls = _fake_sp(resolvable)
    restore = _patch(subprocess=fake_sp,
                     _repo_dir=lambda r: "/fake/rp",
                     _is_git=lambda p: True,
                     workspace=lambda mid, r: os.path.join(tmp, "wt", r))
    try:
        ws, branch = C.ensure_workspace("mid12345", repo, "conductor-x", base=base)
        return ws, branch, calls
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def _wt_add(calls):
    return next((a for a in calls if "worktree" in a and "add" in a and "-b" in a), None)


# ── _base_branch: deterministic, opt-in parse ──────────────────────────────────
def test_base_branch_parses_eq_and_colon():
    assert C._base_branch("do X\n\nbase=demo-architecture") == "demo-architecture"
    assert C._base_branch("base:feature/x") == "feature/x"


def test_base_branch_absent_is_none():
    assert C._base_branch("Implement P0 in svc-chatbot; open an MR") is None
    assert C._base_branch("") is None
    assert C._base_branch(None) is None


def test_base_branch_word_boundary_guard():
    # a token embedded in a longer word must not match
    assert C._base_branch("update the database=connection string") is None


def test_base_branch_allows_dotted_slashed_refs():
    assert C._base_branch("base=release/1.2.x") == "release/1.2.x"
    assert C._base_branch("base=demo.architecture-v2") == "demo.architecture-v2"


# ── ensure_workspace: base wiring ──────────────────────────────────────────────
def test_uses_remote_tracking_base_first():
    ws, branch, calls = _call_ensure("demo-architecture", {"origin/demo-architecture", "demo-architecture"})
    assert _wt_add(calls)[-1] == "origin/demo-architecture"   # prefers origin/<base>
    assert branch == "conductor-x"


def test_falls_back_to_local_base_when_no_remote():
    ws, branch, calls = _call_ensure("demo-architecture", {"demo-architecture"})   # only local exists
    assert _wt_add(calls)[-1] == "demo-architecture"


def test_fail_closed_when_base_unresolvable():
    raised = None
    try:
        _call_ensure("demo-architecture", set())        # nothing resolves
    except RuntimeError as e:
        raised = e
    assert raised is not None, "must raise, not silently branch off main"
    assert "demo-architecture" in str(raised)


def test_no_base_uses_default_branch_unchanged():
    ws, branch, calls = _call_ensure(None, {"origin/main"})
    assert _wt_add(calls)[-1] == "origin/main"            # legacy behaviour preserved


def test_no_base_never_probes_a_feature_ref():
    # regression guard: without a base= token we must only ever probe the default-branch candidates
    _ws, _b, calls = _call_ensure(None, {"origin/main"})
    probed = [a[-1] for a in calls if "rev-parse" in a]
    assert all(r in ("origin/main", "origin/master", "main", "master") for r in probed), probed


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
