"""Decision logic of verify_mission's comment gate: findings fail, clean passes, an absent or
unrunnable gate script skips. Run from agent-runner/ with AGENTS_NEXUS_DIR set:
`.venv/bin/python test_comment_gate.py`."""
import asyncio
import json
import subprocess
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
    """Call _comment_gate with a stubbed subprocess; returns (probe, recorded (argv, kwargs))."""
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        if isinstance(result, Exception):
            raise result
        return result

    # The real exception classes, so an over-broad `except` in _comment_gate still fails here.
    restore = _patch(subprocess=types.SimpleNamespace(
        run=run, SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired, DEVNULL=subprocess.DEVNULL), COMMENT_GATE=gate)
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
    assert "bad revision" in gp["skipped"]
    assert C._comment_gate_finding("svc-chatbot", gp) is None


def test_subprocess_crash_is_skipped_not_a_failure():
    gp, _ = _run_gate(OSError("no interpreter"))
    assert "no interpreter" in gp["skipped"]
    assert C._comment_gate_finding("svc-chatbot", gp) is None


def test_timeout_is_skipped_not_a_failure():
    gp, _ = _run_gate(subprocess.TimeoutExpired("cmd", 180))
    assert gp["skipped"] and C._comment_gate_finding("svc-chatbot", gp) is None


def test_no_resolvable_base_is_skipped():
    gp, calls = _run_gate(_R(0, ""), base=None)
    assert "base" in gp["skipped"]
    assert calls == []


def test_a_skipped_probe_never_reads_as_a_pass():
    """`ok: True` on a skip would tell the reviewer fleet the gate verified something."""
    for gp in (_run_gate(_R(2, "", "boom"))[0],
               _run_gate(OSError("x"))[0],
               _run_gate(_R(0, ""), base=None)[0]):
        assert gp["ok"] is None, gp


def test_findings_are_capped_but_counted_in_full():
    gp, _ = _run_gate(_R(1, "\n".join(f"a.py:{i}: TCK-1{i:03d}  # x" for i in range(30))))
    assert gp["count"] == 30 and len(gp["findings"]) == 20


def test_replan_feedback_fits_the_800_char_window():
    """Budgeted by length, not count: 160-char finding lines must not crowd out the reviewers."""
    for width in (40, 110, 160):
        line = "managers/ai/litellm_manager.py:3914: TCK-5760  # " + "x" * width
        gp, _ = _run_gate(_R(1, "\n".join(line for _ in range(29))))
        f = C._comment_gate_finding("svc-chatbot", gp)
        size = len(json.dumps([f]))
        assert size < 800, (width, size)
        assert "more; see the comment_gate probe" in f["what"], width
        assert "29 ticket reference(s)" in f["what"], width


def test_replan_feedback_always_shows_at_least_one_finding():
    gp, _ = _run_gate(_R(1, "a.py:1: TCK-1  # " + "x" * 900))
    what = C._comment_gate_finding("svc-chatbot", gp)["what"]
    assert "TCK-1" in what and "more" not in what.rsplit("\n", 1)[-1]


# ── invocation shape ──────────────────────────────────────────────────────────
def test_invoked_in_diff_mode_against_the_given_base_and_worktree():
    _gp, calls = _run_gate(_R(0, ""), base="origin/release-1.2")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[1] == __file__
    assert argv[2:] == ["--diff", "origin/release-1.2", "--cwd", "/fake/ws"]
    assert kwargs["stdin"] is subprocess.DEVNULL, "a stdin-reading gate must not inherit ours"


# ── the mission-level loop: `ran` gates whether `ok` means anything ───────────
def _gate_run(per_repo):
    """Drive _run_comment_gate over fake worktrees; per_repo maps repo -> probe (or None)."""
    probes = []
    restore = _patch(_mission_worktrees=lambda mid, st: [(r, f"/ws/{r}") for r in per_repo],
                     _diff_base=lambda ws, goal: "origin/main",
                     _comment_gate=lambda ws, base: per_repo[ws.rsplit("/", 1)[1]])
    try:
        return asyncio.run(C._run_comment_gate("mid", "goal", [], probes)) + (probes,)
    finally:
        restore()


def test_no_worktrees_means_the_gate_did_not_run():
    ran, ok, findings, probes = _gate_run({})
    assert (ran, ok, findings, probes) == (False, True, [], [])


def test_absent_script_does_not_count_as_having_run():
    ran, ok, _f, probes = _gate_run({"svc-chatbot": None})
    assert ran is False and ok is True
    assert probes == [], "an absent gate must not leave a probe the reviewers can read"


def test_skipped_gate_does_not_count_as_having_run():
    ran, ok, _f, probes = _gate_run({"svc-chatbot": C._gate_skipped("bad ref", "origin/main")})
    assert ran is False and ok is True
    assert probes[0]["ok"] is None


def test_clean_gate_counts_as_having_run():
    clean = {"probe": "comment_gate", "base": "origin/main", "ok": True, "count": 0, "findings": []}
    ran, ok, findings, _p = _gate_run({"svc-chatbot": clean})
    assert (ran, ok, findings) == (True, True, [])


def test_one_dirty_repo_fails_the_whole_mission():
    clean = {"probe": "comment_gate", "base": "origin/main", "ok": True, "count": 0, "findings": []}
    dirty = {"probe": "comment_gate", "base": "origin/main", "ok": False, "count": 1,
             "findings": [FINDING]}
    ran, ok, findings, _p = _gate_run({"a-svc": clean, "b-svc": dirty})
    assert ran is True and ok is False
    assert len(findings) == 1 and findings[0]["severity"] == "blocker"


# ── _diff_base ────────────────────────────────────────────────────────────────
def _diff_base(goal, resolvable):
    def run(args, **kw):
        return _R(0 if args[-1].split("^{")[0] in resolvable else 1)

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
