#!/usr/bin/env bash
# PreToolUse(Bash) guard: refuse commands that would dump a credential-bearing file
# into the transcript.
#
# WHY THIS EXISTS (2026-07-26): a `grep -n -A6 "cni:" talos-config/controlplane.yaml`
# printed the k8s bootstrap token and the Talos secretbox encryption secret into a session
# transcript. The -A6 asked for six lines of trailing context that were never needed and
# never previewed. Nothing caught it: the LLM-egress scrubber is pattern-based (AKIA,
# sk-ant-, ghp_) and the secretbox secret is a bare 44-char base64 blob with no prefix, so
# no pattern matched. Pattern matching finds the loud subset; high-entropy values without a
# distinctive shape -- exactly what Talos and Kubernetes secrets are -- are invisible to it.
#
# A CLAUDE.md rule alone is a promise to be careful. This is the enforceable half.
#
# It blocks BROAD READS of credential paths. Targeted operations still work, and that is the
# point: assert a property instead of printing a value.
#     ALLOWED:  grep -c / -l / -o, wc, sha256sum, git ls-files, ls, stat, yq on one field
#     BLOCKED:  cat, head, tail, less, strings, xxd, and grep -A/-B/-C on those paths
#
# It ALSO blocks SELF-DUMPING commands (see SELF_DUMPING below) that read a credential
# without naming a path -- git remote -v, git config --list, bare env, kubectl get secret
# -o yaml. These match on command shape alone, because there is no path to match on.
#
# Fails OPEN on internal error (never wedge the session on a guard bug), but fails CLOSED on
# a match. Exit 2 + stderr is the documented PreToolUse blocking contract.

set -uo pipefail

payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

read -r -d '' PY <<'PYEOF'
import json, re, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)                      # unparseable -> fail open

if d.get("tool_name") != "Bash":
    sys.exit(0)

cmd = (d.get("tool_input") or {}).get("command") or ""
if not cmd:
    sys.exit(0)

# A heredoc body is DATA, not commands. A commit message or a doc that merely MENTIONS
# `git remote -v` must not trip the guard -- that false positive surfaced immediately
# (2026-08-12), blocking the very commit that documented the incident.
#
# EXCEPTION: when the heredoc feeds an interpreter, the body really is executed, so it
# stays in scope. `bash <<'EOF' ... EOF` must still be scanned, or stripping bodies would
# be a one-line evasion hole.
# The (?<!\.) guard matters: without it, \bsh\b matches the ".sh" in a FILENAME, so any
# line that merely mentions a shell script (`git add hooks/foo.sh && git commit -F - <<EOF`)
# would be misread as an interpreter line and have its body scanned anyway.
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

scan = strip_heredoc_bodies(cmd)

# Paths that can hold credentials. Substring match on the command text.
CRED = [
    r"talos-config", r"cluster-secrets", r"controlplane\.yaml", r"worker\.yaml",
    r"talosconfig", r"kubeconfig", r"\.kube/config", r"\.git/config",
    r"id_rsa", r"id_ed25519", r"\.pem\b", r"\.p12\b", r"\.pfx\b",
    r"credentials\b", r"\.aws/credentials", r"\.npmrc", r"\.netrc",
    r"secrets?\.ya?ml", r"\.env\b", r"cluster-secrets\.yaml",
]

# Readers that emit unbounded / unpreviewed output.
# Command position: start of string, after a separator, or after whitespace -- but NOT
# immediately after a `-`. Added 2026-08-19: `\bhead\s` matched the `--head` FLAG of
# `gh pr create --head <branch>`, and with any `.env` in the PR body supplying the CRED
# half the guard refused an ordinary PR. `-` is a non-word character, so \b matches
# right inside a flag. Same defect class as `\brm\b` firing on `docker run --rm`, which
# was fixed in notify-classify.py's _DENY the same day.
_CP = r"(?:^|[;&|(`]\s*|(?<![-\w]))"

