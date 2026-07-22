"""Tests for diff-derived MR naming (cheap-Haiku _name_mr + _open_mr head-branch wiring).

Motivation: the conductor used to slug the MR title/branch from the goal PROSE, producing names like
`[Conductor] branch off origin/main; the MR must target main` and branch
`conductor-branch-off-origin-main-the-mr-must-target-main`. _name_mr summarizes the DIFF on a cheap
model instead; _open_mr pushes the working branch to the clean diff-derived ref.

Stubs the SDK/subprocess boundary; no network. Mirrors test_triage_on_exhaust.py's _patch + __main__.
Run: agent-runner/.venv/bin/python test_mr_naming.py (from agent-runner/, AGENTS_NEXUS_DIR set)."""
import asyncio
import types
import conductor as C


def _run(coro):
    return asyncio.run(coro)


def _patch(**kw):
    saved = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    return lambda: [setattr(C, k, v) for k, v in saved.items()]


class _R:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


# ── _name_mr: happy path + sanitization + fallback contract ────────────────────
def _stub_namer(json_text, diff="stat\n\n+code"):
    """Make _name_mr run against a fake query() yielding json_text, over a non-empty diff."""
    restore_diff = _patch(_mr_diff=lambda wt, target="main": diff)

    async def fake_query(prompt, options):
        # one AssistantMessage carrying one TextBlock
        yield C.AssistantMessage(content=[C.TextBlock(text=json_text)], model="haiku")

    restore_query = _patch(query=fake_query)
    return lambda: (restore_query(), restore_diff())


def test_name_mr_happy_path():
    restore = _stub_namer('{"title":"feat(overlay): add overlay SDK","branch":"feat-overlay-sdk",'
                          '"description":"## What\\nAdds SDK."}')
    try:
        out = _run(C._name_mr("some goal (do X; target main)", "/tmp/wt", "main"))
        assert out is not None
        assert out["title"] == "feat(overlay): add overlay SDK"
        assert out["branch"] == "feat-overlay-sdk"
        assert out["description"].startswith("## What")
    finally:
        restore()


def test_name_mr_strips_slashes_from_branch():
    # CI breaks on '/' in branch names — the namer's branch must be sanitized even if the model emits one.
    restore = _stub_namer('{"title":"feat: x","branch":"feat/overlay/sdk","description":"## What\\nx"}')
    try:
        out = _run(C._name_mr("g", "/tmp/wt"))
        assert "/" not in out["branch"], out["branch"]
        assert out["branch"] == "feat-overlay-sdk"
    finally:
        restore()


def test_name_mr_caps_lengths():
    restore = _stub_namer('{"title":"' + "t" * 200 + '","branch":"' + "b" * 200 + '","description":"## W\\nx"}')
    try:
        out = _run(C._name_mr("g", "/tmp/wt"))
        assert len(out["title"]) <= 72
        assert len(out["branch"]) <= 40
    finally:
        restore()


def test_name_mr_none_on_empty_diff():
    restore = _patch(_mr_diff=lambda wt, target="main": "   ")
    try:
        assert _run(C._name_mr("g", "/tmp/wt")) is None
    finally:
        restore()


def test_name_mr_none_on_bad_json():
    restore = _stub_namer("sorry, I cannot produce JSON")
    try:
        assert _run(C._name_mr("g", "/tmp/wt")) is None   # _extract_json raises → None
    finally:
        restore()


def test_name_mr_none_on_missing_field():
    # a result missing a required key must fall back (None), not ship a half-named MR
    restore = _stub_namer('{"title":"feat: x","branch":"feat-x"}')   # no description
    try:
        assert _run(C._name_mr("g", "/tmp/wt")) is None
    finally:
        restore()


# ── _open_mr: pushes to the clean mr_branch, opens MR from it ───────────────────
def test_open_mr_uses_clean_mr_branch():
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args[:3] == ["glab", "mr", "list"]:
            return _R(0, "[]")           # no existing MR
        return _R(0, "https://gitlab.example/mr/1")

    restore = _patch(subprocess=types.SimpleNamespace(run=fake_run),
                     REPORTING={"mr": {"target": "main"}})
    try:
        b = {"worktree": "/tmp/wt", "branch": "conductor-ugly-goal-slug", "mr_branch": "feat-overlay-sdk"}
        C._open_mr(b, "feat(overlay): add overlay SDK", "## What\nx", draft=False)
        push = next(a for a in calls if a[:3] == ["git", "-C", "/tmp/wt"] or ("push" in a))
        # working branch pushed to the CLEAN ref
        assert any("conductor-ugly-goal-slug:refs/heads/feat-overlay-sdk" in " ".join(a) for a in calls), calls
        # MR opened from the clean source branch
        create = next(a for a in calls if a[:3] == ["glab", "mr", "create"])
        assert "feat-overlay-sdk" in create and "conductor-ugly-goal-slug" not in create
    finally:
        restore()


def test_open_mr_falls_back_to_working_branch_when_no_mr_branch():
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args[:3] == ["glab", "mr", "list"]:
            return _R(0, "[]")
        return _R(0, "https://gitlab.example/mr/2")

    restore = _patch(subprocess=types.SimpleNamespace(run=fake_run),
                     REPORTING={"mr": {"target": "main"}})
    try:
        b = {"worktree": "/tmp/wt", "branch": "fc-1-real-branch"}   # no mr_branch
        C._open_mr(b, "t", "d")
        create = next(a for a in calls if a[:3] == ["glab", "mr", "create"])
        assert "fc-1-real-branch" in create
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
