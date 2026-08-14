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


# ── auto-mode classifier carve-out ──────────────────────────────────────────
# Claude Code fails CLOSED when a permission-classifier call errors: the tool call
# is denied, not retried and not re-prompted. These guard that the proxy keeps
# those calls off the session model and owns their transient failures.

CLASSIFIER_BODY = {
    "model": "claude-opus-4-8",
    "system": "<cc_automode_permissions>\nrules\n</cc_automode_permissions>",
    "messages": [{"role": "user", "content": "=== ACTION BEING CLASSIFIED ===\nBash: ls"}],
}


def test_classifier_detected_by_either_marker():
    perms = json.dumps({"system": "<cc_automode_permissions>x"}).encode()
    action = json.dumps({"messages": [{"content": "=== ACTION BEING CLASSIFIED ==="}]}).encode()
    assert routing.is_classifier_request(perms)
    assert routing.is_classifier_request(action)
    # an ordinary turn is not a classification, and empty bytes never raise
    ordinary = json.dumps({"messages": [{"role": "user", "content": "run the tests"}]}).encode()
    assert not routing.is_classifier_request(ordinary)
    assert not routing.is_classifier_request(b"")


def test_classifier_pinned_off_requested_model_at_floor_tier():
    cd = routing.Cooldowns()
    # default floor is sonnet — cheapest tier >= sonnet that is <= opus in cost
    assert routing.select_classifier_model(
        "claude-opus-4-8", CLASSIFIER_BODY, POOL, cd, 0.0) == "claude-sonnet-5"
    # an explicit haiku floor goes all the way down
    assert routing.select_classifier_model(
        "claude-opus-4-8", CLASSIFIER_BODY, POOL, cd, 0.0, floor_tier="haiku") == "claude-haiku-4-5"


def test_classifier_never_upgrades_and_ignores_unknown_models():
    cd = routing.Cooldowns()
    # requested is already below the floor → left alone, never lifted to sonnet
    assert routing.select_classifier_model(
        "claude-haiku-4-5", CLASSIFIER_BODY, POOL, cd, 0.0) == "claude-haiku-4-5"
    assert routing.select_classifier_model(
        "gpt-4o-mini", CLASSIFIER_BODY, POOL, cd, 0.0) == "gpt-4o-mini"


def test_classifier_oversized_transcript_keeps_requested_model():
    """A classifier transcript too large for the cheaper model must NOT be
    downgraded: a 400 for exceeding the context window denies the tool call
    permanently, which is worse than the transient failure being avoided."""
    cd = routing.Cooldowns()
    assert routing.select_classifier_model(
        "claude-opus-4-8", CLASSIFIER_BODY, POOL, cd, 0.0, max_cheap_tokens=1) == "claude-opus-4-8"


def _classifier_body_of(est_tokens):
    """A classifier body whose _estimate_input_tokens lands near est_tokens."""
    body = dict(CLASSIFIER_BODY)
    body["messages"] = [{"role": "user", "content": "=== ACTION BEING CLASSIFIED ===\n"
                                                    + "x" * (est_tokens * 4)}]
    return body


def test_classifier_183k_transcript_routes_to_sonnet():
    """Regression for the live failure: a real stage-2 call estimated 183,250
    tokens, and the original single 180k cap pinned it back onto the unavailable
    session model. Sonnet holds that comfortably, so it must be used."""
    cd = routing.Cooldowns()
    body = _classifier_body_of(183_250)
    assert routing.select_classifier_model(
        "claude-opus-5", body, POOL, cd, 0.0) == "claude-sonnet-5"


def test_classifier_skips_a_tier_that_cannot_fit():
    """Above haiku's window the guard skips haiku but still downgrades to sonnet,
    rather than giving up and keeping the requested model."""
    cd = routing.Cooldowns()
    body = _classifier_body_of(225_000)
    assert routing.select_classifier_model(
        "claude-opus-5", body, POOL, cd, 0.0, floor_tier="haiku") == "claude-sonnet-5"


def test_classifier_keeps_requested_when_no_tier_fits(monkeypatch):
    monkeypatch.setattr(routing, "TIER_CONTEXT", {"haiku": 1, "sonnet": 1, "opus": 1})
    cd = routing.Cooldowns()
    assert routing.select_classifier_model(
        "claude-opus-5", _classifier_body_of(50_000), POOL, cd, 0.0) == "claude-opus-5"


def test_classifier_skips_cooled_down_model():
    cd = routing.Cooldowns(threshold=2, window=100)
    cd.record("claude-sonnet-5", 529, 0.0)
    cd.record("claude-sonnet-5", 529, 0.0)  # trips cooldown
    served = routing.select_classifier_model(
        "claude-opus-4-8", CLASSIFIER_BODY, POOL, cd, 1.0, floor_tier="haiku")
    assert served == "claude-haiku-4-5"