BROAD = [
    (_CP + r"cat\s", "cat"),
    (_CP + r"bat\s", "bat"),
    (_CP + r"head\s", "head"),
    (_CP + r"tail\s", "tail"),
    (_CP + r"less\s", "less"),
    (_CP + r"more\s", "more"),
    (_CP + r"strings\s", "strings"),
    (_CP + r"xxd\s", "xxd"),
    (_CP + r"od\s", "od"),
    (r"\bbase64\s+-d", "base64 -d"),
    (r"\bgrep\b[^|;&]*\s-[A-Za-z]*[ABC]\s*\d", "grep with -A/-B/-C context"),
    (r"\bsed\s+-n[^|;&]*p\b", "sed -n p"),
    (r"\bawk\b[^|;&]*print\s*\$0", "awk print $0"),
    # An unrestricted interpreter dumps a file as completely as `cat`.
    #
    # WHY THIS EXISTS (2026-08-19): a live NATS admin credential was printed into a
    # durable transcript by `python3 -c` doing open().read().splitlines() over a
    # `.env.example`. The CRED half matched fine (`\.env\b` hits ".env.example"); this
    # BROAD half did not, because no interpreter was listed here. Both halves have to
    # fire, so the command sailed through -- the same two-correct-looking-checks shape
    # as the `git remote -v` incident below.
    #
    # Worse: `python3 -c` was ITEM 4 ON THIS HOOK'S OWN "Do this instead" list. The
    # guard recommended, as the safe alternative, the exact tool that defeated it. That
    # wording assumed targeted field extraction but nothing enforced it, and the
    # operator was following the printed advice. The advice below has been rewritten.
    #
    # Requires a READ PRIMITIVE, not merely an interpreter: `python3 -c "print(1)"`
    # sitting next to an unrelated .env mention is not a dump. Anything that actually
    # opens the file is, including the "print only the key NAMES" shape -- so that shape
    # is no longer advertised; use `grep -oE '^[A-Z_]+=' FILE`, which never reads a value.
    # Span tuning, both directions measured rather than guessed:
    #   [^|;&]* (the neighbouring convention) MISSED `python3 -c "import pathlib;
    #     print(...read_text())"` -- inline interpreter code legitimately contains `;`.
    #   [\s\S]*  matched an interpreter and a read primitive anywhere in one command,
    #     which fired on a PROSE message that merely discussed both. Caught immediately,
    #     on the very message reporting this fix.
    # Settled on: require the -c/-e INLINE-CODE flag (prose naming python without it no
    # longer qualifies) and bound the tail. Deliberately does not cover
    # `python3 script.py` reading a credential -- the hook cannot see a script's
    # contents, so that is out of reach here rather than silently assumed safe.
    (r"\b(?:python[0-9.]*|perl|ruby|node|deno|bun)\b[^|;&]{0,80}\s-(?:c|e)\b[\s\S]{0,300}?"
     r"(?:open\s*\(|read_text\s*\(|readFileSync|read_file|File\.read|IO\.read|slurp|fileinput)",
     "interpreter reading a file (open/read) -- dumps as completely as cat"),
]

