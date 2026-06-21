#!/usr/bin/env python3
"""spark-summary.py — derive concise repo descriptions from the Spark index.

The Slack orchestrator's spawn classifier picks which spawnable repo a message
concerns by matching the message against each repo's NAME + DESCRIPTION (see
slack-bridge/orchestrator.js `classifyRepoForSpawn`). Hand-writing those
descriptions in ~/.tmux/spawnable-repos.json works but goes stale. This script
auto-derives a one-paragraph description for each spawnable repo from the LIVE
Spark service's `installation_summary` (which the nightly Spark sync keeps
current) and writes them to a cache the bridge merges in:

    ~/.tmux/spark-summaries.json   { "<repo>": "<description>", ... }

Merge semantics (in the bridge): a hand-written `desc` in the allowlist ALWAYS
wins; this cache only fills repos that have none. So the workflow becomes "add a
repo to the allowlist (path only); its description fills itself in" — with the
hand-written field still available as an override.

The raw `installation_summary` is a long monitor-log (repeated headers + the
repo's whole CLAUDE.md). We pull just a concise blurb from it, preferring the
CLAUDE.md "Project Overview" section, then the first real paragraph, then a
structured framework/deploy fallback.

It talks to the live Spark service over MCP-over-SSE (same transport as
spark-resolve.py) and must run under a Python with the `mcp` SDK — the spark
venv: spark/.venv/bin/python. It NEVER raises to the caller: any failure for a
given repo is skipped (that repo keeps whatever desc it already had), and a
total failure writes nothing and exits 0, so a nightly/timer run is fail-open.

Usage:
    spark-summary.py [--allowlist FILE] [--out FILE] [--repos a,b,c]
Env:
    SLACK_SPAWN_ALLOWLIST_FILE   default ~/.tmux/spawnable-repos.json
    SLACK_SPAWN_SUMMARIES_FILE   default ~/.tmux/spark-summaries.json
    SPARK_SSE_URL                default http://localhost:8343/sse
    SPARK_TIMEOUT                seconds, default 30
    SPARK_DESC_MAXLEN            chars, default 500
"""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile

HOME = os.path.expanduser("~")
SSE_URL = os.environ.get("SPARK_SSE_URL", "http://localhost:8343/sse")
TIMEOUT = float(os.environ.get("SPARK_TIMEOUT", "30"))
MAXLEN = int(os.environ.get("SPARK_DESC_MAXLEN", "500"))

DEFAULT_ALLOWLIST = os.environ.get(
    "SLACK_SPAWN_ALLOWLIST_FILE", os.path.join(HOME, ".tmux", "spawnable-repos.json"))
DEFAULT_OUT = os.environ.get(
    "SLACK_SPAWN_SUMMARIES_FILE", os.path.join(HOME, ".tmux", "spark-summaries.json"))

