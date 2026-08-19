#!/usr/bin/env python3
"""Permission-prompt middle-man: auto-approve gate + human-friendly summary.

Reads a Claude Code Notification hook payload (stdin) for a permission_prompt and:
  * decides read vs modify (the auto-approve gate), and
  * for prompts that need a human, produces a `category` + a 1-2 sentence
    middle-man `summary` (what the agent wants, what it's pertinent to, an honest
    safety read).

Output contract (so the hook needs no JSON parsing in bash):
  exit 0   -> READ: safe to auto-approve. Nothing printed.
  exit 10  -> MODIFY: needs a human. Prints the /notify JSON body on stdout
              ({name,pane,kind,category,summary}) ready to POST.
Any error -> exit 10 with a deterministic fallback summary (fail safe to "ask").

Policy (rewritten 2026-08-19 — the default is now ALLOW, see _PERMISSIVE):
Local reads (Read/Glob/Grep/LS/NotebookRead) -> read (no LLM, fast).
Bash -> TWO hard denylists first, and nothing downstream can override either:
  _DENY        rm/sudo/dd/pipe-to-shell/--force/...  (unchanged)
  _DESTRUCTIVE deleting production data (DROP/DELETE FROM/TRUNCATE/FLUSHALL/...),
               deleting k8s or cloud resources (kubectl delete|drain|--prune, helm
               uninstall, terraform destroy|apply, aws *delete-*/terminate-*), and
               `doppler secrets` (secret disclosure, per ~/.claude/CLAUDE.md).
Anything past those is auto-approved: first via the deterministic read allowlist,
then — because Alex's bar is "as long as we're not deleting production data from a
db, or deleting something from k8s, most of it is fine" — via the permissive tier,
with no model call. Local file edits (Edit/Write/MultiEdit/NotebookEdit) are likewise
permitted unless the path looks credential-ish.
MCP / web egress / unknown still go to the LLM: their blast radius is not knowable
from here and _DESTRUCTIVE only understands shell text.
Compound shell commands are "read" only if every part is read-only; the denylists
match against the whole command string, so a destructive part anywhere blocks.
Set CLASSIFY_STRICT=1 to revert to allowlist-only behaviour with no code change.

Loosened 2026-08-14 per Alex — more confident given block-credential-dump.sh as a
separate, independent layer for the credential-exposure threat specifically. That
hook does NOT cover any of the below; each of these is its own risk call:
  - kubectl/aws: all read verbs allowlisted. exec/cp/attach/debug/run/port-forward/
    proxy stay excluded on purpose — they execute code, copy data into a live
    resource, or open a network tunnel, none of which is "read".
  - A force-less `git push` now auto-approves. -f/--force/--force-with-lease/--hard
    still hard-deny unconditionally, checked before this allowlisting ever runs.
  - `mv` auto-approves unless the segment touches a credential-ish path or uses -f.
  - `chmod -R`/`chown -R` removed from the hard denylist (falls to the LLM now).

Control flow added 2026-08-18. A loop or conditional whose every command is read-only
now auto-approves deterministically, without an LLM call: `for`/`while`/`until`/
`select`/`case`/`if`, function definitions, and multi-line bodies. Previously any of
these failed on an unrecognized command head (`for`, `do`, `then`, ...) and fell
through to the LLM — which, with the permission classifier fail-closed as often as it
is here, meant interrupting the human for a `for` loop over grep. Two soundness holes
found and closed while doing it, both of which had made the gate too permissive:
  - Newlines were not command separators, so in a multi-line command only line 1's
    leading token was ever classified.
  - `$(...)`/backtick bodies were never inspected, so `echo "$(git commit -am x)"`
    read as a plain `echo`. Bodies are now checked too, recursively; `$((arith))` is
    correctly exempt since it cannot execute a command.

Env: AN=agent name, PANE=tmux pane id, KIND=notification_type, FB=fallback text.
Anthropic key loaded from the repo .env; default api base (the .env base is container-only).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import deque

MODEL = "anthropic/claude-haiku-4-5-20251001"
READ_TOOLS = {"read", "glob", "grep", "ls", "notebookread"}

# Command position: start of string, after a separator, or after whitespace. Used to
# stop `rm`/`dd`/`truncate` matching as bare words (2026-08-19). `\brm\b` fired on the
# `--rm` in `docker run --rm` — 98 weighted false positives over 30 days, a third of
# everything the rm clause caught — because `-` is a non-word character, so \b matches
# right inside the flag. `docker run --rm` deletes a finished container and nothing else.
_CMDPOS = r"(?:^|[;&|(`]\s*|\s)"

_DENY = re.compile(
    rf"({_CMDPOS}rm\s|{_CMDPOS}rmdir\s|\bsudo\b|{_CMDPOS}dd\s|\bmkfs"
    # `killall` removed 2026-08-19 per Alex: killing a process deletes no data and is
    # ordinary dev cleanup (`killall node`), so it is not in either named destructive
    # category. shutdown/reboot STAY — they would end an unattended overnight run.
    r"|\bshutdown\b|\breboot\b"
    # /dev/null explicitly excluded (2026-08-17): a raw-device write (dd/cat > /dev/sda)
    # is what this guards against; >/dev/null and 2>/dev/null are the standard
    # discard idiom and were being hard-denied by this alone, unconditionally
    # overriding even an LLM "read" verdict (classify()'s `safe` gate short-circuits
    # before the LLM result is ever consulted).
    r"|:\(\)\s*\{|>\s*/dev/(?!null\b)"
    # Force pushes always hard-deny, checked before the git-push allowlisting below
    # ever runs. SCOPED TO GIT as of 2026-08-19: the previous bare `--force\b|--hard\b`
    # matched anywhere in any command, so `npm install --force` (8 weighted hits in a
    # 30-day sample) was hard-denied as if it were a force push. Every other
    # destructive --force use is covered by _DESTRUCTIVE (docker rm, kubectl delete,
    # terraform destroy), so nothing is lost by narrowing this to git.
    r"|\bgit\s+push\b[^|;&]{0,200}-f\b|\bgit\b[^|;&]{0,200}(--force|--hard)\b"
    r"|\|\s*(sh|bash|zsh)\b|\b(curl|wget)\b[^|]*\|\s*(sh|bash)"
    rf"|\bnpm\s+publish\b|{_CMDPOS}truncate\s)",
    re.I,
)

# --- Permissive mode (added 2026-08-19, DEFAULT ON per Alex) ----------------
# Alex's stated bar, verbatim: "As long as we're not deleting production data from a
# db, or deleting something from k8s... most of it is fine."
#
# That INVERTS this module's model. It was built as "allowlist reads, ask about
# everything else", which capped auto-approval at 49.3% of real traffic even after the
# allowlist work — the residue is dominated by python3 (2083), write redirects (1805),
# git add/commit (1675) and kubectl (1038), none of which an allowlist can ever vouch
# for. Under the new bar those should simply run.
#
# The load-bearing consequence, and the reason _DESTRUCTIVE below exists: today
# `kubectl delete` and `DELETE FROM` are blocked only INCIDENTALLY, because everything
# unrecognized falls through to a human. Flip the default to allow and that incidental
# protection disappears silently. So the two categories Alex named have to become
# EXPLICIT hard denials — the permissive tier is only as safe as this regex.
#
# CLASSIFY_STRICT=1 reverts to the old allowlist-only behaviour with no code change.
_PERMISSIVE = os.environ.get("CLASSIFY_STRICT", "").lower() not in ("1", "true", "yes")

_DESTRUCTIVE = re.compile(
    # --- production data: the first thing Alex named ---
    r"\bdrop\s+(table|database|schema|index|view|column|constraint|owned)\b"
    r"|\bdelete\s+from\b"
    r"|\btruncate\s+table\b"
    r"|\balter\s+table\b[^;]{0,200}\bdrop\b"
    r"|\bdropdb\b"
    r"|\.drop\s*\(|\bdeleteMany\s*\(|\bdeleteOne\s*\(|\bremove\s*\(\s*\{\s*\}\s*\)"
    r"|\bflushall\b|\bflushdb\b"
    r"|\balembic\s+downgrade\b|\bdb:drop\b|\bdb:reset\b|\bmigrate\s+(down|reset|fresh)\b"
    # Django's `migrate <app> zero` reverses every migration for that app, dropping
    # its tables. It reads like an ordinary migrate and is the easiest one to miss.
    r"|\bmigrate\b[^|;&]{0,80}\bzero\b"
    # --- kubernetes: the second thing Alex named ---
    # [^|;&]* keeps each clause inside its own segment, so `kubectl get x | grep delete`
    # is not caught by the word `delete` appearing downstream of a pipe.
    r"|\bkubectl\b[^|;&]{0,200}\bdelete\b"
    r"|\bkubectl\b[^|;&]{0,200}\bdrain\b"
    r"|\bkubectl\b[^|;&]{0,200}--prune\b"
    r"|\bkubectl\b[^|;&]{0,200}\bscale\b[^|;&]{0,200}--replicas[=\s]*0\b"
    r"|\bhelm\s+(uninstall|delete|rollback)\b"
    # --- infrastructure that deletes cloud resources ---
    r"|\bterraform\s+(destroy|apply)\b|\bpulumi\s+(destroy|up)\b"
    r"|\baws\b[^|;&]{0,200}\s(delete|terminate|remove)-[a-z-]+"
    r"|\bgcloud\b[^|;&]{0,200}\bdelete\b|\baz\b[^|;&]{0,200}\bdelete\b"
    r"|\bdocker\s+(rm|rmi)\b|\bdocker\s+\w+\s+(rm|prune)\b|\bsystem\s+prune\b"
    # --- filesystem destruction that is NOT spelled `rm`, so _DENY misses it ---
    r"|\brmtree\b|\bos\.remove\b|\bos\.unlink\b|\bfs\.rmSync\b|\bunlinkSync\b"
    r"|\bshred\b|\bwipefs\b"
    # --- deleting a REMOTE git branch (`git push --delete` / `git push origin :x`) ---
    r"|\bgit\s+push\b[^|;&]{0,200}(--delete\b|\s:\S)"
    # --- secret DISCLOSURE into a durable transcript ---
    # NOT one of Alex's two categories, kept on his own standing host rule
    # (~/.claude/CLAUDE.md, written after a real leak): `doppler secrets` prints values
    # that cannot be un-printed. `doppler run` and `doppler secrets set` are unaffected.
    r"|\bdoppler\s+secrets\b(?!\s+set\b)",
    re.I,
)

# --- Deterministic read-only allowlist -------------------------------------
# Common inspection commands auto-approve WITHOUT an LLM call — instant and 100%
# reliable, so the gate never wavers on dual-use tools (curl, git, kubectl). The
# LLM only sees commands this can't vouch for. Conservative: any output redirect
# or unknown command head falls through to the LLM.
_READ_CMDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "echo", "pwd", "which", "whoami", "hostname",
    "uname", "date", "env", "printenv", "df", "du", "stat", "file", "tree", "realpath",
    "dirname", "basename", "grep", "egrep", "fgrep", "rg", "ag", "ack", "sort", "uniq",
    "cut", "tr", "column", "jq", "yq", "xxd", "od", "cmp", "tac", "nl", "cd", "true",
    "sleep", "test", "readlink", "type", "id", "ps", "top", "free", "uptime", "comm",
    # Shell builtins and test forms that execute nothing and persist nothing. Added
    # 2026-08-18 with control-flow support below, which started routing loop and
    # conditional HEADERS through here: `if [ -f x ]` and `while read -r line` are
    # the two idiomatic shapes and both used to fail on an unrecognized command head.
    "read", "printf", "seq", "expr", "[", "[[", ":",
    # --- inert system inspection (added 2026-08-19) ---
    # Each reports state and changes none. `systemctl` is NOT here — it is gated
    # positionally below, since it also starts and stops units.
    "journalctl", "ss", "netstat", "lsof", "dig", "nslookup", "getent",
    "lscpu", "nproc", "lsblk", "arch", "tty", "locale", "groups", "users", "w",
    "sha256sum", "sha1sum", "md5sum", "cksum", "diff", "strings", "paste", "join",
    "fold", "rev", "expand", "unexpand",
    # Deliberately NOT here: `split`/`csplit` (write xaa/xab files), `awk` (gated
    # separately — it can system()), `xargs`/`watch` (execute arbitrary commands),
    # the pagers `less`/`more` (`!cmd` shells out), and `base64` (no reason to spend
    # permissiveness budget on a decode path near secrets).
    # --- fleet tooling (added 2026-08-19, per Alex) ---
    # agent-send.sh was the #2 blocked head at 849 invocations. It publishes a message
    # to a peer over the NATS bus. Explicitly a POLICY call, not a read-only claim:
    # it IS outward-facing and reaches other hosts. Alex authorised it directly.
    # agent-registry.sh/nx-kv.sh/agent-resolve.sh are peer lookups and genuinely read.
    "agent-send.sh", "agent-registry.sh", "agent-resolve.sh", "nx-kv.sh",
    "substrate.sh",
})

# --- Shell control flow (added 2026-08-18) ----------------------------------
# Tokens that are shell SYNTAX rather than commands. A compound command arrives at
# _segment_is_read already split on `;`/newline, so `for f in *.log; do grep x "$f";
# done` shows up as three segments whose first and third begin with a keyword, not a
# command. Every such segment used to fail on an unrecognized command head, so ANY
# loop or conditional — however read-only its body — fell through to the LLM.
#
# Why that mattered enough to fix: with the permission classifier fail-closed as
# often as it is on this box (410 recorded automode-unavailable denials), "falls
# through to the LLM" reliably means "asks the human". A `for` loop over grep/cat is
# precisely the shape that should never have interrupted anyone, and it was the
# single most common one that did.
_STRUCTURAL = frozenset({
    "do", "done", "then", "else", "elif", "fi", "esac", "{", "}", "!", "time", ";;",
})
# Keyword whose remainder is a COMMAND and must still be checked: drop the keyword
# and keep walking the same segment.
_COND_KEYWORDS = frozenset({"if", "while", "until"})
# Keyword whose remainder is DATA, not a command — `for f in *.log`, `select x in a b`,
# `case "$x" in`. The shell expands that word list, it never executes it. Any command
# substitution hiding inside is caught separately, by _deterministic_read.
_DATA_KEYWORDS = frozenset({"for", "select", "case"})
# A `case` branch label: `x)`, `*.log)`, `[0-9]*)`. Requires no parens inside, so it
# cannot swallow a subshell group (handled on its own path in _segment_is_read).
_CASE_LABEL = re.compile(r"^[^()\s]+\)$")
# A function-definition header: `name()`.
_FUNC_DEF = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)$")
_READ_SUB = {
    # push added 2026-08-14: a force-less push is content-inert on the local side and
    # any force variant is already hard-denied above, unconditionally, before this
    # allowlist is ever consulted -- so "push" reaching here is guaranteed force-less.
    # check-ignore/ls-remote/diff-tree/symbolic-ref/var/count-objects added 2026-08-19
    # (pure reads, measured blocked). NOTE: "remote" is no longer decided here — it
    # moved to _GIT_SUB_ACTION_READ because plain membership auto-approved
    # `git remote add origin <url>`, which is a write. branch/tag/stash/worktree/
    # submodule/config are likewise gated in _git_is_read, not by membership.
    "git": frozenset({"status", "log", "diff", "show", "merge-base", "range-diff",
                      "rev-parse", "rev-list", "blame", "ls-files", "ls-tree", "describe",
                      "shortlog", "reflog", "cat-file", "for-each-ref", "show-ref",
                      "fetch", "whatchanged", "grep", "version", "push",
                      "check-ignore", "ls-remote", "diff-tree", "symbolic-ref", "var",
                      "count-objects", "verify-commit", "check-attr"}),
    # diff/events/auth/wait added 2026-08-14 (none mutate cluster state). "auth" needs
    # a further check below -- `auth can-i` is read but `auth reconcile -f rbac.yaml`
    # applies RBAC changes, so membership here alone is not enough for it. Deliberately
    # excluded: exec/cp/attach/debug/run (execute code or create a resource),
    # port-forward/proxy (open a network tunnel -- not a "read" of anything), config
    # (can mutate the local kubeconfig, e.g. delete-context/use-context).
    # kustomize added 2026-08-19: it renders manifests locally and touches no cluster.
    # The measured breakdown otherwise VALIDATES this set — the blocked kubectl weight
    # is exec:384, port-forward:89, rollout:54, annotate:40, apply:28, which are all
    # excluded on purpose and should keep asking.
    "kubectl": frozenset({"get", "describe", "logs", "top", "explain", "api-resources",
                          "api-versions", "version", "cluster-info", "diff", "events",
                          "auth", "wait", "kustomize"}),
    "helm": frozenset({"template", "diff", "get", "list", "status", "show", "history",
                       "version", "lint"}),
    "terraform": frozenset({"plan", "show", "output", "validate", "version", "providers"}),
    "docker": frozenset({"ps", "images", "logs", "inspect", "version", "info", "top",
                         "stats", "history"}),
    # Decided POSITIONALLY by _gh_is_read, not by membership — see the note there.
    "gh": frozenset({"pr", "issue", "run", "repo", "release", "api", "status",
                     "workflow", "search", "cache", "label", "ruleset"}),
    # glab (GitLab CLI) added 2026-08-18. It is the fleet's primary GitLab interface and,
    # per ~/.claude/CLAUDE.md, the ONLY sanctioned one — scripts shell out to `glab api`
    # rather than handling a token, so glab owns the credential. Measured 145 of 986
    # distinct commands over a 4h sample: the second most common head (after python3) that
    # this allowlist could not vouch for, i.e. the largest remaining source of prompts
    # that did not need a human. Group membership alone is NOT sufficient — see
    # _glab_is_read, which requires a read ACTION positionally.
    "glab": frozenset({"api", "mr", "issue", "ci", "pipeline", "repo", "release",
                       "variable", "schedule", "label", "auth", "config", "version"}),
}

# `glab api` defaults to GET. It becomes a write when given an explicit write method, or
# — the easy one to miss — as soon as ANY field/body flag appears, because glab (like gh)
# silently switches to POST then, with no -X anywhere in the command.
_GLAB_WRITE_METHOD = re.compile(r"(-X|--method)[=\s]*(post|put|patch|delete)", re.I)
_GLAB_API_BODY = re.compile(r"(^|\s)(-F|--field|-f|--raw-field|--input|--data)\b")
# Read actions, matched POSITIONALLY (the token right after the group), never by searching
# the segment. Searching would approve `glab mr merge --description "list of changes"` on
# the word "list" inside a quoted string — the weakness the `gh` branch above still has.
_GLAB_READ_ACTIONS = frozenset({"list", "view", "diff", "status", "get", "trace"})
# --- gh, the GitHub CLI (rewritten positionally 2026-08-19) -----------------
# The previous rule was `sub in _READ_SUB["gh"] and re.search(r"\b(view|list|status|
# get)\b", seg)` — a SUBSTRING search over the whole segment. That is the exact
# weakness _glab_is_read was written to avoid, and it was a live false-approve here:
#   gh pr merge 42 --body "list of changes"
# contains "list", so the gate auto-approved a MERGE. Matching the action positionally
# (the token right after the group) fixes it: the action there is `merge`.
# Measured 632 blocked invocations (api:223, pr:372), so this is also the second
# largest named recovery — it is both the safer rule AND the wider one.
# `watch` polls a run to completion and prints progress — a read, and the only
# regression the 30-day replay found when this rule replaced the substring search.
# `download` is deliberately absent: `gh run download` writes artifacts to disk.
_GH_READ_ACTIONS = frozenset({"list", "view", "diff", "status", "checks", "watch"})
_GH_WRITE_METHOD = re.compile(r"(-X|--method)[=\s]*(post|put|patch|delete)", re.I)
# Same silent-POST trap as glab: any field/body flag flips `gh api` to POST with no -X.
_GH_API_BODY = re.compile(r"(^|\s)(-F|--field|-f|--raw-field|--input)\b")

# --- git read subcommands (added 2026-08-19) --------------------------------
# `branch`/`tag`/`stash`/`worktree`/`submodule`/`config` are all dual-use, so none can
# be a plain _READ_SUB member — each is gated on its own flags or action. Measured
# blocked weight: branch 172, check-ignore 36, worktree 30, stash 27, config 13,
# ls-remote 11. (add 434 / checkout 259 / commit 53 stay withheld, correctly.)
_GIT_BRANCH_WRITE = re.compile(
    r"(^|\s)(-d|-D|-m|-M|-c|-C|-u|--delete|--move|--copy|--set-upstream-to"
    r"|--unset-upstream|--edit-description|--force)\b")
_GIT_TAG_WRITE = re.compile(
    r"(^|\s)(-d|-a|-s|-m|-f|--delete|--annotate|--sign|--message|--force)\b")
# Only the reading forms. Never --edit (opens an editor on .git/config, which
# ~/.claude/CLAUDE.md classes as credential-bearing) and never a bare `git config k v`.
_GIT_CONFIG_READ = re.compile(r"(^|\s)(--get|--get-all|--get-regexp|--list|-l)\b")
_GIT_SUB_ACTION_READ = {
    "stash": frozenset({"list", "show"}),
    "worktree": frozenset({"list"}),
    "submodule": frozenset({"status", "summary"}),
    # A bare `git remote` / `git remote -v` lists (flags are stripped before the action
    # is read, so the no-action path below covers -v). `git remote add` is a write —
    # and note that BEFORE this table, "remote" was a plain _READ_SUB member, so
    # `git remote add origin <url>` auto-approved. This is a fix, not just a widening.
    "remote": frozenset({"show", "get-url"}),
}

# --- awk (added 2026-08-19, guarded) ----------------------------------------
# awk is read-only in its overwhelmingly common use (`awk '{print $2}'`; 409 blocked
# invocations in the sample) but it is a full language: system() and a `|` pipe execute
# commands, getline can read from one, and close()/ENVIRON reach outside the record
# stream. Same shape as the existing find/sed/curl guards — allow the inert form, ask
# about anything carrying an execution construct. A `print > "file"` write needs no
# clause here: _deterministic_read's redirect check already refuses the whole command.
_AWK_EXEC = re.compile(r"(\bsystem\s*\(|\bclose\s*\(|\bENVIRON\b|\bgetline\b|\|)")

# --- docker compose (added 2026-08-19) --------------------------------------
# `docker`'s subcommand set had no `compose` entry, so every compose command fell
# through (67 blocked). Read verbs only; up/down/build/restart all mutate.
_COMPOSE_READ = frozenset({"ps", "logs", "config", "top", "images", "ls", "version"})

# --- systemctl (added 2026-08-19, positional) -------------------------------
# Gated to the query verbs. start/stop/restart/enable/disable/daemon-reload all change
# system state and keep asking.
_SYSTEMCTL_READ = frozenset({
    "status", "show", "list-units", "list-unit-files", "list-timers", "list-sockets",
    "is-active", "is-enabled", "is-failed", "cat", "get-default", "show-environment",
})

# curl/wget become "modify" if they carry a write method, a request body, an upload,
# or write the response to a file.
_HTTP_WRITE = re.compile(
    r"(-X\s*(post|put|patch|delete)|--data\b|--data-[a-z]+|(^|\s)-d\b"
    r"|--upload-file|(^|\s)-T\b|(^|\s)-[oO]\b|--output)", re.I)

# mv auto-approves (added 2026-08-14) EXCEPT when it touches a credential-ish path
# (same spirit as block-credential-dump.sh's CRED list, trimmed to what's relevant
# here) or forces an overwrite with -f. This is deliberately narrow -- it is not a
# general data-loss guard, just enough to keep a careless mv off dotfiles/secrets.
_MV_SENSITIVE = re.compile(
    r"(\.ssh\b|\.aws\b|\.kube\b|\.npmrc\b|\.netrc\b|id_rsa|id_ed25519"
    r"|\.pem\b|\.env\b|\.git/config|secrets?\.ya?ml)", re.I)


# Global flags that take a separate-token value and can appear BEFORE the
# subcommand (e.g. `kubectl --context prod get`, `git -C /repo show`). Their value
# is skipped when locating the real subcommand. --endpoint-url/--output added
# 2026-08-14 for `aws --profile x --region y ec2 describe-instances`-style commands.
_VALUE_FLAGS = frozenset({
    "-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user", "--as",
    "--server", "-s", "-C", "--git-dir", "--work-tree", "--profile", "--region", "-o",
    "--endpoint-url", "--output",
    # -R/--repo/--host added 2026-08-18 for `glab -R owner/repo mr list` (gh takes -R the
    # same way, so this improves that branch too). Safe to add globally: this set is only
    # consulted by _subcommand/_strip_leading_flags, which run for _READ_SUB commands and
    # aws — none of which takes a valueless -R. It never reaches `grep -R`, since grep
    # short-circuits as a _READ_CMDS member before any flag parsing happens.
    "-R", "--repo", "--host",
})


def _subcommand(toks):
    """First non-flag token (skipping flags and known flag-values) = the subcommand."""
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            if "=" not in t and t in _VALUE_FLAGS:
                skip = True
            continue
        return t
    return ""


def _strip_leading_flags(toks):
    """Drop a leading run of flags (and their value-tokens) from toks. Used by the
    aws check, which needs TWO positional tokens (service, then action) rather than
    _subcommand's single one."""
    i = 0
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if ("=" not in toks[i] and toks[i] in _VALUE_FLAGS) else 1
    return toks[i:]