# Self-dumping commands: the credential is IMPLICIT. No path is ever typed and no
# general-purpose reader is used, so the CRED-plus-BROAD pair above can never fire --
# both halves pass and the command sails through.
#
# WHY THIS EXISTS (2026-08-12): `git remote -v` printed a fine-grained GitHub PAT
# (embedded in the origin URL) into a transcript. CRED lists `.git/config`, but the
# command never names that path -- it opens it. BROAD lists readers, and `git` is not
# one. Two correct-looking checks both said "allow".
#
# These match on command SHAPE alone and block with no CRED hit required. Targeted
# forms stay allowed on purpose: `printenv PATH`, `git config --get user.email`, and
# `doppler secrets get X --plain` (which captures into a var rather than printing).
SELF_DUMPING = [
    (r"\bgit\s+remote\s+(-v\b|--verbose\b|show\b|get-url\b)",
     "git remote -v/show/get-url (prints .git/config URLs; a PAT may be embedded)"),
    (r"\bgit\s+config\b[^|;&]*(--list\b|-l\b)",
     "git config --list (prints remote URLs and everything else)"),
    (r"\bgit\s+config\b[^|;&]*--get\b[^|;&]*\burl\b",
     "git config --get ...url (the URL is the thing that embeds the token)"),
    # The leading (?:^|[|;&]|\bsudo\s+) requires env/printenv to be in COMMAND position.
    # A bare \b was not enough: \b matches after any non-word char, so a PATH ending in
    # "env" at end-of-command satisfied \b env \s* $ and blocked `ls /tmp/my-env` while
    # blaming the bare-env rule. It also mis-attributed real blocks -- `cat /tmp/x.env`
    # tripped this rule instead of the .env-path-plus-reader pair it actually violates.
    # (?m) so `env` on a non-final line of a multi-line command is still caught; without
    # it, $ only matched end-of-string and a trailing pipeline hid the dump.
    (r"(?m)(?:^|[|;&]|\bsudo\s+)\s*(env|printenv)\s*(\||;|&|>|$)",
     "bare env/printenv (dumps every exported secret at once)"),
    (r"\bkubectl\s+get\s+secrets?\b[^|;&]*-o\s*(yaml|json)",
     "kubectl get secret -o yaml/json (base64 blobs match no scrubber pattern)"),
    # --only-names is exempt (2026-08-19): its own help says "only print the secret
    # names; omit all values", so it cannot disclose by construction -- the same
    # reasoning that already allows `grep -oE '^[A-Z_]+='` in the guidance above.
    # The rule was written against the plaintext table but matched on subcommand
    # alone, so it also refused the one form that is safe, and "doppler is blocked"
    # got mis-filed as a credentials problem for days. Scoped with [^|;&]* so the
    # flag exempts only ITS OWN command segment: `doppler secrets --only-names ;
    # doppler secrets` still blocks on the second half.
    (r"\bdoppler\s+secrets\b(?!\s+get\b)(?![^|;&]*--only-names)",
     "doppler secrets (prints the full plaintext table)"),
    # Credential-store CLIs printing their OWN stored auth. Same implicit-path shape as
    # `git remote -v` below: no path is ever typed, the CLI is not a general-purpose
    # reader, and the value sits in a config file CRED does not list
    # (~/.doppler/.doppler.yaml, ~/.config/gh/hosts.yml). All three checks pass and the
    # command sails through -- the fourth instance of that shape in this file.
    #
    # WHY THIS EXISTS (2026-08-19): `doppler configure --json` printed a live Doppler CLI
    # token into a transcript. SELF_DUMPING already had `doppler secrets`; it was scoped
    # to the wrong subcommand.
    #
    # The generalisable part: REDACTION LIVES IN THE HUMAN-READABLE FORMATTER, and the
    # machine-readable path bypasses it. Measured on this box, lengths only:
    #     doppler configure          -> token-shaped string len=10  (elided)
    #     doppler configure --json   -> token-shaped string len=49  (full)
    #     gh auth status             -> len=36 (asterisk run)
    #     gh auth token              -> len=40 (full PAT)
    # Safe and unsafe forms differ by one flag, which is what a shape-based blocklist is
    # worst at, so every dangerous form is matched explicitly rather than by subcommand.
    #
    # The doppler rule uses two lookaheads from the `doppler` anchor so flag ORDER does
    # not matter (`doppler --json configure` reaches the same config), while [^|;&]*
    # keeps both halves inside one command segment. It deliberately does NOT fire on
    # `doppler secrets --only-names --json`, which carries no values.
    (r"\bdoppler\b(?=[^|;&]*\bconfigure\b)(?=[^|;&]*--json)",
     "doppler configure --json (prints the CLI token in full; the table form redacts it)"),
    (r"\bdoppler\s+configure\s+get\b[^|;&]*\btoken\b",
     "doppler configure get token (prints the CLI token)"),
    (r"\bgh\s+auth\s+token\b",
     "gh auth token (prints the full PAT; gh auth status redacts it)"),
    (r"\bgh\s+auth\s+status\b[^|;&]*(?:--show-token|\s-t\b)",
     "gh auth status --show-token (prints the full PAT)"),
    # Both verified against aws-cli/2.35.21 on alex-nexus: `aws configure help` lists
    # export-credentials, and bare `aws configure get` exits with a usage error rather
    # than printing anything.
    #
    # An earlier revision of this comment claimed aws was not installed on this box. It
    # is, at /snap/bin/aws. That false claim came from reading the output of
    #     command -v aws >/dev/null && aws --version | grep -oE '^aws-cli/[0-9.]+' || echo "NOT on PATH"
    # where `||` catches failure of the WHOLE left side, so a non-matching grep printed
    # the not-installed message. Check for a missing binary with `command -v` on its own
    # line -- never as the head of an && / || chain whose tail can fail independently.
    (r"\baws\s+configure\s+get\b[^|;&]*(?:secret|access_key|session_token)",
     "aws configure get <secret> (prints the raw credential)"),
    (r"\baws\s+configure\s+export-credentials\b",
     "aws configure export-credentials (prints key + secret + session token)"),
    (r"\bhelm\s+get\s+values\b", "helm get values"),
    (r"\bnpm\s+config\s+list\b", "npm config list (may print _authToken)"),
]

