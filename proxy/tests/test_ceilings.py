"""
Offline tests for the per-session runaway ceilings. Dev-only, no network: the
pure accounting/gate functions are exercised directly, and the 403 refusal is
driven through TestClient (the gate short-circuits before any upstream call, so
no mock transport is needed for it).

Run:  cd proxy && /path/to/venv/bin/python -m pytest tests/ -q
"""

import json
import os

os.environ.setdefault("ANTHROPIC_API_BASE", "http://work.invalid")

import httpx
import pytest
from fastapi.testclient import TestClient

import main


PRICES = {"claude-opus-5": {"input": 5.0, "output": 25.0}}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Every test starts with an empty tally and no price table."""
    monkeypatch.setattr(main, "_SESSION_SPEND", {})
    monkeypatch.setattr(main, "_PRICES", {})
    monkeypatch.setattr(main, "_CACHE_READ_MULT", 0.10)
    monkeypatch.setattr(main, "_CACHE_WRITE_MULT", 1.25)


def _ud(inp=0, out=0, cr=0, cw=0):
    return {"input": inp, "output": out,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}


# ── cost arithmetic ──────────────────────────────────────────────────────────

def test_turn_usd_is_zero_without_a_price_table():
    """The proxy image has no volume mounts, so this is the DEFAULT state.
    No prices must mean no dollar figure — never a guessed one."""
    assert main._turn_usd("claude-opus-5", _ud(inp=1_000_000)) == 0.0


def test_turn_usd_matches_the_snapshot_formula(monkeypatch):
    monkeypatch.setattr(main, "_PRICES", PRICES)
    # 1M fresh in @ $5 + 1M cache-write @ 5*1.25 + 1M cache-read @ 5*0.10 + 1M out @ $25
    got = main._turn_usd("claude-opus-5", _ud(inp=1_000_000, cw=1_000_000,
                                              cr=1_000_000, out=1_000_000))
    assert got == pytest.approx(5.0 + 6.25 + 0.50 + 25.0)


def test_turn_usd_strips_a_dated_model_id(monkeypatch):
    monkeypatch.setattr(main, "_PRICES", {"claude-haiku-4-5": {"input": 1.0, "output": 5.0}})
    assert main._turn_usd("claude-haiku-4-5-20251001", _ud(out=1_000_000)) == pytest.approx(5.0)


def test_turn_usd_does_not_tier_fallback(monkeypatch):
    """claude-opus-4-8 must NOT price an unknown claude-opus-9. The report does
    substitute (loudly); the ceiling must not, or a mispriced model could hold a
    runaway under the cap."""
    monkeypatch.setattr(main, "_PRICES", {"claude-opus-4-8": {"input": 5.0, "output": 25.0}})
    assert main._turn_usd("claude-opus-9", _ud(out=1_000_000)) == 0.0


# ── accounting ───────────────────────────────────────────────────────────────

def test_account_usage_sums_all_four_buckets():
    main._account_usage("s", "m", _ud(inp=1, out=2, cr=4, cw=8))
    main._account_usage("s", "m", _ud(inp=16))
    acc = main._SESSION_SPEND["s"]
    assert acc["tokens"] == 31
    assert acc["turns"] == 2


def test_untagged_sessions_are_still_accounted():
    main._account_usage(None, "m", _ud(inp=5))
    assert main._SESSION_SPEND["(untagged)"]["tokens"] == 5


def test_sessions_are_tracked_independently():
    main._account_usage("a", "m", _ud(inp=10))
    main._account_usage("b", "m", _ud(inp=1))
    assert main._SESSION_SPEND["a"]["tokens"] == 10
    assert main._SESSION_SPEND["b"]["tokens"] == 1


# ── the gate ─────────────────────────────────────────────────────────────────

def test_unknown_session_is_never_breached():
    assert main._ceiling_breach("never-seen") is None


def test_under_the_cap_is_allowed(monkeypatch):
    monkeypatch.setattr(main, "CEILING_TOKENS", 100)
    main._account_usage("s", "m", _ud(inp=99))
    assert main._ceiling_breach("s") is None


def test_at_the_cap_breaches(monkeypatch):
    """>= not >: a session sitting exactly on the cap has already spent it."""
    monkeypatch.setattr(main, "CEILING_TOKENS", 100)
    main._account_usage("s", "m", _ud(inp=100))
    breach = main._ceiling_breach("s")
    assert breach and "token ceiling" in breach


def test_zero_cap_disables_the_check(monkeypatch):
    monkeypatch.setattr(main, "CEILING_TOKENS", 0)
    main._account_usage("s", "m", _ud(inp=10**12))
    assert main._ceiling_breach("s") is None


def test_work_and_personal_caps_are_independent(monkeypatch):
    monkeypatch.setattr(main, "CEILING_TOKENS", 10)          # personal: tight
    monkeypatch.setattr(main, "CEILING_TOKENS_WORK", 10_000)  # work: loose
    monkeypatch.setattr(main, "WORK_SESSION_PREFIX", "work-")
    main._account_usage("personal-thing", "m", _ud(inp=50))
    main._account_usage("work-thing", "m", _ud(inp=50))
    assert main._ceiling_breach("personal-thing") is not None
    # A work session must NOT inherit the personal cap — routing excludes work
    # sessions, and copying that gate here would exempt the only real cash.
    assert main._ceiling_breach("work-thing") is None


def test_usd_cap_is_skipped_without_prices(monkeypatch):
    """Default container state: tokens still guard, dollars silently don't."""
    monkeypatch.setattr(main, "CEILING_TOKENS", 0)   # take tokens out of play
    monkeypatch.setattr(main, "CEILING_USD", 0.01)
    main._account_usage("s", "claude-opus-5", _ud(out=10_000_000))
    assert main._ceiling_breach("s") is None


