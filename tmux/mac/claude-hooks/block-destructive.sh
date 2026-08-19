#!/usr/bin/env bash
# PreToolUse(Bash) guard: refuse commands that delete production data or delete
# deployed infrastructure.
#
# WHY THIS EXISTS (2026-08-19). notify-classify.py's permissive mode auto-approves
# 94.7% of real Bash traffic, on Alex's stated bar: "as long as we're not deleting
# production data from a db, or deleting something from k8s, most of it is fine."
# Its _DESTRUCTIVE denylist is what holds that line.
#
# But _DESTRUCTIVE only runs when a PERMISSION PROMPT is raised. An overnight,
# unattended agent is spawned with `--dangerously-skip-permissions` (the only posture
# that comes up running -- see open-claude.sh's CLAUDE_EXTRA_ARGS note), and that mode
# raises no prompts at all. So the single guard standing between an unattended run and
# `kubectl delete` would never have executed. This hook closes exactly that gap: a
# PreToolUse hook fires on every tool call in every session, skip-permissions included.
#
# It is the same relationship block-credential-dump.sh has to the CLAUDE.md secret
# rule: the classifier is the promise, this is the enforceable half.
#
# SINGLE SOURCE OF TRUTH: the pattern is imported from notify-classify.py, never
# copied. A second copy would drift, and the copy that drifts is the one that stops
# blocking. If that import fails this hook fails OPEN and says so on stderr -- a guard
# bug must not wedge every session -- but it fails CLOSED on a match.
#
# Exit 2 + stderr is the documented PreToolUse blocking contract.

set -uo pipefail

payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

CLASSIFIER="${NEXUS_CLASSIFIER:-$HOME/.tmux/notify-classify.py}"

read -r -d '' PY <<'PYEOF'
import importlib.util, json, os, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)                                   # unparseable -> fail open

if d.get("tool_name") != "Bash":
    sys.exit(0)
cmd = (d.get("tool_input") or {}).get("command") or ""
if not cmd.strip():
    sys.exit(0)

src = os.environ.get("CLASSIFIER_PATH", "")
try:
    spec = importlib.util.spec_from_file_location("nc", src)
    nc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nc)
    pattern = nc._DESTRUCTIVE
except Exception as e:                            # guard bug -> fail OPEN, but say so
    print(f"block-destructive: could not load _DESTRUCTIVE from {src} ({type(e).__name__}); "
          f"NOT guarding this call", file=sys.stderr)
    sys.exit(0)

m = pattern.search(cmd)
if not m:
    sys.exit(0)

# Report WHICH construct matched, never the whole command: the command can carry
# inline credentials and stderr goes into the durable transcript.
sys.stderr.write(f"""BLOCKED by block-destructive.sh

  matched construct: {m.group(0)[:80]!r}

This command deletes production data or deployed infrastructure. Auto-approval is
deliberately wide on this box (94.7% of Bash runs without asking), and this denylist
is the line that makes that safe -- so it holds even under
--dangerously-skip-permissions, where no permission prompt would ever be raised.

If this is intentional:
  - Run it yourself: prefix the command with ! in the prompt.
  - Or narrow it -- a scoped `kubectl delete pod X` still matches; that is on purpose.

If this is a FALSE POSITIVE, fix the pattern rather than working around it:
  {src}  ->  _DESTRUCTIVE
  tests: agents-nexus/tmux/mac/tmux-scripts/test-notify-classify.py (EXPECT_BLOCKED)
""")
sys.exit(2)
PYEOF

# EXPORT, not a var-prefix: `VAR=x printf ... | python3` scopes VAR to printf only,
# so python3 saw an empty path and fell through its fail-open branch on every call.
export CLASSIFIER_PATH="$CLASSIFIER"
printf '%s' "$payload" | python3 -c "$PY"
rc=$?
[ "$rc" -eq 2 ] && exit 2
exit 0
