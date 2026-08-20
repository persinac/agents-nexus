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
import importlib.util, json, os, re, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)                                   # unparseable -> fail open

if d.get("tool_name") != "Bash":
    sys.exit(0)
cmd = (d.get("tool_input") or {}).get("command") or ""
if not cmd.strip():
    sys.exit(0)

# A heredoc body is DATA, not commands. Borrowed verbatim in shape from
# block-credential-dump.sh, which solved this first (2026-08-12).
#
# WHY (2026-08-19): this guard cost ~6 false blocks in one session -- a `gh pr create
# --body` whose PR description merely MENTIONED a delete command, and a heredoc feeding
# documentation prose. The commit message for this very hook had to be passed via
# `git commit -F` for the same reason.
#
# An earlier version of this file REJECTED heredoc stripping outright, reasoning that
# `bash <<'EOF'` executes its body so a stripped body is where a destructive command
# would hide. That reasoning was right and the conclusion was wrong: the exception
# below is exactly the missing piece, and it already existed one directory over.
#
# EXCEPTION: when the heredoc feeds an interpreter, the body really is executed, so it
# stays in scope. The (?<!\.) guard stops \bsh\b matching the ".sh" in a FILENAME.
INTERP = re.compile(r"(?<!\.)\b(?:(?:ba|z|k|da)?sh|python[0-9.]*|perl|ruby|node|eval)\b")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

def strip_heredoc_bodies(text):
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        m = HEREDOC.search(lines[i])
        if m and not INTERP.search(lines[i]):
            delim = m.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != delim:
                i += 1                       # drop the body
            if i < len(lines):
                out.append(lines[i])         # keep the closing delimiter
        i += 1
    return "\n".join(out)

# `agent-send.sh <fqdn> "<message>"` hands its argument to the NATS bus as DATA -- the
# script reads nothing and executes nothing, and the receiving agent gets the text as a
# user turn. So a report that merely NAMES a destructive command must not be refused.
# Not hypothetical: on 2026-08-20 this class refused an inter-agent report about the
# guards themselves. Heredoc stripping cannot reach it (a payload is a quoted argv
# element, not a heredoc body).
#
# Command substitution is deliberately NOT stripped -- `"$(kubectl delete ns prod)"`
# really runs, because the shell expands it before agent-send is invoked. Same principle
# as the INTERP exception above: strip what is inert, keep what executes.
_MSG_SCRIPT = re.compile(r"\bagent-send\.sh\b")
_QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"")

def strip_message_payload(text):
    if not _MSG_SCRIPT.search(text):
        return text
    return _QUOTED_SPAN.sub(
        lambda m: m.group(0) if ("$(" in m.group(0) or "`" in m.group(0)) else " ",
        text)

cmd = strip_message_payload(strip_heredoc_bodies(cmd))

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

# Record the block. Until 2026-08-19 neither PreToolUse guard logged anything at all,
# so a refusal existed only as stderr in one transcript and there was no way to answer
# "what has the destructive denylist actually stopped?".
#
# Deliberately does NOT write the command text — same rule as notify-classify.py's
# _log_decision: a durable file must not receive a credential, and commands carry
# inline tokens. Head + truncated hash is enough to group and correlate.
# Format matches the classifier's log exactly so one awk reads both.
try:
    import hashlib, time
    log = os.path.join(os.environ.get("NEXUS_TMUX_DIR") or
                       os.path.expanduser("~/.tmux"), "gate-decisions.log")
    if os.path.exists(log) and os.path.getsize(log) > 5 * 1024 * 1024:
        os.replace(log, log + ".1")
    head = re.sub(r"[^\w.:-]", "", os.path.basename((cmd.split() or ["-"])[0]))[:32] or "-"
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("{} block destructive-guard {} {} {}\n".format(
            int(time.time()), os.environ.get("PANE") or "-", head,
            hashlib.sha256(cmd.encode("utf-8", "replace")).hexdigest()[:12]))
except Exception:
    pass                                          # logging must never wedge the guard

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