def test_usd_cap_fires_when_prices_are_loaded(monkeypatch):
    monkeypatch.setattr(main, "_PRICES", PRICES)
    monkeypatch.setattr(main, "CEILING_TOKENS", 0)
    monkeypatch.setattr(main, "CEILING_USD", 1.0)
    main._account_usage("s", "claude-opus-5", _ud(out=1_000_000))  # $25
    breach = main._ceiling_breach("s")
    assert breach and "cost ceiling" in breach


# ── the refusal ──────────────────────────────────────────────────────────────

def test_breached_session_gets_403_before_any_upstream_call(monkeypatch):
    """No mock transport installed on purpose: if the gate let this through it
    would try to reach the real upstream and this test would not return 403."""
    monkeypatch.setattr(main, "CEILING_TOKENS", 10)
    main._account_usage("hot", "claude-opus-5", _ud(inp=999))

    tc = TestClient(main.app)
    r = tc.post("/sess/hot/v1/messages",
                json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    err = r.json()
    assert err["type"] == "error"
    assert err["error"]["type"] == "permission_error"
    assert "token ceiling" in err["error"]["message"]


def test_healthy_session_is_not_refused(monkeypatch):
    """Complements the test above: proves the 403 came from the gate and not
    from something refusing every request."""
    monkeypatch.setattr(main, "CEILING_TOKENS", 10**9)
    monkeypatch.setattr(main, "PERSONAL_UPSTREAM", "http://up")

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(main, "_log_generation", _noop)

    def handler(request):
        return httpx.Response(200, json={"content": [], "usage": {}})
    monkeypatch.setattr(main, "client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    tc = TestClient(main.app)
    r = tc.post("/sess/cool/v1/messages",
                json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


def test_admin_ceilings_reports_caps_and_tallies(monkeypatch):
    monkeypatch.setattr(main, "CEILING_TOKENS", 123)
    main._account_usage("s", "m", _ud(inp=7))

    tc = TestClient(main.app)
    body = tc.get("/admin/ceilings").json()
    assert body["caps"]["personal"]["tokens"] == 123
    assert body["usd_enforced"] is False
    assert body["sessions"]["s"]["tokens"] == 7
    assert body["sessions"]["s"]["over"] is None
