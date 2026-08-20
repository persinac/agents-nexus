#!/usr/bin/env python3
"""Meta-suite: prove block-credential-dump.test.py can DETECT a broken guard.

WHY THIS EXISTS (2026-08-20): the guard "fails OPEN on internal error" by design -- a
bug inside the embedded python must never wedge the session. The cost of that choice is
that a BROKEN guard and a WORKING guard produce identical observable behaviour: commands
run, nothing is refused, no error is printed. An earlier revision had a mismatched
bracket and silently allowed everything.

A green functional suite is therefore not self-validating. It only means something if we
can show it goes RED when the guard is damaged. That is what this file asserts.

It never touches the deployed guard: every mutant is a COPY in a temp dir, handed to the
functional suite through its HOOK_PATH override.

Run:  python3 block-credential-dump.mutation.test.py
Exit: 0 if every mutation was detected, 1 otherwise.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE / "block-credential-dump.sh"
SUITE = HERE / "block-credential-dump.test.py"


def run_suite(hook_path):
    """Run the functional suite against `hook_path`. Return (failure_count, result_line).

    Parses the RESULT line rather than per-case labels: the labels are presentation and
    may be reworded, but the RESULT line is the suite's verdict and is what CI reads.
    """
    env = {**os.environ, "HOOK_PATH": str(hook_path)}
    p = subprocess.run([sys.executable, str(SUITE)], env=env, capture_output=True, text=True)
    out = p.stdout + p.stderr
    line = next((l.strip() for l in out.splitlines() if "RESULT:" in l), "")
    if not line:
        return None, "<suite produced no RESULT line>"
    if "ALL PASS" in line:
        return 0, line
    m = re.search(r"(\d+)\s+FAILURES?", line)
    if not m:
        return None, line
    return int(m.group(1)), line


def neutralise_list(name):
    """Replace a top-level list literal with []. Proves that decision path is exercised."""

    def transform(src):
        pat = re.compile(rf"^{name} = \[.*?^\]$", re.S | re.M)
        new, n = pat.subn(f"{name} = []", src)
        if n != 1:
            raise AssertionError(f"{name}: expected 1 list literal, matched {n}")
        return new

    return transform


def replace_once(old, new):
    def transform(src):
        if src.count(old) != 1:
            raise AssertionError(f"expected exactly one {old!r}, found {src.count(old)}")
        return src.replace(old, new)

    return transform


# Each mutation damages the guard in a way a careless edit really could. Every one of
# them MUST make the functional suite go red. A mutation that stays green is a hole:
# it means that code path has no test behind it.
MUTATIONS = [
    (
        "syntax error (mismatched bracket) -> guard raises -> fails open",
        replace_once("SELF_DUMPING = [", "SELF_DUMPING = [["),
    ),
    (
        "guard runs but never signals a block (exit 2 -> exit 0)",
        replace_once("sys.exit(2)", "sys.exit(0)"),
    ),
    (
        "SELF_DUMPING emptied -> implicit-credential commands unguarded",
        neutralise_list("SELF_DUMPING"),
    ),
    (
        "CRED emptied -> credential paths unrecognised",
        neutralise_list("CRED"),
    ),
    (
        "BROAD emptied -> unbounded readers unrecognised",
        neutralise_list("BROAD"),
    ),
]


def main():
    if not GUARD.exists() or not SUITE.exists():
        print(f"  cannot find guard/suite beside {HERE}")
        return 1

    print("  BASELINE (unmodified guard):")
    base, base_line = run_suite(GUARD)
    print(f"    {base_line}")
    if base != 0:
        print("    FAIL: the guard is already failing its own suite -- fix that first.")
        return 1

    failures = 0
    print("  MUTATIONS (each must be DETECTED, i.e. turn the suite red):")
    with tempfile.TemporaryDirectory(prefix="guard-mutants-") as td:
        src = GUARD.read_text()
        for i, (label, transform) in enumerate(MUTATIONS):
            try:
                mutated = transform(src)
            except AssertionError as e:
                print(f"    STALE  {label}\n           mutation no longer applies: {e}")
                failures += 1
                continue

            mutant = Path(td) / f"mutant_{i}.sh"
            mutant.write_text(mutated)
            mutant.chmod(0o755)

            count, line = run_suite(mutant)
            if count is None:
                # The suite could not even report -- still a detection, but say so plainly.
                print(f"    caught {label}\n           ({line})")
                continue
            if count == 0:
                print(f"    MISSED {label}\n           suite stayed GREEN with a broken guard")
                failures += 1
            else:
                print(f"    caught {label}  ({count} red)")

    print()
    if failures:
        print(f"  RESULT: {failures} UNDETECTED MUTATION(S) -- the suite has blind spots")
        return 1
    print(f"  RESULT: ALL {len(MUTATIONS)} MUTATIONS DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
