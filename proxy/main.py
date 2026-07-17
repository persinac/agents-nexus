"""
Transparent Anthropic pass-through proxy with Langfuse tracing.

Sits between Claude Code and the configured upstream(s). Forwards every request
verbatim (all headers, including Authorization/x-api-key) and logs each
/v1/messages call to Langfuse as a generation for local usage tracking.
"""

import asyncio
import json
import logging
import os
import time
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from langfuse import get_client, propagate_attributes

log = logging.getLogger("proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Per-session upstream routing:
#   * WORK sessions (tagged `work-<repo>`) → the work/corporate gateway (WORK_UPSTREAM).
#   * Personal / untagged sessions → straight to Anthropic (subscription OAuth).
# Personal traffic must NEVER touch the work gateway (no corp-auth injection /
# attribution, and no dependency on the gateway being up). Both still flow through
# this proxy so Langfuse tracing is intact for every session.
WORK_UPSTREAM = os.environ["ANTHROPIC_API_BASE"].rstrip("/")
PERSONAL_UPSTREAM = os.environ.get("PERSONAL_UPSTREAM", "https://api.anthropic.com").rstrip("/")
# Legacy alias kept for any external reference; work is the historical default.
FALLBACK_UPSTREAM = PERSONAL_UPSTREAM
# Optional org-specific reconnect hint shown when the work gateway is down (e.g. a
# re-auth CLI). Empty in the public default; a private overlay supplies it via env.
WORK_RECONNECT_HINT = os.environ.get("WORK_UPSTREAM_RECONNECT_HINT", "").strip()
langfuse = get_client()

app = FastAPI()
# 600s read/write for long streaming turns, but a short 3s CONNECT timeout so a
# hung/unreachable work upstream fails fast (in seconds) instead of stalling every
# request on the TCP connect.
client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=3.0))


@app.on_event("shutdown")
async def _close_client() -> None:
    await client.aclose()
    langfuse.shutdown()

# Headers that must not be forwarded (HTTP/1.1 hop-by-hop + size management)
_HOP_BY_HOP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive"}


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}


def _response_headers(r: httpx.Response) -> dict[str, str]:
    skip = _HOP_BY_HOP | {"content-encoding"}
    return {k: v for k, v in r.headers.items() if k.lower() not in skip}


# ── Health checks ─────────────────────────────────────────────────────────────

@app.get("/health/liveliness")
@app.get("/health/readiness")
async def health():
    return {"status": "healthy"}


# ── Upstream routing policy ───────────────────────────────────────────────────

# Work sessions (tagged `work-<repo>` by the work launcher) route through the
# work gateway (WORK_UPSTREAM). If it is unreachable they HARD-FAIL — no silent
# bypass to direct Anthropic (compliance). Everything else (personal / untagged)
# routes straight to Anthropic on the subscription and never touches the work
# gateway, so there is nothing to "fall over" from.
WORK_SESSION_PREFIX = "work-"
WORK_HARD_FAIL_MSG = (
    "Work gateway unavailable and direct-Anthropic failover is disabled for "
    "work sessions (compliance)."
    + (f" Reconnect with: {WORK_RECONNECT_HINT}" if WORK_RECONNECT_HINT else "")
)


def _is_work(session_id: str | None) -> bool:
    return bool(session_id and session_id.startswith(WORK_SESSION_PREFIX))


def _upstream_for(session_id: str | None) -> str:
    """The single upstream this session talks to. No cross-routing/fallback:
    work → WORK_UPSTREAM, personal → Anthropic direct."""
    return WORK_UPSTREAM if _is_work(session_id) else PERSONAL_UPSTREAM


# ── Main proxy ────────────────────────────────────────────────────────────────

def _extract_session(path: str) -> tuple[str | None, str]:
    """Strip optional `sess/<name>/` prefix; return (session_id, real_path)."""
    if path.startswith("sess/"):
        rest = path[len("sess/"):]
        name, sep, real = rest.partition("/")
        if sep:
            return (name or None), real
    return None, path


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    body_bytes = await request.body()
    headers = _forward_headers(request)

    session_id, path = _extract_session(path)

    is_messages = path.rstrip("/") == "v1/messages"
    body: dict = {}
    if is_messages and body_bytes:
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError:
            pass

    is_stream = bool(body.get("stream"))
    t0 = time.monotonic()

    if is_stream:
        return _stream_response(request, path, body_bytes, body, headers, t0, session_id)

    upstream = _upstream_for(session_id)
    try:
        r = await client.request(
            request.method, f"{upstream}/{path}",
            content=body_bytes, headers=headers, params=dict(request.query_params),
        )
    except httpx.HTTPError as e:
        is_work = _is_work(session_id)
        msg = WORK_HARD_FAIL_MSG if is_work else str(e)
        log.warning(
            "upstream %s unreachable (%s /%s)%s: %s",
            upstream, request.method, path,
            " [work: hard-fail, no bypass]" if is_work else "",
            e,
        )
        return Response(
            content=json.dumps({"error": {"type": "upstream_unavailable", "message": msg}}).encode(),
            status_code=502,
            media_type="application/json",
        )
    if is_messages and r.status_code == 200:
        try:
            asyncio.create_task(_log_generation(body, r.json(), t0, session_id))
        except Exception:
            pass
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=_response_headers(r),
    )