def _aws_is_read(toks):
    """AWS CLI read heuristic (added 2026-08-14): `aws s3 ls`/`presign`, or
    `aws <service> <get-*|describe-*|list-*|head-*>` -- the same prefix convention
    AWS's own read-only IAM policy examples use. Anything else (create-/put-/delete-/
    update-/modify-/run-/terminate-/tag-/attach-/... or a shape this can't parse)
    returns False and falls through to the LLM -- this never silently approves an
    unrecognized action."""
    rest = _strip_leading_flags(toks[1:])                # drop 'aws' + global flags
    if not rest:
        return False
    service = rest[0].lower()
    rest = _strip_leading_flags(rest[1:])
    if not rest:
        return False
    action = rest[0].lower()
    if service == "s3":
        return action in ("ls", "presign")
    return action.startswith(("get-", "describe-", "list-", "head-"))


def _glab_is_read(toks, seg):
    """GitLab CLI read heuristic (added 2026-08-18).

    Two shapes, handled separately because they carry their read/write intent in
    different places:

      `glab api <path>`      GET by default. A write only when an explicit write method
                             is given, or when ANY field/body flag appears — glab, like
                             gh, silently switches to POST then, with no -X in sight.
                             That second case is the one worth being careful about: it
                             is a write that does not look like one.
      `glab <group> <action>` needs the action to be a read verb, checked POSITIONALLY.

    Positional matters. Searching the whole segment for a read verb — what the `gh`
    branch does — approves `glab mr merge --description "list of changes"` on the word
    "list" sitting inside a quoted string. Taking the token right after the group closes
    that: the action there is `merge`, and merge is not a read.

    Anything unrecognized returns False and falls through to the LLM, so a new glab
    subcommand is never silently approved by this.
    """
    rest = _strip_leading_flags(toks[1:])            # drop 'glab' + global flags (-R, --host)
    if not rest:
        return False
    group = rest[0].lower()
    if group == "version":
        return True                                   # no action token; inert
    if group == "api":
        return not _GLAB_WRITE_METHOD.search(seg) and not _GLAB_API_BODY.search(seg)
    if group not in _READ_SUB["glab"]:
        return False
    rest = _strip_leading_flags(rest[1:])
    return bool(rest) and rest[0].lower() in _GLAB_READ_ACTIONS