def test_classifier_routed_even_with_route_disabled(monkeypatch):
    """The carve-out is resilience, not cost: it fires with the cost router off."""
    monkeypatch.setattr(main, "_COOLDOWNS", routing.Cooldowns())
    monkeypatch.setattr(main, "ROUTE_ENABLED", False)
    served, difficulty = main._decide_served(
        True, None, CLASSIFIER_BODY, "claude-opus-4-8", True)
    assert difficulty == "classifier" and served != "claude-opus-4-8"


def test_classifier_call_detection_gates(monkeypatch):
    raw = json.dumps(CLASSIFIER_BODY).encode()
    monkeypatch.setattr(main, "CLASSIFIER_ROUTE_ENABLED", True)
    monkeypatch.setattr(main, "CLASSIFIER_ROUTE_WORK", True)
    assert main._is_classifier_call(True, None, raw)
    assert main._is_classifier_call(True, "work-acme", raw)     # work included by default
    assert not main._is_classifier_call(False, None, raw)       # non-/v1/messages
    monkeypatch.setattr(main, "CLASSIFIER_ROUTE_WORK", False)
    assert not main._is_classifier_call(True, "work-acme", raw)  # work exempted
    assert main._is_classifier_call(True, None, raw)             # personal still shielded
    monkeypatch.setattr(main, "CLASSIFIER_ROUTE_ENABLED", False)
    assert not main._is_classifier_call(True, None, raw)         # kill switch


def _recording(status_by_model):
    """Handler answering per requested model; records the models it was asked for."""
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        status = status_by_model.get(model, 200)
        if status == 200:
            return httpx.Response(200, json={"content": [], "usage": {}})
        return httpx.Response(status, json={"type": "error", "error": {"message": "boom"}})
    return handler, seen


@pytest.mark.asyncio
async def test_classifier_429_is_retried_not_surfaced(orch):
    """A surfaced 429 is a permission DENIAL, so the proxy owns it for classifier
    calls — unlike a main-loop turn, where it is passed through for CC's backoff."""
    handler, calls = _scripted([429, 429, 200])
    orch(handler)
    raw = json.dumps(CLASSIFIER_BODY).encode()
    res = await main._nonstream_response(
        "POST", "v1/messages", raw, dict(CLASSIFIER_BODY), {}, {}, 0.0, None,
        True, "claude-opus-4-8", "claude-opus-4-8", "classifier", True,
    )
    assert res.status_code == 200
    assert calls["n"] == 3

    # same script, ordinary turn → surfaced verbatim on the first 429, no retry
    handler2, calls2 = _scripted([429, 429, 200])
    orch(handler2)
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}
    res2 = await main._nonstream_response(
        "POST", "v1/messages", json.dumps(body).encode(), body, {}, {}, 0.0, None,
        True, "claude-opus-4-8", "claude-opus-4-8", "normal",
    )
    assert res2.status_code == 429 and calls2["n"] == 1


@pytest.mark.asyncio
async def test_classifier_400_reverts_to_requested_model(orch):
    """Belt for the size heuristic: if the cheaper model rejects the transcript,
    fall back to the model the client asked for rather than denying the call."""
    handler, seen = _recording({"claude-sonnet-5": 400, "claude-opus-4-8": 200})
    orch(handler)
    raw = json.dumps(CLASSIFIER_BODY).encode()
    res = await main._nonstream_response(
        "POST", "v1/messages", raw, dict(CLASSIFIER_BODY), {}, {}, 0.0, None,
        True, "claude-opus-4-8", "claude-sonnet-5", "classifier", True,
    )
    assert res.status_code == 200
    assert seen == ["claude-sonnet-5", "claude-opus-4-8"]


@pytest.mark.asyncio
async def test_classifier_may_shed_on_work_session(orch):
    """Work sessions never shed a main-loop turn, but a classifier call may: only
    body["model"] changes, never the upstream, so corp attribution is intact."""
    handler, seen = _recording({"claude-sonnet-5": 529, "claude-haiku-4-5": 200})
    orch(handler)
    raw = json.dumps(CLASSIFIER_BODY).encode()
    res = await main._nonstream_response(
        "POST", "v1/messages", raw, dict(CLASSIFIER_BODY), {}, {}, 0.0, "work-acme",
        True, "claude-opus-4-8", "claude-sonnet-5", "classifier", True,
    )
    assert res.status_code == 200
    assert seen[-1] == "claude-haiku-4-5"           # shed down-ladder after retries
    assert seen.count("claude-sonnet-5") == 1 + main.CLASSIFIER_MAX_RETRIES


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