def _stream_response(
    request: Request,
    path: str,
    body_bytes: bytes,
    body: dict,
    headers: dict,
    t0: float,
    session_id: str | None,
) -> StreamingResponse:
    chunks: list[bytes] = []

    upstream = _upstream_for(session_id)

    async def generate() -> AsyncGenerator[bytes, None]:
        primary = f"{upstream}/{path}"
        try:
            async with client.stream("POST", primary, content=body_bytes, headers=headers) as r:
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    yield chunk
        except httpx.HTTPError as e:
            # Single upstream per session — no cross-routing. If bytes already
            # streamed we can't recover; either way surface the error. Work
            # sessions get the compliance hint (never a silent Anthropic bypass).
            if _is_work(session_id) and not chunks:
                log.warning("upstream stream unreachable (%s) [work: hard-fail]: %s", primary, e)
                yield _stream_error(RuntimeError(WORK_HARD_FAIL_MSG))
                return
            log.warning("upstream stream error (%s): %s", primary, e)
            yield _stream_error(e)
            return
        asyncio.create_task(_log_stream(body, chunks, t0, session_id))

    return StreamingResponse(generate(), media_type="text/event-stream")


def _stream_error(e: Exception) -> bytes:
    err = json.dumps({"type": "error", "error": {"type": "upstream_unavailable", "message": str(e)}})
    return f"event: error\ndata: {err}\n\n".encode()


# ── Langfuse logging ─────────────────────────────────────────────────────────