# --- Shell text that is DATA, not commands (added 2026-08-19) ---------------
# Measured over 30 days of real traffic (14,338 Bash calls, 8,601 withheld by this
# gate): 29.8% of everything withheld contained a heredoc, a backslash-newline
# continuation, or a `#` comment line. None of those are policy calls — they are
# MIS-PARSES. _split_top_level treats `\n` as a command separator (correctly, since
# 2026-08-18), so a heredoc body and a continued line both arrive as bogus "segments"
# whose leading token is Python source, prose, or a terminator word. The blame
# histogram was full of `import`, `print(f"`, `EOF`, `PYEOF`, `#`, `The` — every one
# failing on an "unrecognized command head" that was never a command at all.
_LINE_CONT = re.compile(r"\\\n")
# `<<TAG`, `<<'TAG'`, `<<"TAG"`, `<<-TAG`. Quoting matters and is captured: a QUOTED
# tag makes the body fully literal, while an UNQUOTED one still expands `$(...)`, so
# an unquoted body has to keep flowing to _command_subs.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(cmd):
    """Remove heredoc BODIES for segmentation. Returns (text, [executable_bodies]).

    A heredoc body is stdin DATA handed to a command; the shell never executes it, so
    it must not be segmented as commands. Safety is unchanged because the COMMAND HEAD
    is still classified normally: `python3 - <<'PY'` and `bash <<EOF` keep failing on
    `python3`/`bash` exactly as before, and a `cat > f <<'EOF'` still trips the redirect
    check. What changes is that a read-only command is no longer blocked by its own
    payload.

    Unquoted-tag bodies are RETURNED rather than discarded: `<<EOF` still performs
    command substitution, so those bodies must still be scanned.
    """
    out, live, i, n = [], [], 0, len(cmd)
    while i < n:
        m = _HEREDOC.search(cmd, i)
        if not m:
            out.append(cmd[i:])
            break
        nl = cmd.find("\n", m.end())
        if nl == -1:                       # tag with no body in this command string
            out.append(cmd[i:])
            break
        out.append(cmd[i:nl + 1])
        quoted, tag = bool(m.group(1)), m.group(2)
        j, body = nl + 1, []
        while j < n:
            eol = cmd.find("\n", j)
            line = cmd[j:eol if eol != -1 else n]
            if line.strip() == tag:        # terminator (also covers <<- 's tab indent)
                j = eol + 1 if eol != -1 else n
                break
            body.append(line)
            if eol == -1:
                j = n
                break
            j = eol + 1
        if not quoted:
            live.append("\n".join(body))
        i = j
    return "".join(out), live


