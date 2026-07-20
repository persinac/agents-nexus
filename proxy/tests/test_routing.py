"""
Offline tests for nexus-proxy routing + resilience. Dev-only (pytest is NOT a
runtime dependency and is not copied into the container). No real Anthropic and
no live proxy: pure functions are tested directly; the httpx orchestration is
driven by an in-process httpx.MockTransport.

Run:  cd proxy && . .venv-test/bin/activate && pytest -q
"""

import json
import os

os.environ.setdefault("ANTHROPIC_API_BASE", "http://work.invalid")
os.environ.setdefault("ROUTE_ENABLED", "1")

import httpx
import pytest
from fastapi import Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import routing
import main


POOL = [
    routing.Model("claude-haiku-4-5", "haiku", 1.0),
    routing.Model("claude-sonnet-5", "sonnet", 3.0),
    routing.Model("claude-opus-4-8", "opus", 15.0),
]
TRIVIAL = frozenset({"trivial"})


# ── pure: difficulty ────────────────────────────────────────────────────────

def test_trivial_turn_with_full_tools_still_trivial():
    """Guards spec defect #2: a huge tools array must NOT lift difficulty — CC
    resends its full tool set every turn, so tools can't discriminate."""
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1024,
        "tools": [{"name": f"tool_{i}", "description": "x" * 400} for i in range(60)],
    }
    assert routing.classify_difficulty(body) == "trivial"


def test_thinking_and_big_context_are_hard():
    assert routing.classify_difficulty({"messages": [{"role": "user", "content": "x"}],
                                        "thinking": {"type": "enabled"}}) == "hard"
    big = {"messages": [{"role": "user", "content": "x" * 200_000}]}
    assert routing.classify_difficulty(big) == "hard"


def test_mid_size_is_normal():
    body = {"messages": [{"role": "user", "content": "moderate question"}], "max_tokens": 4096}
    assert routing.classify_difficulty(body) == "normal"


# ── pure: selection / shed ──────────────────────────────────────────────────

def test_trivial_downgrades_within_anthropic_only():
    cd = routing.Cooldowns()
    served = routing.select_model("claude-opus-4-8", "trivial", POOL, cd, TRIVIAL, 0.0)
    assert served == "claude-haiku-4-5"
    # never crosses vendor: the served id is always an Anthropic pool member
    assert served in {m.model for m in POOL}


def test_normal_and_hard_keep_requested():
    cd = routing.Cooldowns()
    assert routing.select_model("claude-opus-4-8", "normal", POOL, cd, TRIVIAL, 0.0) == "claude-opus-4-8"
    assert routing.select_model("claude-opus-4-8", "hard", POOL, cd, TRIVIAL, 0.0) == "claude-opus-4-8"


def test_unknown_model_passes_through():
    cd = routing.Cooldowns()
    assert routing.select_model("gpt-4o-mini", "trivial", POOL, cd, TRIVIAL, 0.0) == "gpt-4o-mini"


def test_never_upgrades_from_haiku():
    cd = routing.Cooldowns()
    assert routing.select_model("claude-haiku-4-5", "trivial", POOL, cd, TRIVIAL, 0.0) == "claude-haiku-4-5"


def test_selection_skips_cooled_down_model():
    cd = routing.Cooldowns(threshold=2, window=100)
    cd.record("claude-haiku-4-5", 429, 0.0)
    cd.record("claude-haiku-4-5", 429, 0.0)  # trips cooldown
    served = routing.select_model("claude-opus-4-8", "trivial", POOL, cd, TRIVIAL, 1.0)
    assert served == "claude-sonnet-5"  # haiku cooled → next cheapest


def test_shed_walks_down_then_stops():
    cd = routing.Cooldowns()
    assert routing.shed_model("claude-opus-4-8", POOL, cd, 0.0) == "claude-sonnet-5"
    assert routing.shed_model("claude-sonnet-5", POOL, cd, 0.0) == "claude-haiku-4-5"
    assert routing.shed_model("claude-haiku-4-5", POOL, cd, 0.0) is None


# ── pure: backoff + cooldowns ───────────────────────────────────────────────

def test_backoff_honors_retry_after_and_caps():
    assert routing.backoff_delays(0, "2.5") == 2.5
    assert routing.backoff_delays(50) == 8.0            # capped
    assert 0.5 <= routing.backoff_delays(0) <= 1.0      # base + jitter, attempt 0