def _summarize_blocks(content: list[dict]) -> dict:
    """Render assistant content blocks into a Langfuse-friendly summary."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_uses: list[dict] = []
    for block in content:
        t = block.get("type")
        if t == "text":
            text_parts.append(block.get("text", ""))
        elif t == "thinking":
            thinking_parts.append(block.get("thinking", ""))
        elif t == "tool_use":
            tool_uses.append({"name": block.get("name"), "input": block.get("input")})
    return {
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts),
        "tool_uses": tool_uses,
    }


def _pick_output(summary: dict) -> str | dict:
    """If only text is present, return the bare string; otherwise the dict."""
    if summary["text"] and not summary["thinking"] and not summary["tool_uses"]:
        return summary["text"]
    return summary


async def _log_generation(body: dict, response: dict, t0: float, session_id: str | None) -> None:
    try:
        usage = response.get("usage", {}) or {}
        summary = _summarize_blocks(response.get("content", []))
        output = _pick_output(summary)

        _emit_trace(
            name="messages",
            session_id=session_id,
            body=body,
            output=output,
            usage_details=_usage_details(usage),
            metadata={"latency_ms": round((time.monotonic() - t0) * 1000)},
        )
    except Exception as e:
        log.warning("langfuse log failed: %s", e)


def _usage_details(usage: dict) -> dict:
    """Map Anthropic's usage shape to Langfuse usage_details with cache buckets.

    Anthropic reports input_tokens excluding cached portions, plus separate
    cache_creation_input_tokens and cache_read_input_tokens. Langfuse prices
    each bucket independently when the model has matching keys configured.
    """
    return {
        "input": usage.get("input_tokens", 0) or 0,
        "output": usage.get("output_tokens", 0) or 0,
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
    }


async def _log_stream(body: dict, chunks: list[bytes], t0: float, session_id: str | None) -> None:
    try:
        raw = b"".join(chunks).decode("utf-8", errors="replace")
        usage_acc: dict = {}
        blocks: list[dict] = []
        current: dict | None = None
        json_buf: list[str] = []

        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload.strip() == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue

            ev_type = ev.get("type")
            if ev_type == "message_start":
                u = ev.get("message", {}).get("usage", {}) or {}
                _merge_usage(usage_acc, u)
            elif ev_type == "content_block_start":
                cb = ev.get("content_block", {})
                current = dict(cb)
                json_buf = []
            elif ev_type == "content_block_delta":
                d = ev.get("delta", {})
                dt = d.get("type")
                if current is None:
                    continue
                if dt == "text_delta":
                    current["text"] = current.get("text", "") + d.get("text", "")
                elif dt == "thinking_delta":
                    current["thinking"] = current.get("thinking", "") + d.get("thinking", "")
                elif dt == "input_json_delta":
                    json_buf.append(d.get("partial_json", ""))
            elif ev_type == "content_block_stop":
                if current is not None:
                    if current.get("type") == "tool_use":
                        raw_input = "".join(json_buf)
                        try:
                            current["input"] = json.loads(raw_input) if raw_input else {}
                        except json.JSONDecodeError:
                            current["input"] = raw_input
                    blocks.append(current)
                    current = None
                    json_buf = []
            elif ev_type == "message_delta":
                _merge_usage(usage_acc, ev.get("usage", {}) or {})

        summary = _summarize_blocks(blocks)
        output = _pick_output(summary)

        _emit_trace(
            name="messages-stream",
            session_id=session_id,
            body=body,
            output=output,
            usage_details=_usage_details(usage_acc),
            metadata={
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "stream": True,
            },
        )
    except Exception as e:
        log.warning("langfuse stream log failed: %s", e)


def _merge_usage(acc: dict, u: dict) -> None:
    """Last-write-wins for usage fields across stream events.

    Anthropic populates message_start.message.usage with input + cache totals
    (output_tokens=1 placeholder), then message_delta.usage with the final
    output_tokens (and may revise input/cache totals). Keep the latest non-null
    value per field so we end with the authoritative numbers.
    """
    for k in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = u.get(k)
        if v is not None:
            acc[k] = v


# Langfuse rejects whole items > ~2 MB (input + output + envelope). Cap each
# field at 750 KB so a worst-case input+output stays under ~1.5 MB; the SDK's
# placeholder ("<truncated due to size exceeding limit>") never kicks in.
LANGFUSE_FIELD_CAP = 750 * 1024
HEAD_BYTES = 450 * 1024
TAIL_BYTES = 300 * 1024
TRUNC_MARKER = "\n... [truncated by proxy] ...\n"


def _preview(value) -> str:
    """Render value as a string. If oversize, keep first 450 KB + last 300 KB."""
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    data = s.encode("utf-8")
    if len(data) <= LANGFUSE_FIELD_CAP:
        return s
    head = data[:HEAD_BYTES].decode("utf-8", errors="replace")
    tail = data[-TAIL_BYTES:].decode("utf-8", errors="replace")
    return f"{head}{TRUNC_MARKER}{tail}"


def _system_text(system) -> str | None:
    """Normalize Anthropic's top-level `system` field to text.

    It may be a plain string, or a list of content blocks
    (e.g. [{"type": "text", "text": ..., "cache_control": ...}]).
    Join the text of all text blocks; return None when absent/empty.
    """
    if not system:
        return None
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        text = "".join(
            b.get("text", "")
            for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return text or None
    return None


def _build_input(body: dict, session_id: str | None) -> list:
    """Langfuse input = the messages array, with the system prompt prepended
    as a synthetic role:'system' entry — but only for tagged requests.

    Anthropic carries the system prompt as a top-level `system` field rather
    than a message. We surface it as a role:'system' entry (logging only —
    never forwarded upstream) so it renders above the messages, but ONLY when
    the request opted in via a `sess/<name>/` prefix (session_id set). Untagged
    traffic (e.g. default Claude Code) omits it to avoid persisting a large,
    near-identical system prompt on every generation.
    """
    messages = body.get("messages") or []
    if session_id is None:
        return messages
    system = _system_text(body.get("system"))
    if system is not None:
        return [{"role": "system", "content": system}] + messages
    return messages


def _emit_trace(
    *,
    name: str,
    session_id: str | None,
    body: dict,
    output: str | dict,
    usage_details: dict,
    metadata: dict,
) -> None:
    """Emit a standalone generation observation (becomes its own trace root)."""
    input_preview = _preview(_build_input(body, session_id))
    output_preview = _preview(output)
    trace_name = session_id or "claude-code"
    with propagate_attributes(session_id=session_id, trace_name=trace_name):
        gen = langfuse.start_observation(
            as_type="generation",
            name=name,
            model=body.get("model"),
            input=input_preview,
            output=output_preview,
            usage_details=usage_details,
            metadata=metadata,
        )
        gen.set_trace_io(input=input_preview, output=output_preview)
        gen.end()
    langfuse.flush()