def _gh_is_read(toks, seg):
    """GitHub CLI read heuristic (added 2026-08-19). Positional, like _glab_is_read.

    Replaces a substring search that approved `gh pr merge --body "list of changes"`
    on the word "list" sitting inside a quoted flag value. See _GH_READ_ACTIONS.
    """
    rest = _strip_leading_flags(toks[1:])              # drop 'gh' + global flags (-R)
    if not rest:
        return False
    group = rest[0].lower()
    if group in ("version", "status"):                 # no action token; inert
        return True
    if group == "api":
        # GET by default; a write only via an explicit method or ANY body/field flag.
        return not _GH_WRITE_METHOD.search(seg) and not _GH_API_BODY.search(seg)
    if group not in _READ_SUB["gh"]:
        return False
    rest = _strip_leading_flags(rest[1:])
    return bool(rest) and rest[0].lower() in _GH_READ_ACTIONS


def _git_is_read(toks, seg, sub):
    """git read heuristic for the dual-use subcommands (added 2026-08-19).

    Only reached for a `sub` this function claims; everything else stays on plain
    _READ_SUB membership. Each case inspects THIS segment rather than trusting the
    caller's _DENY pre-check, matching the existing `push` and `mv` precedent.
    """
    if sub == "push":
        # Self-contained, not just relying on _DENY: membership can't see flags.
        return "-f" not in toks and not re.search(r"--force", seg, re.I)
    if sub == "branch":
        return not _GIT_BRANCH_WRITE.search(seg)
    if sub == "tag":
        return not _GIT_TAG_WRITE.search(seg)
    if sub == "config":
        return bool(_GIT_CONFIG_READ.search(seg))
    if sub in _GIT_SUB_ACTION_READ:
        after = _strip_leading_flags(toks[1:])         # drop 'git' + global flags
        idx = after.index(sub) + 1 if sub in after else len(after)
        action = _strip_leading_flags(after[idx:])
        if not action:
            # A bare `git stash` STASHES; a bare `git remote`/`git worktree` lists.
            return sub in ("remote", "worktree", "submodule")
        return action[0].lower() in _GIT_SUB_ACTION_READ[sub]
    return sub in _READ_SUB["git"]