# "## Project Overview" (or "## Overview") block, up to the next ## heading.
_OVERVIEW = re.compile(
    r"^#{2,}\s*(?:Project\s+)?Overview\s*$\r?\n(.*?)(?=^\s*#{1,6}\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE)
# The near-universal CLAUDE.md preamble — never a useful description.
_BOILERPLATE = re.compile(r"provides guidance to Claude Code|claude\.ai/code", re.IGNORECASE)
# Structured header lines emitted by Spark's summary.
_FRAMEWORK = re.compile(r"^Framework:\s*([^\|\r\n]+?)\s*(?:\||$)", re.MULTILINE)
_DEPLOY = re.compile(r"Deploy:\s*([^\|\r\n]+)", re.MULTILINE)
_PATH = re.compile(r"^Path:\s*(.+?)\s*$", re.MULTILINE)


def _emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _note(msg):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def _collapse(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _first_paragraph(text):
    """First non-empty, non-heading, non-list paragraph of a markdown block."""
    for para in re.split(r"\r?\n\s*\r?\n", text or ""):
        p = _collapse(para)
        if not p:
            continue
        if p.startswith("#") or p.startswith("- ") or p.startswith("* ") or p.startswith("```"):
            continue
        if _BOILERPLATE.search(p):
            continue
        return p
    return ""


def _truncate(s):
    if len(s) <= MAXLEN:
        return s
    cut = s[:MAXLEN]
    # Prefer to end on a sentence/word boundary rather than mid-token.
    dot = cut.rfind(". ")
    if dot >= MAXLEN * 0.6:
        return cut[:dot + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp >= MAXLEN * 0.6 else cut).rstrip() + "…"


def extract_desc(summary, name):
    """Distill Spark's long installation_summary into one concise paragraph."""
    if not summary:
        return ""
    # 1. The CLAUDE.md "Project Overview" section — the strongest signal.
    m = _OVERVIEW.search(summary)
    if m:
        para = _first_paragraph(m.group(1))
        if len(para) >= 30:
            return _truncate(para)
    # 2. First real prose paragraph after the "## CLAUDE.md" marker (skip the
    #    duplicated "# CLAUDE.md" / "# <name>" title lines Spark prepends).
    body = summary
    idx = summary.find("## CLAUDE.md")
    if idx != -1:
        body = summary[idx + len("## CLAUDE.md"):]
    for para in re.split(r"\r?\n\s*\r?\n", body):
        p = _collapse(para)
        if (len(p) >= 40 and not p.startswith("#")
                and not p.lower().startswith(name.lower())
                and not _BOILERPLATE.search(p)):
            return _truncate(p)
    # 3. Structured fallback: framework + deploy + path, whatever is present.
    fw = (_FRAMEWORK.search(summary) or [None, None])[1]
    dep = (_DEPLOY.search(summary) or [None, None])[1]
    path = (_PATH.search(summary) or [None, None])[1]
    bits = []
    if fw:
        bits.append(f"{_collapse(fw)} project")
    if dep:
        bits.append(f"deploy: {_collapse(dep)}")
    if path:
        bits.append(f"({_collapse(path)})")
    return _truncate(" · ".join(bits)) if bits else ""


def _extract_text(result):
    """Pull the text payload out of an MCP call_tool result across SDK shapes."""
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
    # The spark tools wrap output as {"result": "..."} — unwrap if so.
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "result" in obj:
            return obj["result"]
    except (ValueError, TypeError):
        pass
    return raw


async def _fetch_all(repos):
    """One SSE session, one installation_summary call per repo. Returns
    { repo: summary_text } for repos that answered; missing repos are skipped."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    out = {}
    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for repo in repos:
                try:
                    res = await session.call_tool("installation_summary", {"repo_name": repo})
                    out[repo] = _extract_text(res)
                except Exception as e:  # noqa: BLE001 — one repo failing must not sink the rest
                    _note(f"  {repo}: summary call failed — {type(e).__name__}: {e}")
    return out


def _load_repos(allowlist_file, explicit):
    if explicit:
        return [r.strip() for r in explicit.split(",") if r.strip()]
    try:
        with open(allowlist_file, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as e:
        _note(f"could not read allowlist {allowlist_file}: {e}")
        return []
    if not isinstance(obj, dict):
        return []
    return [k for k in obj if not k.startswith("__")]


def _atomic_write(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".spark-summaries.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    ap = argparse.ArgumentParser(description="Cache concise spawnable-repo descriptions from Spark.")
    ap.add_argument("--allowlist", default=DEFAULT_ALLOWLIST)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--repos", default="", help="comma-separated repo names (default: allowlist keys)")
    args = ap.parse_args()

    repos = _load_repos(args.allowlist, args.repos)
    if not repos:
        _emit({"written": 0, "reason": "no spawnable repos"})
        return 0

    try:
        summaries = asyncio.run(asyncio.wait_for(_fetch_all(repos), timeout=TIMEOUT))
    except asyncio.TimeoutError:
        _emit({"written": 0, "error": f"timeout after {TIMEOUT}s", "repos": len(repos)})
        return 0
    except BaseException as e:  # connection refused, SDK import, protocol error, ...
        leaf = e
        while leaf is not None and getattr(leaf, "exceptions", None):
            leaf = leaf.exceptions[0]
        _emit({"written": 0, "error": f"{type(leaf).__name__}: {leaf}"})
        return 0

    descs = {}
    for repo in repos:
        desc = extract_desc(summaries.get(repo, ""), repo)
        if desc:
            descs[repo] = desc
            _note(f"  {repo}: {len(desc)} chars")
        else:
            _note(f"  {repo}: no description extracted (skipped)")

    if not descs:
        _emit({"written": 0, "reason": "no descriptions extracted", "repos": len(repos)})
        return 0

    _atomic_write(args.out, descs)
    _emit({"written": len(descs), "out": args.out, "repos": len(repos)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
