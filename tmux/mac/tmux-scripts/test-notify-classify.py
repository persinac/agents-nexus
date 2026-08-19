#!/usr/bin/env python3
"""Regression matrix for notify-classify.py's DETERMINISTIC auto-approve gate.

Run it directly, no framework and no venv needed:

    python3 tmux/mac/tmux-scripts/test-notify-classify.py

Exercises `_deterministic_read` only, never the LLM tier, so a failure here is a
real logic bug rather than a flaky model call or a network problem. That matters
because this gate decides what runs on Alex's machine WITHOUT asking him: a bug in
the permissive direction is silent, and until 2026-08-18 two such bugs had been
sitting in it (newlines not treated as command separators, and `$(...)` bodies never
inspected — both found by writing this file).

Both directions are asserted on purpose. EXPECT_READ guards against the gate getting
narrower and going back to interrupting people for a `for` loop over grep;
EXPECT_WITHHELD guards against it getting wider and waving through something that
changes state. A new allowlist entry should always arrive with a negative case next
to it showing what it deliberately does NOT cover.
"""
from __future__ import annotations

import importlib.util
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify-classify.py")
_spec = importlib.util.spec_from_file_location("notify_classify", SRC)
nc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nc)

# Must auto-approve with no model call at all.
EXPECT_READ = [
    # --- shell control flow (added 2026-08-18) ---
    # the exact command that interrupted Alex at 14:38 and prompted the change
    'for i in 1 2; do echo "loop-$i"; done',
    'for f in *.log; do grep -c ERROR "$f"; done',
    'if [ -f x ]; then cat x; fi',
    'if grep -q needle f; then echo found; else echo missing; fi',
    'while read -r l; do echo "$l"; done',
    'until [ -f ready ]; do sleep 1; done',
    'case "$x" in a) echo a ;; *) echo b ;; esac',
    'gate_probe() { :; }',
    'for f in $(ls); do cat "$f"; done',
    'echo "$(date +%F)"',
    'echo $((1 + 2))',
    'for i in 1 2 3; do echo $((i * 2)); done',
    'cat a\ngrep x b',
    'for f in *.py; do wc -l "$f"; done 2>/dev/null',
    'for f in *.py; do wc -l "$f"; done 2>&1',
    'if [ -f x ]; then cat x; fi >/dev/null',

    # --- test / check runners (policy allowlist, added 2026-08-18) ---
    # the exact command that left a fleet agent parked on an approval prompt
    ('cd /Users/dev/repos/.worktrees/x && '
     'POSTGRES_URL=postgresql+psycopg://user:pass@localhost:5433/testdb '
     '/Users/dev/repos/team/area/example-svc/'
     '.venv/bin/python -m pytest tests/lib/ tests/managers/ai/test_litellm_manager.py '
     '-q 2>&1 | tail -35'),
    'pytest tests/ -q',
    'python3 -m pytest tests/lib -q',
    'uv run pytest -q',
    'npm test',
    'go test ./...',
    'cargo test',
    'ruff check .',
    'mypy src',
    'for d in tests/a tests/b; do pytest "$d" -q; done',


    # --- glab, the GitLab CLI (added 2026-08-18) ---
    # the real shape from the fleet's own tooling and ~/.claude/CLAUDE.md
    'glab api "projects/12345678/merge_requests/605"',
    'glab api projects/1/merge_requests?state=opened',
    'glab api projects/1 | jq .name',
    'glab api --paginate projects/1/pipelines',
    'glab mr list',
    'glab mr view 605',
    'glab mr diff 605',
    'glab -R example-org/example-svc mr list',
    'glab --repo example-org/example-svc mr view 605',
    'glab ci status',
    'glab ci trace',
    'glab auth status',
    'glab release list',
    'glab variable list',
    'glab config get editor',
    'glab version',
    'for m in 605 606; do glab mr view "$m"; done',

    # --- regressions: reads that already worked before all of the above ---
    'cat /etc/hosts',
    'git status && git log --oneline | head -5',
    '( cd /tmp && ls )',
    'rg -n pattern . 2>/dev/null | head -20',
]

# Must NOT auto-approve — either it changes state, or we refuse to vouch for it.
EXPECT_WITHHELD = [
    # a modifying body inside an otherwise read-shaped loop
    'for f in *; do git commit -am "$f"; done',
    'while read -r l; do curl -X POST http://x -d "$l"; done',
    'if true; then git checkout main; fi',
    # the newline hole: line one reads, line two does not
    'cat a\ngit commit -am x',
    # the substitution hole: read-looking wrapper, modifying body
    'echo "$(git commit -am x)"',
    'for f in $(git stash); do echo "$f"; done',
    'echo "$(echo "$(git reset --soft HEAD~1)")"',
    # a redirect out of a loop
    'for f in *; do echo "$f" > out.txt; done',
    # an unrecognized command anywhere
    'for f in *; do frobnicate "$f"; done',
    # the runner allowlist must not decay into "any python/node command"
    'python3 -c "import shutil; shutil.rmtree(\'x\')"',
    'python3 some_script.py',
    '/path/.venv/bin/python manage.py migrate',
    'uv run python -c "print(1)"',
    'make test',            # a Makefile target can do anything
    'npm run build',

    # --- glab writes, and the traps ---
    'glab mr merge 605',
    'glab mr create --title x --description y',
    'glab mr approve 605',
    'glab mr checkout 605',              # mutates local git state
    'glab mr note 605 --message hi',
    'glab issue close 12',
    'glab api -X POST projects/1/merge_requests',
    'glab api --method DELETE projects/1/labels/x',
    'glab api -XPOST projects/1/notes',
    # the silent-POST trap: a field flag flips glab from GET to POST with no -X anywhere
    'glab api projects/1/notes -F body=hello',
    'glab api projects/1/notes --field body=hello',
    'glab api projects/1/merge_requests --input payload.json',
    'glab auth login',
    'glab ci retry 123',
    'glab variable set KEY=value',
    'glab repo clone example-org/example-svc',
    'glab config set editor vim',
    # the false-approve guard: a read verb inside a quoted string must NOT approve a merge.
    # This is why the action is matched positionally rather than searched for in the segment.
    'glab mr merge 605 --description "list of changes"',
    'glab mr create --title "view the diff and status list"',
    'glab schedule delete 9',

    # formatters rewrite files; ruff/eslint only qualify outside writing modes
    'ruff format .',
    'ruff check --fix .',
    'eslint --fix .',
    'black .',
]


def main() -> int:
    fails = []
    for cmd in EXPECT_READ:
        if not nc._deterministic_read(cmd):
            fails.append(("expected auto-approve, gate withheld", cmd))
    for cmd in EXPECT_WITHHELD:
        if nc._deterministic_read(cmd):
            fails.append(("expected withheld, gate AUTO-APPROVED", cmd))

    total = len(EXPECT_READ) + len(EXPECT_WITHHELD)
    if fails:
        print(f"FAIL — {len(fails)} of {total}")
        for why, cmd in fails:
            print(f"  {why}: {cmd!r}")
        return 1
    print(f"PASS — {total}/{total} "
          f"({len(EXPECT_READ)} auto-approve, {len(EXPECT_WITHHELD)} withheld)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