def _split_top_level(cmd):
    """Split on &&, ||, ;, | -- but ONLY at paren-depth 0 and outside quotes, so a
    subshell/group `( a || b )` or a quoted string containing these characters isn't
    torn into unparseable fragments (each half then failing `_segment_is_read` on an
    unrecognized leading token like a stray `(git`). Added 2026-08-17: the naive
    `re.split(r"&&|\\|\\||;|\\|", cmd)` this replaces broke exactly this shape on a
    fully read-only compound command, forcing it to the LLM/modify fallback."""
    segs, buf, depth, quote, i, n = [], [], 0, None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == quote and cmd[i - 1] != "\\":
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "(":
            depth += 1
            buf.append(c)
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            buf.append(c)
            i += 1
            continue
        if depth == 0:
            if cmd[i:i + 2] in ("&&", "||"):
                segs.append("".join(buf)); buf = []; i += 2
                continue
            # Newline is a command separator too (added 2026-08-18): a multi-line
            # script body would otherwise arrive as ONE segment, whose first token is
            # the only thing _segment_is_read ever inspects — so every command after
            # line 1 went unchecked. Permissive, not merely imprecise: `cat a` on line
            # one made `git commit -am x` on line two look like a plain read. (The
            # outright destructive cases were already covered, since _DENY matches
            # against the whole command string rather than per segment.)
            if c in (";", "|", "\n"):
                segs.append("".join(buf)); buf = []; i += 1
                continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return segs


# --- Test / check runners (added 2026-08-18) --------------------------------
# POLICY, not a read-only claim. Running a project's test suite executes that
# project's own code and can write fixtures, touch a scratch database, or hit a local
# service — it is genuinely not "read". Alex authorised it explicitly ("I'm fine with
# running tests, don't ask me") after a `python -m pytest` run sat parked on an
# approval prompt while he was working elsewhere.
#
# Deliberately narrow: the runner has to be RECOGNISED. An arbitrary interpreter
# invocation (`python -c ...`, `python some_script.py`) still asks, because "it's a
# python command" says nothing about what it does. Formatters that rewrite files by
# default (black, isort, prettier) are excluded on purpose; ruff/eslint qualify only
# in their non-writing modes.
_TEST_RUNNERS = frozenset({
    "pytest", "tox", "nox", "unittest", "jest", "vitest", "mocha", "rspec",
    "mypy", "pyright", "flake8", "pylint", "tsc",
    # added 2026-08-19 — same policy, all non-writing by default
    "shellcheck", "bats", "phpunit", "rubocop", "bandit", "ava", "tap",
})
_PY_INTERPRETERS = frozenset({"python", "python2", "python3", "py"})
# `uv run pytest`, `poetry run pytest`, ... — resolve to whatever they actually run.
_RUNNER_WRAPPERS = frozenset({"uv", "poetry", "pipenv", "pdm", "hatch", "rye"})


