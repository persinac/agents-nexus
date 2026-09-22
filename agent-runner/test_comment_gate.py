"""Decision logic of verify_mission's comment gate: findings fail, clean passes, an absent or
unrunnable gate script skips. Run from agent-runner/ with AGENTS_NEXUS_DIR set:
`.venv/bin/python test_comment_gate.py`."""
import types

import conductor as C

FINDING = "kubernetes/crossplane-svc-chatbot.yml:81: TCK-5788  # Provider circuit breaker."


def _patch(**kw):
    saved = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    return lambda: [setattr(C, k, v) for k, v in saved.items()]


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _run_gate(result, base="origin/main", gate=__file__):
    """Call _comment_gate with a stubbed subprocess; returns (probe, recorded argv lists)."""
    calls = []

    def run(args, **kw):
        calls.append(args)
        if isinstance(result, Exception):
            raise result
        return result

    restore = _patch(subprocess=types.SimpleNamespace(
        run=run, SubprocessError=Exception, TimeoutExpired=Exception), COMMENT_GATE=gate)
    try:
        return C._comment_gate("/fake/ws", base), calls
    finally:
        restore()


# ── decision logic ────────────────────────────────────────────────────────────
def test_findings_are_a_hard_fail():
    gp, _ = _run_gate(_R(1, FINDING + "\n"))
    assert gp["ok"] is False and gp["count"] == 1
    f = C._comment_gate_finding("svc-chatbot", gp)
    assert f["severity"] == "blocker"
    assert "TCK-5788" in f["what"]
    assert "MR description" in f["fix_hint"]


def test_clean_gate_passes():
    gp, _ = _run_gate(_R(0, ""))
    assert gp["ok"] is True and gp["count"] == 0
    assert C._comment_gate_finding("svc-chatbot", gp) is None


def test_missing_script_is_skipped_not_a_failure():
    gp, calls = _run_gate(_R(0, ""), gate="/nonexistent/comment-ticket-refs.py")
    assert gp is None, "an absent guard must not produce a verdict"
    assert calls == [], "must not shell out when the script is absent"
    assert C._comment_gate_finding("svc-chatbot", None) is None


def test_gate_error_exit_is_skipped_not_a_failure():
    gp, _ = _run_gate(_R(2, "", "fatal: bad revision 'origin/nope'"))
    assert gp["ok"] is True and "bad revision" in gp["skipped"]
    assert C._comment_gate_finding("svc-chatbot", gp) is None


def test_subprocess_crash_is_skipped_not_a_failure():
    gp, _ = _run_gate(OSError("no interpreter"))
    assert gp["ok"] is True and "no interpreter" in gp["skipped"]
    assert C._comment_gate_finding("svc-chatbot", gp) is None


def test_no_resolvable_base_is_skipped():
    gp, calls = _run_gate(_R(0, ""), base=None)
    assert gp["ok"] is True and "base" in gp["skipped"]
    assert calls == []


def test_findings_are_capped_but_counted_in_full():
    gp, _ = _run_gate(_R(1, "\n".join(f"a.py:{i}: TCK-1{i:03d}  # x" for i in range(30))))
    assert gp["count"] == 30 and len(gp["findings"]) == 20


# ── invocation shape ──────────────────────────────────────────────────────────
def test_invoked_in_diff_mode_against_the_given_base_and_worktree():
    _gp, calls = _run_gate(_R(0, ""), base="origin/release-1.2")
    assert len(calls) == 1
    argv = calls[0]
    assert argv[1] == __file__
    assert argv[2:] == ["--diff", "origin/release-1.2", "--cwd", "/fake/ws"]


# ── _diff_base ────────────────────────────────────────────────────────────────
def _diff_base(goal, resolvable):
    def run(args, **kw):
        return _R(0 if args[-1].rstrip("^{commit}").rstrip("^{") in resolvable else 1)

    restore = _patch(subprocess=types.SimpleNamespace(run=run))
    try:
        return C._diff_base("/fake/ws", goal)
    finally:
        restore()


def test_diff_base_prefers_the_goals_explicit_base():
    assert _diff_base("do X\n\nbase=demo-architecture", {"origin/main"}) == "demo-architecture"


def test_diff_base_falls_back_to_the_origin_default():
    assert _diff_base("do X", {"origin/main"}) == "origin/main"
    assert _diff_base("do X", {"origin/master"}) == "origin/master"


def test_diff_base_is_none_when_nothing_resolves():
    assert _diff_base("do X", set()) is None


if __name__ == "__main__":
    import sys
    import traceback
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    fails = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  \033[32mPASS\033[0m {name}")
        except Exception as e:
            fails += 1
            print(f"  \033[31mFAIL\033[0m {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
