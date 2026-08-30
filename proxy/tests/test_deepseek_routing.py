"""
Offline tests for the DeepSeek cost leg (docs/deepseek-routing.md). Same
conventions as test_routing.py: pure functions tested directly, orchestration
driven by an in-process httpx.MockTransport — no real network, no litellm.

These guard the design's two core claims:
  1. A `ds-` session is decided BEFORE anything else in _decide_served/
     _is_classifier_call, so routing.py's Anthropic-only pool never runs for it.
  2. A `ds-` session never falls back to Anthropic on failure — no shed, no
     classifier-revert, no work-style hard-fail bypass.

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

import routing
import main

from test_routing import orch, _scripted, _drain  # reuse the established fixtures


# ── kill switch: DEEPSEEK_ENABLED defaults to inert ─────────────────────────

def test_deepseek_disabled_by_default_ignores_prefix(monkeypatch):
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", False)
    assert main._is_deepseek("ds-anything") is False
    assert main._upstream_for("ds-anything") == main.PERSONAL_UPSTREAM


def test_deepseek_enabled_requires_the_prefix(monkeypatch):
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    assert main._is_deepseek("ds-mission-1") is True
    assert main._is_deepseek("mission-1") is False
    assert main._is_deepseek(None) is False


def test_upstream_for_deepseek_beats_work(monkeypatch):
    """A session can't be both — but if a name somehow carried both prefixes,
    deepseek wins (checked first), since it's the more specific opt-in."""
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    monkeypatch.setattr(main, "LITELLM_UPSTREAM", "http://litellm.invalid")
    monkeypatch.setattr(main, "WORK_UPSTREAM", "http://work.invalid")
    assert main._upstream_for("ds-work-thing") == "http://litellm.invalid"
    assert main._upstream_for("work-thing") == "http://work.invalid"


# ── _decide_served: deepseek short-circuits everything else ────────────────

CLASSIFIER_BODY = {
    "model": "claude-opus-4-8",
    "system": "<cc_automode_permissions>\nrules\n</cc_automode_permissions>",
    "messages": [{"role": "user", "content": "=== ACTION BEING CLASSIFIED ===\nBash: ls"}],
}


def test_decide_served_forces_deepseek_model_unconditionally(monkeypatch):
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    monkeypatch.setattr(main, "DEEPSEEK_MODEL", "deepseek-cheap")
    monkeypatch.setattr(main, "ROUTE_ENABLED", True)  # would otherwise also apply
    served, difficulty = main._decide_served(
        True, "ds-mission-1", CLASSIFIER_BODY, "claude-opus-4-8", is_classifier=True,
    )
    assert (served, difficulty) == ("deepseek-cheap", "vendor-route")


def test_decide_served_ignores_bg_ceiling_for_deepseek_sessions(monkeypatch):
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    monkeypatch.setattr(main, "DEEPSEEK_MODEL", "deepseek-cheap")
    monkeypatch.setattr(main, "BG_CEILING_ENABLED", True)
    monkeypatch.setattr(main, "BG_CEILING_MODEL", "claude-sonnet-5")
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}
    served, difficulty = main._decide_served(True, "ds-bg-thing", body, "claude-opus-4-8")
    assert (served, difficulty) == ("deepseek-cheap", "vendor-route")


def test_is_classifier_call_false_for_deepseek_even_with_classifier_body(monkeypatch):
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    body_bytes = json.dumps(CLASSIFIER_BODY).encode()
    assert main._is_classifier_call(True, "ds-mission-1", body_bytes) is False
    # sanity: the same body IS a classifier call on a normal session
    assert main._is_classifier_call(True, "mission-1", body_bytes) is True


# ── orchestration: header stripping, forced model, no-fallback ─────────────

