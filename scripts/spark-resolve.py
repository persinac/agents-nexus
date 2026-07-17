#!/usr/bin/env python3
"""spark-resolve.py — resolve a natural-language request to the repo it concerns.

Used by the slack-bridge orchestrator spawn branch: given an inbound Slack
message, ask the running Spark MCP service "which repo is this about?" and return
the top match as JSON on stdout:

    {"repo": "store-front", "score": 0.42}
    {"repo": null, "reason": "no results"}          # nothing matched
    {"repo": null, "error": "connect: ..."}         # service down / timeout

It talks to the LIVE Spark service over MCP-over-SSE (default
http://localhost:8343/sse) rather than re-querying any local index — the index
lives in the Spark Docker service, so the running service is the only source of
truth. Must be run with a Python that has the `mcp` SDK (the spark venv:
spark/.venv/bin/python); the bridge invokes it with that interpreter.

This NEVER raises to the caller: any failure (service down, timeout, bad output)
prints a {"repo": null, ...} object and exits 0, so the bridge degrades to its
usual usage hint instead of crashing.

Usage:
    spark-resolve.py "the request text"        # or pass text on stdin
Env:
    SPARK_SSE_URL   default http://localhost:8343/sse
    SPARK_TOP_K     default 3
    SPARK_TIMEOUT   seconds, default 20
"""
import asyncio
import json
import os
import re
import sys

SSE_URL = os.environ.get("SPARK_SSE_URL", "http://localhost:8343/sse")
TOP_K = int(os.environ.get("SPARK_TOP_K", "3"))
TIMEOUT = float(os.environ.get("SPARK_TIMEOUT", "20"))

# Each Spark result block starts with: "### [1] <repo> (score: 0.123)"
_HEADER = re.compile(r"^###\s*\[\d+\]\s*(.+?)\s*\(score:\s*([0-9.]+)\)\s*$", re.MULTILINE)


def _emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _parse_top(text):
    """Return (repo, score) of the first result block, or (None, None)."""
    if not text:
        return None, None
    m = _HEADER.search(text)
    if not m:
        return None, None
    return m.group(1).strip(), float(m.group(2))


def _extract_text(result):
    """Pull the text payload out of an MCP call_tool result across SDK shapes."""
    # Structured content (newer SDKs) may carry the raw string directly.
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    chunks = []
    for item in (content or []):
        t = getattr(item, "text", None)
        if t is None and isinstance(item, dict):
            t = item.get("text")
        if t:
            chunks.append(t)
    raw = "\n".join(chunks)
    # The spark tool returns JSON like {"result": "### [1] ..."} — unwrap if so.
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "result" in obj:
            return obj["result"]
    except (ValueError, TypeError):
        pass
    return raw


async def _resolve(query):
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("spark", {"query": query, "top_k": TOP_K})
    return _extract_text(result)


def main():
    query = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not query:
        _emit({"repo": None, "error": "empty query"})
        return 0
    try:
        text = asyncio.run(asyncio.wait_for(_resolve(query), timeout=TIMEOUT))
    except asyncio.TimeoutError:
        _emit({"repo": None, "error": f"timeout after {TIMEOUT}s"})
        return 0
    except BaseException as e:  # connection refused, SDK import, protocol error, ...
        # asyncio TaskGroups wrap failures in an ExceptionGroup — drill to a leaf
        # so the bridge logs "ConnectionRefusedError" not "ExceptionGroup".
        leaf = e
        while leaf is not None and hasattr(leaf, "exceptions") and getattr(leaf, "exceptions"):
            leaf = leaf.exceptions[0]
        _emit({"repo": None, "error": f"{type(leaf).__name__}: {leaf}"})
        return 0
    repo, score = _parse_top(text)
    if repo is None:
        _emit({"repo": None, "reason": "no results"})
    else:
        _emit({"repo": repo, "score": score})
    return 0


if __name__ == "__main__":
    sys.exit(main())