# A self-dumping command blocks on its own; otherwise fall through to path-plus-reader.
self_hit = next((label for pat, label in SELF_DUMPING if re.search(pat, scan)), None)

if self_hit:
    reader, path_note = self_hit, "implicit -- the command opens it without naming it"
else:
    cred_hit = next((p for p in CRED if re.search(p, scan)), None)
    if not cred_hit:
        sys.exit(0)

    broad_hit = next((label for pat, label in BROAD if re.search(pat, scan)), None)
    if not broad_hit:
        sys.exit(0)                  # targeted read against a cred path -> allow

    reader, path_note = broad_hit, f"matched /{cred_hit}/"

# Record the block. Until 2026-08-19 neither PreToolUse guard logged anything, so a
# refusal existed only as stderr in one transcript. Deliberately does NOT write the
# command text: this guard exists precisely because such text can carry a credential,
# and moving that hazard from the transcript into a log file would not fix it. Format
# matches notify-classify.py's _log_decision so one awk reads both files.
try:
    # `os` is NOT among this script's top-level imports (json, re, sys only), so it is
    # imported here. Without it every write raised NameError inside this try/except and
    # the guard logged nothing at all — silently, which is the failure mode this whole
    # logging change exists to eliminate.
    import hashlib, os, time
    _log = os.path.join(os.environ.get("NEXUS_TMUX_DIR") or
                        os.path.expanduser("~/.tmux"), "gate-decisions.log")
    if os.path.exists(_log) and os.path.getsize(_log) > 5 * 1024 * 1024:
        os.replace(_log, _log + ".1")
    _head = re.sub(r"[^\w.:-]", "", os.path.basename((cmd.split() or ["-"])[0]))[:32] or "-"
    with open(_log, "a", encoding="utf-8") as _fh:
        _fh.write("{} block credential-guard {} {} {}\n".format(
            int(time.time()), os.environ.get("PANE") or "-", _head,
            hashlib.sha256(cmd.encode("utf-8", "replace")).hexdigest()[:12]))
except Exception:
    pass                                     # logging must never wedge the guard

sys.stderr.write(
    "BLOCKED by block-credential-dump.sh\n\n"
    f"  reader:          {reader}\n"
    f"  credential path: {path_note}\n\n"
    "This command would print a credential-bearing file into the transcript, which is\n"
    "durable and is also sent to the model API. On 2026-07-26 exactly this pattern\n"
    "(grep -A6 on talos-config/controlplane.yaml) leaked the k8s bootstrap token and the\n"
    "Talos secretbox encryption secret. Neither could be un-leaked: Kubernetes has no\n"
    "certificate revocation and the remediation was a CA rotation nobody wanted.\n\n"
    "Do this instead -- assert the property, never print the value:\n"
    "  grep -c PATTERN FILE                 # does it exist / how many\n"
    "  grep -oE 'name:\\s*\\w+' FILE          # extract ONE field, no context\n"
    "  sha256sum FILE | cut -c1-16          # compare without disclosing\n"
    "  grep -oE '^[A-Z_]+=' FILE            # key NAMES only -- never reads a value\n\n"
    "  NOT `python3 -c` / `node -e` on a credential file. That was advertised here\n"
    "  until 2026-08-19, when an interpreter reading a .env printed a live credential\n"
    "  into a transcript. An unrestricted interpreter dumps a file exactly as\n"
    "  completely as cat; the advice assumed targeted extraction and nothing enforced\n"
    "  it. Use the greps above -- they cannot return a value by construction.\n\n"
    "If you genuinely must see raw content, do it in a shell the user controls\n"
    "(prefix the command with ! in the prompt) so it never enters the transcript.\n"
)
sys.exit(2)
PYEOF

printf '%s' "$payload" | python3 -c "$PY"
rc=$?
[ "$rc" -eq 2 ] && exit 2
exit 0