def _is_test_runner(toks, seg):
    """True for a recognized test/lint/typecheck invocation. See _TEST_RUNNERS."""
    if not toks:
        return False
    head = os.path.basename(toks[0])
    rest = toks[1:]
    if head in _RUNNER_WRAPPERS:
        return _is_test_runner(rest[1:], seg) if rest[:1] == ["run"] else False
    if head in _PY_INTERPRETERS or re.match(r"^python[0-9.]*$", head):
        # Any interpreter path qualifies, a venv's included — it's the MODULE that
        # decides. `-c` never qualifies: that is arbitrary inline code.
        if "-c" in rest or "-m" not in rest:
            return False
        i = rest.index("-m")
        return len(rest) > i + 1 and rest[i + 1] in _TEST_RUNNERS
    if head in ("npm", "yarn", "pnpm"):
        sub = _subcommand(rest)
        if sub == "test":
            return True
        return sub == "run" and bool(re.search(r"\brun\s+(test|lint|typecheck)\b", seg))
    if head in ("npx", "pnpx", "bunx"):
        # `npx jest` / `npx tsc` / `npx playwright test` — 207 blocked invocations, of
        # which jest:84 + playwright:63 + tsc:55 = 202 are recognized runners we
        # already trust when invoked directly. Drop npx's own flags (-y, --no-install)
        # and re-dispatch, so an unrecognized package still asks.
        inner = [t for t in rest if not t.startswith("-")]
        return bool(inner) and _is_test_runner(inner, seg)
    if head == "playwright":
        return _subcommand(rest) == "test"
    if head == "deno":
        return _subcommand(rest) in ("test", "lint", "check")   # `deno fmt` rewrites
    if head in ("go", "cargo"):
        return _subcommand(rest) == "test"
    if head == "ruff":                       # `ruff format` and `--fix` rewrite files
        return _subcommand(rest) != "format" and not re.search(r"--fix\b", seg)
    if head == "eslint":
        return not re.search(r"--fix\b", seg)
    return head in _TEST_RUNNERS


def _segment_is_read(seg):
    seg = seg.strip()
    if not seg:
        return True
    # A comment executes nothing. Multi-line command bodies are full of these (617 of
    # the 8,601-call withheld sample carried one) and each arrived as its own segment,
    # failing on `#` as an "unrecognized command head".
    if seg.startswith("#"):
        return True
    # A whole parenthesized group `( ... )` -- recurse on its interior with the same
    # depth/quote-aware splitter rather than treating the literal `(cmd` as a command.
    if seg.startswith("(") and seg.endswith(")"):
        return all(_segment_is_read(s) for s in _split_top_level(seg[1:-1]))
    toks = seg.split()
    while toks:  # strip VAR=val prefixes, shell syntax, and benign command wrappers
        b = os.path.basename(toks[0])
        if toks[0] in _STRUCTURAL:
            toks = toks[1:]
        elif _SAFE_REDIRECT.fullmatch(toks[0]):
            # A discard/fd-dup redirect that ends up LEADING its segment, which is
            # what `... ; done 2>/dev/null` produces once `done` is stripped. Only a
            # full match is dropped, so a real `> file` is never silently discarded —
            # and _deterministic_read has already refused any command containing one
            # before a segment gets here.
            toks = toks[1:]
        elif toks[0] in _DATA_KEYWORDS:
            # `for f in <words>` / `case "$x" in` — the remainder is a word list the
            # shell expands, never a command. Nothing here can execute, so the header
            # is read by construction; the loop BODY is a separate segment and is
            # checked on its own.
            return True
        elif toks[0] in _COND_KEYWORDS:
            toks = toks[1:]      # `if`/`while`/`until <cmd>` — the condition runs
        elif _FUNC_DEF.match(toks[0]) or _CASE_LABEL.match(toks[0]):
            toks = toks[1:]
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]
        elif b == "timeout":
            toks = toks[2:] if len(toks) > 1 and re.match(r"^[\d.]+[smhd]?$", toks[1]) else toks[1:]
        elif b in ("command", "nice", "stdbuf", "nohup"):
            toks = toks[1:]
        else:
            break
    if not toks:
        return True
    cmd = os.path.basename(toks[0])
    if cmd in ("curl", "wget"):
        return not _HTTP_WRITE.search(seg)
    if cmd == "sed":
        return "-i" not in toks                                    # not in-place
    if cmd == "find":
        return not re.search(r"-(delete|exec|execdir|ok|fprint|fls|fprintf)\b", seg)
    if cmd == "mv":
        return "-f" not in toks and not _MV_SENSITIVE.search(seg)
    if cmd == "aws":
        return _aws_is_read(toks)
    if cmd == "glab":
        # Ahead of the generic _READ_SUB membership below on purpose: for glab the group
        # alone ("mr", "ci", "api") says nothing about read vs write, so it must never be
        # allowed to pass on membership.
        return _glab_is_read(toks, seg)
    if cmd == "gh":
        # Ahead of generic membership, same reason as glab: for gh the GROUP alone
        # ("pr", "api") says nothing about read vs write.
        return _gh_is_read(toks, seg)
    if cmd in ("awk", "gawk", "mawk", "nawk"):
        return not _AWK_EXEC.search(seg)
    if cmd == "systemctl":
        return _subcommand(toks[1:]).lower() in _SYSTEMCTL_READ
    if cmd in ("export", "set", "declare", "typeset", "readonly", "local", "unset"):
        # These never execute anything — but two of their forms DUMP every shell
        # variable with its value: the bare `export`/`set`/`declare`, and the explicit
        # `-p` print form. On this host that is a credential-disclosure path (see
        # ~/.claude/CLAUDE.md), so those two keep asking. Everything else is an
        # assignment or a shell-option change and is inert — note `set -euo pipefail`
        # carries `pipefail` as a VALUE for -o, so this cannot require all-flags.
        return len(toks) > 1 and "-p" not in toks[1:]
    if cmd in _READ_CMDS:
        return True
    if cmd in _READ_SUB:
        sub = _subcommand(toks[1:])                                # skip leading flags (e.g. --context X)
        if cmd == "git":
            return _git_is_read(toks, seg, sub)
        if cmd == "kubectl" and sub == "auth":  # can-i is read; reconcile applies RBAC
            return bool(re.search(r"\bcan-i\b", seg))
        if cmd == "docker" and sub == "compose":
            after = _strip_leading_flags(toks[1:])
            idx = after.index("compose") + 1 if "compose" in after else len(after)
            action = _strip_leading_flags(after[idx:])
            return bool(action) and action[0].lower() in _COMPOSE_READ
        return sub in _READ_SUB[cmd]
    # Last: a recognized test/check runner. Placed here, after every read-only check,
    # so it only ever widens the gate and never shadows one of them.
    return _is_test_runner(toks, seg)


# Redirects that DISCARD output or duplicate a stream (2>/dev/null, &>/dev/null,
# 2>&1) don't persist anything new -- stripped before the write-redirect check so
# they don't force an otherwise all-read compound command to the LLM/modify
# fallback. A real write (`> file`, `>> file`) is untouched by this and still
# trips the check below. Added 2026-08-17: a fully read-only `cd && echo
# "$(git rev-parse ...)" && ...` with three `2>/dev/null`s was falling through
# to the LLM on this alone.
_SAFE_REDIRECT = re.compile(r"[12&]?>&?(/dev/null|[0-2]\b)")


