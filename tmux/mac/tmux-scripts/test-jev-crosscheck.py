#!/usr/bin/env python3
"""Tests the Jev cross-check veto without a key, a network call, or the SDK installed."""
import importlib.util
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("nc", HERE / "notify-classify.py")
nc = importlib.util.module_from_spec(spec)
sys.modules["nc"] = nc
spec.loader.exec_module(nc)

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}: want {want!r} got {got!r}")


def with_jev(verdict):
    nc._jev = lambda name, inp: verdict
    return nc._jev_declines("Bash", {"command": "git status"})


orig_jev = nc._jev
orig_gate = nc._JEV_GATE
nc._JEV_GATE = 0.30

check("absent/disabled jev never vetoes", with_jev(None), False)
check("confident read is vouched for", with_jev(("read", 0.95)), False)
check("read at the gate is vouched for", with_jev(("read", 0.30)), False)
check("low-margin read is vetoed", with_jev(("read", 0.17)), True)
check("coin-flip read is vetoed", with_jev(("read", 0.00)), True)
check("confident modify is vetoed", with_jev(("modify", 0.99)), True)
check("low-margin modify is vetoed", with_jev(("modify", 0.06)), True)

nc._JEV_GATE = orig_gate
nc._jev = orig_jev

os.environ.pop("CC_JEV_CROSSCHECK", None)
check("real _jev is inert when unset", nc._jev("Bash", {"command": "ls"}), None)
os.environ["CC_JEV_CROSSCHECK"] = "1"
os.environ.pop("TYPESAFE_API_KEY", None)
check("real _jev is inert without a key", nc._jev("Bash", {"command": "ls"}), None)
os.environ.pop("CC_JEV_CROSSCHECK", None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