def test_cooldown_threshold_trips_then_expires():
    cd = routing.Cooldowns(threshold=2, window=10)
    cd.record("m", 500, 0.0)
    assert cd.active(0.0) == set()      # one hit, not tripped
    cd.record("m", 500, 1.0)
    assert "m" in cd.active(1.0)        # second hit within window → cooled
    assert cd.active(100.0) == set()    # window elapsed


# ── pure: request shaping (kill switch / cache preservation) ────────────────

def test_body_for_model_is_byte_identical_when_unchanged():
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}],
            "system": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}]}
    raw = json.dumps(body).encode()
    # unchanged model → original bytes returned verbatim (preserves prompt cache)
    assert main._body_for_model("claude-opus-4-8", "claude-opus-4-8", body, raw) is raw
    # rewrite → only `model` changes; system/messages preserved
    out = json.loads(main._body_for_model("claude-haiku-4-5", "claude-opus-4-8", body, raw))
    assert out["model"] == "claude-haiku-4-5"
    assert out["system"] == body["system"] and out["messages"] == body["messages"]


def test_kill_switch_disables_downgrade(monkeypatch):
    """Spec verification #3: ROUTE_ENABLED=0 → the requested model is served
    unchanged even for a trivial turn, so the outbound body stays byte-identical."""
    monkeypatch.setattr(main, "_COOLDOWNS", routing.Cooldowns())
    monkeypatch.setattr(main, "ROUTE_ENABLED", False)
    trivial = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    served, difficulty = main._decide_served(True, None, trivial, "claude-opus-4-8")
    assert served == "claude-opus-4-8" and difficulty == "n/a"


def test_route_enabled_downgrades_trivial_but_not_work(monkeypatch):
    monkeypatch.setattr(main, "_COOLDOWNS", routing.Cooldowns())
    monkeypatch.setattr(main, "ROUTE_ENABLED", True)
    trivial = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    served, difficulty = main._decide_served(True, None, trivial, "claude-opus-4-8")
    assert difficulty == "trivial" and served == "claude-haiku-4-5"
    # work sessions are never routed, even on a trivial turn
    w_served, w_diff = main._decide_served(True, "work-acme", trivial, "claude-opus-4-8")
    assert w_served == "claude-opus-4-8" and w_diff == "n/a"


# ── admin: hot-reload ROUTE_ENABLED without a restart ───────────────────────

