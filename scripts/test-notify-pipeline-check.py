#!/usr/bin/env python3
"""Fixture tests proving every notify-pipeline-check detector fires (and stays quiet when it should). Run: python3 scripts/test-notify-pipeline-check.py"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent / "notify-pipeline-check.py"
_spec = importlib.util.spec_from_file_location("npc", SRC)
npc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(npc)

T = Path(tempfile.mkdtemp(prefix="notify-check-test-"))
NOW = int(time.time())

passed = failed = 0


def check(desc: str, got, want) -> None:
    global passed, failed
    if got == want:
        print(f"  PASS  {desc:<52} {got}")
        passed += 1
    else:
        print(f"  FAIL  {desc:<52} got={got} want={want}")
        failed += 1


def write(name: str, lines: list[str]) -> Path:
    p = T / name
    p.write_text("".join(lines))
    return p


def reset(**over) -> None:
    npc.APPROVE_LOG = write("approve.log", [])
    npc.ASKED_LOG = write("asked.log", [])
    npc.REPEAT_LOG = write("repeat.log", [])
    npc.DEBUG_LOG = write("debug.log", [])
    npc.PRESENCE_LOG = T / "absent-presence.log"
    npc.CLASSIFY_PY = write("fake-python", ["#!/bin/sh\n"])
    npc.HERDR_PLUGINS = write(
        "plugins.json",
        [json.dumps([{"plugin_id": "nexus.presence", "enabled": False}])])
    npc.LOG_CAPS = {}
    npc.missed_check = lambda _w: {}
    for k, v in over.items():
        setattr(npc, k, v)


def ids(window: float = 1.0) -> set[str]:
    return {i["id"] for i in npc.collect(window)[0]}


def approvals(n: int) -> Path:
    return write("approve.log", [f"{NOW-60} auto-approve w1:p1\n"] * n)


def asks(n: int, age: int = 60, name: str = "asked.log") -> Path:
    return write(name, [f"{NOW-age} w1:p1 permission_prompt {{}}\n"] * n)


print("== healthy system ==")
reset()
npc.APPROVE_LOG = approvals(100)
npc.ASKED_LOG = asks(2)
check("no issues", ids(), set())

print("== classifier-degraded ==")
reset()
npc.APPROVE_LOG = approvals(5)
npc.ASKED_LOG = asks(30)
check("fires below 50% handled", "classifier-degraded" in ids(), True)

reset()
npc.ASKED_LOG = asks(5)
check("silent below the 20-prompt floor", "classifier-degraded" in ids(), False)

print("== classifier-silent ==")
reset()
stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 120))
npc.DEBUG_LOG = write(
    "debug.log",
    [f"{stamp} [w1:p1] type=permission_prompt prompt=x raw=y\n"] * 25)
check("fires on prompts with zero approvals", "classifier-silent" in ids(), True)

print("== classifier-venv-missing ==")
reset(CLASSIFY_PY=T / "does-not-exist")
check("fires when the interpreter is gone",
      "classifier-venv-missing" in ids(), True)

print("== log-unrotated ==")
reset()
big = write("big.log", ["x" * 300])
npc.LOG_CAPS = {big: 100}
check("fires past cap*1.2", any(i.startswith("log-unrotated") for i in ids()), True)

reset()
ok = write("ok.log", ["x" * 100])
npc.LOG_CAPS = {ok: 1000}
check("silent under cap", any(i.startswith("log-unrotated") for i in ids()), False)

print("== presence-reenabled canary ==")
reset()
npc.HERDR_PLUGINS = write(
    "plugins.json",
    [json.dumps([{"plugin_id": "nexus.presence", "enabled": True}])])
check("fires when the plugin comes back", "presence-reenabled" in ids(), True)

print("== gate-misresolved ==")
reset()
npc.missed_check = lambda _w: {"by_outcome": {"ok": 3, "wrong-tool": 1, "no-tool": 2}}
check("fires on a misresolved pending call", "gate-misresolved" in ids(), True)

reset()
npc.missed_check = lambda _w: {"by_outcome": {"ok": 9}}
check("silent when every ask was owed", "gate-misresolved" in ids(), False)

reset()
npc.missed_check = lambda _w: {"by_outcome": {"ok": 4, "no-coverage": 7}}
check("silent on subagent calls the transcript cannot see",
      "gate-misresolved" in ids(), False)

reset()
npc.missed_check = lambda _w: {}
check("silent when the sub-check cannot run", "gate-misresolved" in ids(), False)

print("== ask-spike ==")
reset()
npc.APPROVE_LOG = approvals(300)
rows = [f"{NOW - 5*86400} w1:p1 permission_prompt {{}}\n"] * 10
rows += [f"{NOW-60} w1:p1 permission_prompt {{}}\n"] * 15
npc.ASKED_LOG = write("asked.log", rows)
got = ids()
check("fires vs the 7-day baseline", "ask-spike" in got, True)
check("and does not also claim degraded", "classifier-degraded" in got, False)

print()
print(f"PASS={passed} FAIL={failed}")
raise SystemExit(1 if failed else 0)
