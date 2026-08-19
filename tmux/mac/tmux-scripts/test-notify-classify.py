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

    # --- shell text that is DATA, not commands (added 2026-08-19) ---
    # Measured: 29.8% of everything this gate withheld over 30 days contained one of
    # these three shapes. All were MIS-PARSES — heredoc bodies, continued lines and
    # comments arriving as bogus segments whose "command head" was Python source or prose.
    "grep -c ERROR <<'EOF'\nline one\nline two\nEOF",
    "grep -q x <<'DATA'\n# this # is # data\nrm -rf /not-a-command\nDATA",
    'aws ec2 describe-instances \\\n  --query "Reservations[].Instances[]" \\\n  --output json',
    'git log --oneline \\\n  --since=yesterday',
    '# just a comment\nls -la',
    'ls -la  # trailing comment is part of the segment\n# whole-line comment\npwd',

    # --- gh, positional (added 2026-08-19) ---
    'gh pr view 42',
    'gh pr diff 42',
    'gh pr checks 42',
    'gh pr list --state open',
    'gh api repos/owner/repo/pulls',
    'gh api --paginate repos/owner/repo/issues',
    'gh run watch 12345',
    'gh run list --limit 5',
    'gh -R owner/repo pr view 42',
    'gh release list',

    # --- git read subcommands (added 2026-08-19) ---
    'git branch',
    'git branch -a',
    'git branch --list "feat/*"',
    'git tag',
    'git tag -l "v1.*"',
    'git stash list',
    'git stash show',
    'git worktree list',
    'git worktree',
    'git remote',
    'git remote -v',
    'git remote show origin',
    'git submodule status',
    'git config --get user.name',
    'git config --list',
    'git check-ignore -v build/',
    'git ls-remote --heads origin',

    # --- npx-wrapped runners (added 2026-08-19) ---
    'npx jest --runInBand',
    'npx tsc --noEmit',
    'npx playwright test',
    'npx -y vitest run',
    'shellcheck tmux/mac/tmux-scripts/hook-notification.sh',

    # --- awk, guarded (added 2026-08-19) ---
    'ps aux | awk \'{print $2}\'',
    'awk -F: \'{print $1}\' /etc/passwd',
    'git log --oneline | awk \'{print $1}\' | head -5',

    # --- assignment builtins, docker compose, systemctl, inert inspection ---
    'export FOO=bar',
    'set -euo pipefail',
    'docker compose ps',
    'docker compose logs api',
    'docker compose config',
    'systemctl status nginx',
    'systemctl is-active docker',
    'systemctl list-units --type=service',
    'journalctl -u agents-nexus-stack -n 50',
    'ss -tlnp',
    'lsof -i :8788',
    'dig +short example.com',
    'sha256sum /etc/hosts',
    'diff a.txt b.txt',
    'kubectl kustomize overlays/prod',

    # --- fleet tooling (policy call, per Alex) ---
    '/home/persinac/.tmux/agent-send.sh alex-nexus/nexus/agents-nexus "hello there"',
    '~/.tmux/agent-registry.sh peers --exclude w12:p2',
    'curl -s localhost:8788/agents | jq .',

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

    # --- gh: the false-approve this rule was written to close (2026-08-19) ---
    # THE case. The old rule searched the whole segment for view|list|status|get, so
    # the word "list" inside a quoted flag value auto-approved a MERGE. Matching the
    # action positionally sees `merge`. Same defect _glab_is_read already documented.
    'gh pr merge 42 --body "list of changes"',
    'gh issue close 7 --comment "see the list above"',
    'gh pr merge 42',
    'gh pr create --title x --body y',
    'gh pr close 42',
    'gh api -X POST repos/owner/repo/issues',
    'gh api --method DELETE repos/owner/repo/labels/x',
    # the silent-POST trap: a field flag flips gh from GET to POST with no -X anywhere
    'gh api repos/owner/repo/issues -f title=bug',
    'gh api repos/owner/repo/issues --field title=bug',
    'gh run download 12345',          # writes artifacts to disk
    'gh repo clone owner/repo',
    'gh auth login',

    # --- git: the dual-use subcommands must not pass on membership alone ---
    # `git remote` USED to be a plain _READ_SUB member, so this auto-approved a write.
    'git remote add origin https://example.com/r.git',
    'git remote remove origin',
    'git branch -D feature/x',
    'git branch -m old new',
    'git branch --set-upstream-to=origin/main',
    'git tag -d v1.0.0',
    'git tag -a v1.0.0 -m "release"',
    'git stash',                      # bare stash STASHES; only `stash list/show` reads
    'git stash pop',
    'git worktree add ../wt feature',
    'git worktree remove ../wt',
    'git submodule update --init',
    'git config user.email "x@example.com"',
    'git config --unset user.name',

    # --- heredocs: the body is data, but the HEAD is still classified ---
    "python3 - <<'PY'\nimport os\nprint(os.getcwd())\nPY",
    "bash <<'EOF'\nls -la\nEOF",
    "cat > out.txt <<'EOF'\ncontent\nEOF",
    # an UNQUOTED tag still expands $(...), so the body must keep being scanned
    'cat <<EOF\n$(git commit -am sneaky)\nEOF',

    # --- awk: the execution constructs ---
    'awk \'BEGIN{system("date")}\'',
    'awk \'{print | "sh"}\' f',
    'awk \'{while ((getline l < "f") > 0) print l}\'',

    # --- npx must not decay into "any npx package" ---
    'npx create-react-app myapp',
    'npx some-unknown-cli --do-things',
    'npx prettier --write .',

    # --- compose / systemctl / builtins: the writing halves ---
    'docker compose up -d',
    'docker compose down',
    'docker compose restart api',
    'systemctl restart nginx',
    'systemctl daemon-reload',
    'systemctl enable --now foo',
    # a BARE export/set dumps every shell variable — a credential path on this host
    'export',
    'set',
    'declare',
    'declare -p',                     # -p prints every variable WITH its value
    'export -p',

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