def _captured(status=200, stream_body=None):
    """Handler recording the request it received (url + headers) and always
    answering with `status` (200 streams stream_body as SSE)."""
    seen = {}
    chunks = stream_body or [b"event: message_stop\ndata: {}\n\n"]

    async def _ok_body():
        for c in chunks:
            yield c

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        if status == 200:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=_ok_body())
        return httpx.Response(status, json={"type": "error", "error": {"message": "boom"}})
    return handler, seen


@pytest.mark.asyncio
async def test_nonstream_deepseek_strips_auth_and_hits_litellm(orch, monkeypatch):
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    monkeypatch.setattr(main, "LITELLM_UPSTREAM", "http://litellm")
    handler, seen = _captured()
    orch(handler)
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    headers = {"authorization": "Bearer sk-ant-should-not-leak", "x-api-key": "sk-ant-also-not"}
    res = await main._nonstream_response(
        "POST", "v1/messages", raw, body, headers, {}, 0.0, "ds-mission-1",
        True, "claude-opus-4-8", "deepseek-cheap", "vendor-route",
    )
    assert isinstance(res, Response) and res.status_code == 200
    assert seen["url"].startswith("http://litellm/")
    assert "authorization" not in seen["headers"]
    assert "x-api-key" not in seen["headers"]


@pytest.mark.asyncio
async def test_stream_deepseek_strips_auth_and_hits_litellm(orch, monkeypatch):
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    monkeypatch.setattr(main, "LITELLM_UPSTREAM", "http://litellm")
    handler, seen = _captured()
    orch(handler)
    body = {"model": "claude-opus-4-8", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    headers = {"authorization": "Bearer sk-ant-should-not-leak"}
    res = await main._stream_response(
        "v1/messages", raw, body, headers, {}, 0.0, "ds-mission-1",
        "claude-opus-4-8", "deepseek-cheap", "vendor-route",
    )
    assert isinstance(res, StreamingResponse)
    out = await _drain(res)
    assert b"message_stop" in out
    assert seen["url"].startswith("http://litellm/")
    assert "authorization" not in seen["headers"]


@pytest.mark.asyncio
async def test_nonstream_deepseek_failure_does_not_fall_back_to_anthropic(orch, monkeypatch):
    """529/exhausted-retries on litellm must surface as an error — never shed
    down main's Anthropic pool, never retry against PERSONAL_UPSTREAM/WORK_UPSTREAM."""
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    monkeypatch.setattr(main, "LITELLM_UPSTREAM", "http://litellm")
    monkeypatch.setattr(main, "ROUTE_MAX_RETRIES", 1)
    handler, calls = _scripted([529, 529, 529])  # always retryable-failing
    orch(handler)
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    res = await main._nonstream_response(
        "POST", "v1/messages", raw, body, {}, {}, 0.0, "ds-mission-1",
        True, "claude-opus-4-8", "deepseek-cheap", "vendor-route",
    )
    assert isinstance(res, Response) and not isinstance(res, StreamingResponse)
    # surfaced the real 529 verbatim, no shed attempt (calls == retries, not more)
    assert res.status_code == 529
    assert calls["n"] == 2  # 1 + ROUTE_MAX_RETRIES(1), then break — no shed round


@pytest.mark.asyncio
async def test_stream_deepseek_persistent_failure_surfaces_verbatim(orch, monkeypatch):
    monkeypatch.setattr(main, "DEEPSEEK_ENABLED", True)
    monkeypatch.setattr(main, "LITELLM_UPSTREAM", "http://litellm")
    monkeypatch.setattr(main, "ROUTE_MAX_RETRIES", 1)
    handler, calls = _scripted([503, 503, 503])
    orch(handler)
    body = {"model": "claude-opus-4-8", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    res = await main._stream_response(
        "v1/messages", raw, body, {}, {}, 0.0, "ds-mission-1",
        "claude-opus-4-8", "deepseek-cheap", "vendor-route",
    )
    assert isinstance(res, Response) and not isinstance(res, StreamingResponse)
    assert res.status_code == 503
    assert calls["n"] == 2  # no shed round attempted