def _command_subs(cmd):
    """Every `$(...)` / backtick substitution body in `cmd`, nested ones included.

    _segment_is_read only ever inspects a segment's leading token, so before
    2026-08-18 `echo "$(git commit -am x)"` classified as a read `echo` — the
    substitution body was never looked at. That hole predates control-flow support
    but matters far more alongside it, because `for f in $(...)` is the idiomatic
    loop shape and would otherwise have become a blanket bypass.

    `$((arith))` is skipped rather than returned: arithmetic expansion evaluates
    numbers, it cannot run a command, and treating its body as one made every
    `$((i+1))` fail on an unrecognized command head.
    """
    out, i, n = [], 0, len(cmd)
    while i < n:
        if cmd.startswith("$((", i):                  # arithmetic — executes nothing
            depth, j = 0, i + 1
            while j < n:
                if cmd[j] == "(":
                    depth += 1
                elif cmd[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            i = j
            continue
        if cmd.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if cmd[j] == "(":
                    depth += 1
                elif cmd[j] == ")":
                    depth -= 1
                j += 1
            out.append(cmd[i + 2:j - 1] if depth == 0 else cmd[i + 2:])
            i = j
            continue
        if cmd[i] == "`":
            j = cmd.find("`", i + 1)
            if j == -1:
                break
            out.append(cmd[i + 1:j])
            i = j + 1
            continue
        i += 1
    # Recurse: the scan above jumps over a whole `$(...)` span, so a substitution
    # nested inside another is not found by the loop itself. Bodies strictly shrink,
    # so this terminates.
    return out + [inner for body in out for inner in _command_subs(body)]


def _deterministic_read(cmd):
    """True only if EVERY segment is a known read-only operation. Any output redirect
    (>, >>) that isn't a discard/fd-dup, or an unrecognized command, falls through to
    the LLM."""
    # Normalise away the two shapes that are not command separators before anything
    # else looks at the text (2026-08-19). A backslash-newline is a LINE continuation:
    # the shell joins it into one command, but _split_top_level's newline rule tore it
    # into fragments, so the tail of every wrapped command (`  --query '...'`) was
    # classified as its own command head. Heredoc bodies are stdin data, not commands.
    cmd = _LINE_CONT.sub(" ", cmd)
    shell, live_bodies = _strip_heredocs(cmd)
    # Redirect check runs on the heredoc-stripped text: a `>` inside a heredoc body is
    # payload, while `cat > f <<'EOF'` keeps its redirect in `shell` and still fails.
    if ">" in _SAFE_REDIRECT.sub("", shell):
        return False
    # Substitution bodies first — they run BEFORE the command that embeds them, so a
    # modifying one makes the whole command modifying no matter how read-only the
    # embedding command looks. Unquoted heredoc bodies are scanned here too, since
    # `<<EOF` (unlike `<<'EOF'`) still expands $(...).
    subs = _command_subs(shell)
    for body in live_bodies:
        subs += _command_subs(body)
    for body in subs:
        if not all(_segment_is_read(s) for s in _split_top_level(body)):
            return False
    return all(_segment_is_read(s) for s in _split_top_level(shell))

_PROMPT = """You are the middle-man between an autonomous coding agent and its human operator on Slack. The agent paused to ask permission to use a tool. Reply with ONLY a compact JSON object:

{{"decision":"read|modify","category":"<2-4 word label>","summary":"<one or two sentences>"}}

decision rules:
- "read" = the command only INSPECTS and does not change files, branches, remotes, deployed resources, or download-and-run / install anything. Treat ALL of these as read: git status/log/diff/show/branch -l/rev-parse/merge-base/range-diff/blame/ls-files, `git fetch` (updates only remote-tracking refs — safe), a force-less `git push` (no -f/--force/--force-with-lease), kubectl get/describe/logs/top/diff/events/auth/wait, helm template/diff/get/list, terraform plan, docker ps/images/logs/inspect, AWS CLI actions named get-*/describe-*/list-*/head-* (and plain `aws s3 ls`/`presign`), a plain `mv` that doesn't touch a credential-ish path (.ssh/.aws/.kube/.env/id_rsa/...) and isn't forced with -f, and cat/ls/grep/find(without -delete)/head/tail (incl. -f follow)/wc/echo/which/jq.
- "modify" = changes state: writes/edits/deletes files, redirects to a file (> or >>), a FORCED git push (-f/--force/--force-with-lease), git add/commit/reset/checkout/merge/rebase/stash, kubectl apply/create/delete/patch/scale/rollout/edit/exec/cp/attach/debug/run/port-forward, AWS CLI actions named create-*/put-*/delete-*/update-*/modify-*/run-*/terminate-*/tag-*/attach-* (or any shape you can't confidently place in the read list above), helm install/upgrade/uninstall, terraform apply/destroy, package installs, sudo, or piping into a shell.
- Compound command (&&, ||, ;, |): "read" only if EVERY part is read; one modifying part makes the whole "modify".
- BE DECISIVE: if every part is plainly an inspection, answer "read" — do NOT hedge to "modify" just because the domain (git/k8s/deploy) feels operational. Reserve "modify" for an actual state change or genuine ambiguity. Your decision MUST agree with your summary: if the summary concludes nothing is modified, decision is "read".

category: short label, e.g. "read-only inspection", "git fetch", "file edit", "k8s apply", "package install", "delete files".
summary: 1-2 sentences for the operator — what the agent wants to do, what it's pertinent to, and a brief HONEST safety read. Refer to it as "the agent". Do not invent facts.

Tool: {name}
Input: {inp}

JSON:"""


def _trunc(s, n=240):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        root = os.path.realpath(__file__)
        for _ in range(4):  # tmux/mac/tmux-scripts/ -> repo root
            root = os.path.dirname(root)
        for ln in open(os.path.join(root, ".env"), errors="replace"):
            ln = ln.strip()
            if ln.startswith("ANTHROPIC_API_KEY=") and "=" in ln:
                os.environ["ANTHROPIC_API_KEY"] = ln.split("=", 1)[1].strip()
                break
    except Exception:
        pass


def _pin_api_base():
    """Drop an api-base override this process cannot actually reach.

    Root cause of a silent, total outage of this module's LLM tier, found
    2026-08-18. The fleet environment carries, for the CONTAINERS:
        ANTHROPIC_API_BASE=http://host.docker.internal:54777/anthropic
    litellm honours that variable, and `host.docker.internal` does not resolve on the
    macOS host, so every classification died with "[Errno 8] nodename nor servname
    provided, or not known". _llm() catches every exception and returns None, so the
    gate fell back to "modify" and asked a human — for EVERY command the deterministic
    allowlist could not vouch for, with no error surfaced anywhere. The module header
    already documented "default api base (the .env base is container-only)" and
    deliberately loads only ANTHROPIC_API_KEY from .env; what it did not account for is
    the container-only base ALREADY being present in the inherited environment.

    Checked by resolution rather than a hostname blocklist, so this keeps working for
    any future container-only name, and leaves a reachable override (e.g. a local
    nexus-proxy on localhost) alone.
    """
    import socket
    import urllib.parse
    for var in ("ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL"):
        base = os.environ.get(var)
        if not base:
            continue
        host = urllib.parse.urlsplit(base).hostname
        if not host:
            continue
        try:
            socket.getaddrinfo(host, None)
        except OSError:
            os.environ.pop(var, None)


def _deterministic_summary(name, inp):
    short = (name or "").split("__")[-1].lower()
    if short in ("bash", "shell"):
        return f"`{_trunc(inp.get('command', ''))}`"
    if short in ("edit", "multiedit", "write", "notebookedit"):
        return f"`{inp.get('file_path') or inp.get('notebook_path', '?')}`"
    for k in ("command", "url", "query", "path", "file_path", "prompt"):
        if inp.get(k):
            return _trunc(inp[k])
    return name or "a tool call"


def _llm(name, inp):
    """Return (decision, category, summary) from the LLM, or None on any failure."""
    _load_key()
    _pin_api_base()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        # Direct /v1/messages POST rather than litellm (2026-08-19). This module used
        # exactly one litellm feature — a single completion() with one user message —
        # and paid `import litellm` for it on EVERY invocation: measured 1.378 s here,
        # against 0.026 s for the stdlib equivalent. Nothing amortises that, because
        # the hook spawns a fresh process per permission prompt. It was the dominant
        # cost of the whole LLM tier and bought nothing this call site used.
        import urllib.request
        base = (os.environ.get("ANTHROPIC_BASE_URL")
                or os.environ.get("ANTHROPIC_API_BASE")
                or "https://api.anthropic.com").rstrip("/")
        body = json.dumps(inp)[:1500]
        payload = json.dumps({
            "model": MODEL.split("/", 1)[-1],          # strip litellm's "anthropic/"
            "max_tokens": 200,
            "temperature": 0,
            "messages": [{"role": "user",
                          "content": _PROMPT.format(name=name, inp=body)}],
        }).encode()
        req = urllib.request.Request(
            f"{base}/v1/messages", data=payload, method="POST",
            headers={"content-type": "application/json",
                     "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=12) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        text = "".join(b.get("text", "") for b in obj.get("content", [])
                       if isinstance(b, dict)).strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        obj = json.loads(m.group(0))
        decision = "read" if str(obj.get("decision", "")).lower().startswith("read") else "modify"
        category = _trunc(obj.get("category") or "change", 40)
        summary = _trunc(obj.get("summary") or "", 500)
        return decision, category, summary
    except Exception:
        return None


def _emit_modify(category, summary):
    print(json.dumps({
        "name": os.environ.get("AN", ""),
        "pane": os.environ.get("PANE", ""),
        "kind": os.environ.get("KIND", ""),
        "wait_since": os.environ.get("WAIT_SINCE", ""),
        "category": category,
        "summary": summary,
    }))
    sys.exit(10)


def _last_tool_use(transcript_path):
    """Most recent tool_use in the transcript tail. Scans back through the last few
    assistant messages (not just the latest, which may be text-only) so a pending
    tool call is still found when the assistant wrote prose around it."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        lines = deque(open(transcript_path, errors="replace"), maxlen=500)
    except OSError:
        return None
    seen = 0
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for b in reversed(content):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    return (b.get("name") or "", b.get("input") or {})
        seen += 1
        if seen >= 6:                # bound the look-back so we don't grab a stale tool
            break
    return None


def classify(name, inp):
    """Classify a pending tool call. Returns (decision, category, summary) where
    decision is 'read' (safe to auto-approve) or 'modify' (needs a human). This is
    the reusable core shared by the Notification hook (main) and the Agent SDK
    runner's can_use_tool gate (--tool mode)."""
    inp = inp or {}
    short = (name or "").split("__")[-1].lower()
    det = _deterministic_summary(name, inp)
    # 1. Clearly read-only local tools -> read, no LLM.
    if short in READ_TOOLS:
        return "read", "read-only", det
    # 2. Bash: hard denylists (modify) -> read allowlist (auto) -> permissive (auto)
    #    -> LLM. The two denylists are checked together and FIRST; nothing below can
    #    override them, which is what makes the permissive tier safe to enable.
    if short in ("bash", "shell"):
        cmd = (inp.get("command") or "").strip()
        safe = bool(cmd) and not _DENY.search(cmd) and not _DESTRUCTIVE.search(cmd)
        if safe and _deterministic_read(cmd):
            return "read", "read-only inspection", det
        if safe and _PERMISSIVE:
            # Not provably read-only, but not destructive either. Under Alex's bar this
            # is the large middle — python3, file writes, git add/commit, kubectl get
            # variants the allowlist can't parse — and it runs without a model call, so
            # it is also the fastest path in the module.
            return "read", "permitted change", det
        llm = _llm(name, inp)
        if safe and llm and llm[0] == "read":
            return "read", llm[1], (llm[2] or det)
        return "modify", (llm[1] if llm else "shell command"), (llm[2] if llm and llm[2] else det)
    # 3. Local file edits: permitted under the same bar, EXCEPT on a credential-ish
    #    path (same list `mv` is gated on). Editing files is the substance of the work;
    #    every such prompt was reaching a human for no decision anyone wanted to make.
    if _PERMISSIVE and short in ("edit", "multiedit", "write", "notebookedit"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if path and not _MV_SENSITIVE.search(path):
            return "read", "file edit", det
    # 4. Web egress / MCP / unknown -> modify (LLM summarizes). Deliberately NOT
    #    covered by permissive mode: an MCP tool's blast radius is unknowable from
    #    here, and _DESTRUCTIVE only understands shell text.
    llm = _llm(name, inp)
    return "modify", (llm[1] if llm else "needs review"), (llm[2] if llm and llm[2] else det)


def main():
    if os.environ.get("KIND") != "permission_prompt":
        _emit_modify("question", os.environ.get("FB", "needs input"))
    try:
        data = json.load(sys.stdin) or {}
    except Exception:
        data = {}
    tool = _last_tool_use(data.get("transcript_path", ""))
    if not tool:
        _emit_modify("needs review", os.environ.get("FB", "needs input"))
    name, inp = tool
    decision, category, summary = classify(name, inp)
    if decision == "read":
        sys.exit(0)                                       # safe -> auto-approve
    _emit_modify(category, summary)                        # needs a human


if __name__ == "__main__":
    # Direct classification mode for the Agent SDK runner's can_use_tool: read a
    # {"name","input"} JSON on stdin, print {"decision","category","summary"} JSON.
    # Never exits non-zero — the caller fails safe to "modify" on a bad/empty line.
    if "--tool" in sys.argv[1:]:
        try:
            _req = json.load(sys.stdin) or {}
            _d, _c, _s = classify(_req.get("name", ""), _req.get("input") or {})
            print(json.dumps({"decision": _d, "category": _c, "summary": _s}))
        except Exception:
            print(json.dumps({"decision": "modify", "category": "needs review", "summary": ""}))
        sys.exit(0)
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _emit_modify("needs review", os.environ.get("FB", "needs input"))