def test_admin_route_toggles_enabled_live(monkeypatch):
    monkeypatch.setattr(main, "ROUTE_ENABLED", False)
    monkeypatch.setattr(main, "ROUTE_ADMIN_TOKEN", "")  # open on a localhost box
    tc = TestClient(main.app)

    # GET reflects current state (and is NOT proxied upstream — no upstream call)
    assert tc.get("/admin/route").json()["enabled"] is False
    # POST flips the live module global that _decide_served reads
    r = tc.post("/admin/route", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert main.ROUTE_ENABLED is True
    trivial = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    assert main._decide_served(True, None, trivial, "claude-opus-4-8")[0] == "claude-haiku-4-5"
    # and back off again
    assert tc.post("/admin/route", json={"enabled": False}).json()["enabled"] is False
    assert main._decide_served(True, None, trivial, "claude-opus-4-8")[0] == "claude-opus-4-8"


def test_admin_route_token_guard(monkeypatch):
    monkeypatch.setattr(main, "ROUTE_ADMIN_TOKEN", "s3cret")
    monkeypatch.setattr(main, "ROUTE_ENABLED", False)
    tc = TestClient(main.app)
    assert tc.post("/admin/route", json={"enabled": True}).status_code == 403
    assert main.ROUTE_ENABLED is False  # unchanged
    ok = tc.post("/admin/route", json={"enabled": True}, headers={"x-route-admin-token": "s3cret"})
    assert ok.status_code == 200 and main.ROUTE_ENABLED is True


# ── orchestration (httpx.MockTransport, no network) ─────────────────────────

@pytest.fixture
def orch(monkeypatch):
    """Wire main.* to an in-process mock upstream and zero out sleeps."""
    monkeypatch.setattr(main, "PERSONAL_UPSTREAM", "http://up")
    monkeypatch.setattr(main, "WORK_UPSTREAM", "http://work")
    monkeypatch.setattr(main, "ROUTE_MAX_RETRIES", 2)
    monkeypatch.setattr(main, "_COOLDOWNS", routing.Cooldowns())  # isolate per test
    monkeypatch.setattr(routing, "backoff_delays", lambda *a, **k: 0.0)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(main, "_log_generation", _noop)
    monkeypatch.setattr(main, "_log_stream", _noop)

    def install(handler):
        monkeypatch.setattr(main, "client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return install


def _scripted(statuses, stream_body=None):
    """Handler returning the given status sequence; a 200 streams stream_body as
    an async SSE body (AsyncClient requires an async stream). Counts calls."""
    calls = {"n": 0}
    chunks = stream_body or [b"event: message_stop\ndata: {}\n\n"]

    async def _ok_body():
        for c in chunks:
            yield c

    def handler(request):
        i = calls["n"]
        calls["n"] += 1
        status = statuses[min(i, len(statuses) - 1)]
        if status == 200:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=_ok_body())
        return httpx.Response(status, json={"type": "error", "error": {"message": "boom"}})
    return handler, calls


async def _drain(streaming_response):
    out = b""
    async for chunk in streaming_response.body_iterator:
        out += chunk if isinstance(chunk, bytes) else chunk.encode()
    return out


@pytest.mark.asyncio
async def test_nonstream_529_529_200_retries_twice(orch):
    handler, calls = _scripted([529, 529, 200])
    orch(handler)
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    res = await main._nonstream_response(
        "POST", "v1/messages", raw, body, {}, {}, 0.0, None,
        True, "claude-opus-4-8", "claude-opus-4-8", "normal",
    )
    assert isinstance(res, Response) and res.status_code == 200
    assert calls["n"] == 3  # 1 + 2 retries


@pytest.mark.asyncio
async def test_stream_429_then_200_clean_cutover(orch):
    handler, calls = _scripted([429, 200])
    orch(handler)
    body = {"model": "claude-opus-4-8", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    res = await main._stream_response(
        "v1/messages", raw, body, {}, {}, 0.0, None,
        "claude-opus-4-8", "claude-opus-4-8", "normal",
    )
    # a committed 200 stream — never a torn early stream
    assert isinstance(res, StreamingResponse)
    out = await _drain(res)
    assert b"message_stop" in out
    assert calls["n"] == 2  # retried once, then 200


@pytest.mark.asyncio
async def test_stream_persistent_429_surfaces_real_http_status(orch):
    """Guards spec defect #1: a streaming 429 the proxy can't recover from must
    reach Claude Code as a real HTTP 429 (its backoff works), NOT a 200 SSE error."""
    handler, calls = _scripted([429])  # always 429
    orch(handler)
    body = {"model": "claude-opus-4-8", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    res = await main._stream_response(
        "v1/messages", raw, body, {}, {}, 0.0, None,
        "claude-opus-4-8", "claude-opus-4-8", "normal",
    )
    assert isinstance(res, Response) and not isinstance(res, StreamingResponse)
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_stream_midstream_drop_is_not_retried(orch):
    async def _drop():
        yield b"event: message_start\ndata: {}\n\n"
        raise httpx.RemoteProtocolError("peer dropped mid-stream")

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_drop())
    orch(handler)

    body = {"model": "claude-opus-4-8", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    res = await main._stream_response(
        "v1/messages", raw, body, {}, {}, 0.0, None,
        "claude-opus-4-8", "claude-opus-4-8", "normal",
    )
    assert isinstance(res, StreamingResponse)
    out = await _drain(res)
    assert b"message_start" in out and b"event: error" in out  # committed, then in-band error
    assert calls["n"] == 1  # NO retry after the first byte


@pytest.mark.asyncio
async def test_work_session_hardfails_no_bypass_and_no_shed(orch):
    handler, calls = _scripted([503])  # always 503
    orch(handler)
    body = {"model": "claude-opus-4-8", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    res = await main._stream_response(
        "v1/messages", raw, body, {}, {}, 0.0, "work-acme",
        "claude-opus-4-8", "claude-opus-4-8", "n/a",
    )
    assert isinstance(res, Response) and res.status_code == 503
    # work never sheds to another model: 1 + ROUTE_MAX_RETRIES attempts, same model
    assert calls["n"] == 1 + main.ROUTE_MAX_RETRIES
