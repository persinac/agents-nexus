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
# It ALSO opens the file (SECRET_SHAPES) for broad Bash readers and the Read tool: a secret
# pasted into an ordinary notes file is invisible to every path pattern by construction.
#
# Fails OPEN on internal error (never wedge the session on a guard bug), but fails CLOSED on
# a match. Exit 2 + stderr is the documented PreToolUse blocking contract.

set -uo pipefail

payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

read -r -d '' PY <<'PYEOF'
import json, os, re, shlex, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)                      # unparseable -> fail open

tool = d.get("tool_name")
if tool not in ("Bash", "Read"):
    sys.exit(0)

tool_input = d.get("tool_input") or {}
cmd = tool_input.get("command") or ""
if tool == "Bash" and not cmd:
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

# `agent-send.sh <fqdn> "<message>"` hands its argument to the NATS bus as DATA. The
# script reads no files, and the receiving agent gets the text as a user turn, never as
# a command -- so a message that merely NAMES a guarded command must not trip this guard.
#
# Not hypothetical: on 2026-08-20 this refused an inter-agent report ABOUT this guard,
# because the report named a blocked subcommand near a pipe. Same prose false-positive
# class that strip_heredoc_bodies fixed for commit messages, but heredoc stripping cannot
# reach it -- an agent-send payload is a quoted argv element, not a heredoc body.
#
# Command substitution is deliberately NOT stripped: `agent-send.sh x "$(cat ~/.aws/...)"`
# really does read the file, because the shell expands it before agent-send ever runs.
# Same principle as the INTERP carve-out above -- strip what is inert, keep what executes.
# Widened 2026-08-20 from agent-send only to every command whose quoted argument is
# LITERAL PROSE: PR/issue bodies and commit messages. The argument that settles it:
#
#   For literal quoted text this guard cannot prevent the harm it exists to prevent.
#   If a credential is typed inside the quotes it is ALREADY in the transcript by
#   virtue of being typed — the tool_use block is recorded before this hook ever runs,
#   so refusing the command does not un-print anything. What this guard actually stops
#   is a command FETCHING a secret into output, and the fetch mechanism is command
#   substitution, which is still refused below.
#
#   So the carve-out gives up nothing real, and the friction it removes was actively
#   harmful: the workaround it forced was --body-file, whose practical effect is that
#   people stop putting the reasoning in the PR at all.
#
# ONE THING THIS DELIBERATELY GIVES UP, so nobody re-adds the case thinking it was an
# oversight: blocking `git commit -m` on a literal secret had a SECOND-ORDER value —
# keeping it out of git HISTORY. That is a different harm from transcript leakage and
# is outside this guard's charter. It wants gitleaks or a pre-commit secret scanner,
# not a widened PreToolUse regex. Do not restore it here.
_TEXT_PAYLOAD = re.compile(
    r"\bagent-send\.sh\b"
    r"|\bgit\s+commit\b[^|;&]{0,120}(?:-m\b|--message\b)"
    r"|\bgh\s+(?:pr|issue|release)\s+\w+\b[^|;&]{0,160}--(?:body|title)\b"
)
_QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"")


def strip_message_payload(text):
    if not _TEXT_PAYLOAD.search(text):
        return text
    # Command substitution is NEVER stripped: the shell expands it before the carrying
    # command runs, so `--body "$(gh auth token)"` really does fetch the secret.
    return _QUOTED_SPAN.sub(
        lambda m: m.group(0) if ("$(" in m.group(0) or "`" in m.group(0)) else " ",
        text)


scan = strip_message_payload(strip_heredoc_bodies(cmd))

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
    # WHY THIS EXISTS (2026-08-27): `doppler run -- docker compose config nats`, run to
    # check a service's volume mounts during a broker outage, printed
    # NATS_AUTH_CALLOUT_PASS in full. The value had to be rotated.
    #
    # This is the SAME shape as the doppler configure --json entry above -- REDACTION
    # LIVES IN THE HUMAN-READABLE FORMATTER and the machine-readable path bypasses it --
    # but it arrives by a route none of the other rules can see:
    #   * the command names no credential path, so path-plus-reader cannot fire;
    #   * the file it DOES name is an ordinary docker-compose.yaml that contains no
    #     secret at all -- the secret exists only in the environment and is
    #     INTERPOLATED IN at render time. Reading the YAML is safe; rendering it is not.
    # `docker inspect` is the same disclosure by a different verb: it prints the resolved
    # Config.Env of a running container.
    #
    # Scoped to the two rendering subcommands rather than to `docker`, because the vast
    # majority of docker usage discloses nothing and a blanket rule would be ignored.
    # `docker compose config` and bare `docker config` (swarm) both match: the optional
    # group keeps one pattern covering both spellings.
    #
    # ⚠️ NOT exempted: --services / --volumes / --images, which print only names. They
    # are safe by construction, but they are also one edit away from the dangerous form,
    # and the doppler --only-names precedent shows how much comment is needed to keep a
    # narrow exemption honest. A false block here costs one rephrase; a false allow costs
    # a rotation. If the names-only forms become common, exempt them deliberately -- with
    # [^|;&]* scoping so the flag only exempts its own command segment.
    (r"\bdocker(\s+compose)?\s+config\b",
     "docker compose config (renders the RESOLVED environment; secrets that live only "
     "in env are interpolated into the output even though the YAML holds none)"),
    (r"\bdocker\s+inspect\b",
     "docker inspect (prints the container's resolved Config.Env)"),
]

# WHY (2026-08-31): `grep -B3 -A12 <pat> "vault/Daily Notes/_common_commands.md"` printed a
# live cloudflared tunnel token. BROAD matched twice; CRED matched nothing, because the file
# is an ordinary note -- so the guard exited at the CRED miss before BROAD was evaluated.
SECRET_SHAPES = [
    (r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----", "a PEM private key block"),
    (r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "a JWT"),
    (r"\beyJ[A-Za-z0-9_/+=-]{80,}", "a base64-encoded JSON credential (tunnel-token shape)"),
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "an AWS access key id"),
    (r"\bsk-ant-[A-Za-z0-9_-]{24,}", "an Anthropic API key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}", "a GitHub token"),
    (r"\bglpat-[A-Za-z0-9_-]{20,}", "a GitLab PAT"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{20,}", "a Slack token"),
    (r"\bAIza[0-9A-Za-z_-]{35}\b", "a Google API key"),
    (r"--token[=\s]+[A-Za-z0-9_/+=.-]{40,}", "a --token flag carrying a literal value"),
]

# Generic base64/entropy scoring was rejected: it fires on hashes, diffs and embedded images,
# and a guard that cries wolf gets switched off, which is strictly worse than this gap.
MAX_SCAN_BYTES = 2_000_000
MAX_SCAN_FILES = 8


def scan_file(path):
    """Return (label, line_no) for the first credential shape in `path`, else None."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_SCAN_BYTES:
            return None
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SCAN_BYTES)
        if b"\x00" in raw[:8192]:
            return None
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None
    for pat, label in SECRET_SHAPES:
        m = re.search(pat, text)
        if m:
            return label, text.count("\n", 0, m.start()) + 1
    return None


def candidate_paths(text):
    """Existing files named as bare (non-flag) arguments of `text`."""
    try:
        toks = shlex.split(text, comments=False, posix=True)
    except ValueError:
        return []
    out = []
    for t in toks:
        if not t or t.startswith("-"):
            continue
        p = os.path.expanduser(t)
        if os.path.isfile(p):
            out.append(p)
        if len(out) >= MAX_SCAN_FILES:
            break
    return out


def first_secret(paths):
    for p in paths:
        hit = scan_file(p)
        if hit:
            return "{}:{} holds {}".format(os.path.basename(p), hit[1], hit[0])
    return None


# A self-dumping command blocks on its own; otherwise fall through to path-plus-reader.
self_hit = next((label for pat, label in SELF_DUMPING if re.search(pat, scan)), None)

if tool == "Read":
    content_hit = first_secret([os.path.expanduser(tool_input.get("file_path") or "")])
    if not content_hit:
        sys.exit(0)
    reader, path_note = "Read tool (always a full dump)", content_hit
elif self_hit:
    reader, path_note = self_hit, "implicit -- the command opens it without naming it"
else:
    cred_hit = next((p for p in CRED if re.search(p, scan)), None)
    broad_hit = next((label for pat, label in BROAD if re.search(pat, scan)), None)
    if not broad_hit:
        sys.exit(0)                  # targeted read -> allow, cred path or not

    if cred_hit:
        reader, path_note = broad_hit, f"matched /{cred_hit}/"
    else:
        content_hit = first_secret(candidate_paths(scan))
        if not content_hit:
            sys.exit(0)
        reader, path_note = broad_hit, content_hit

# Record the block. Until 2026-08-19 neither PreToolUse guard logged anything, so a
# refusal existed only as stderr in one transcript. Deliberately does NOT write the
# command text: this guard exists precisely because such text can carry a credential,
# and moving that hazard from the transcript into a log file would not fix it. Format
# matches notify-classify.py's _log_decision so one awk reads both files.
_ident = cmd or tool_input.get("file_path") or "-"
try:
    import hashlib, time
    _log = os.path.join(os.environ.get("NEXUS_TMUX_DIR") or
                        os.path.expanduser("~/.tmux"), "gate-decisions.log")
    if os.path.exists(_log) and os.path.getsize(_log) > 5 * 1024 * 1024:
        os.replace(_log, _log + ".1")
    _head = re.sub(r"[^\w.:-]", "", os.path.basename((_ident.split() or ["-"])[0]))[:32] or "-"
    with open(_log, "a", encoding="utf-8") as _fh:
        _fh.write("{} block credential-guard {} {} {}\n".format(
            int(time.time()), os.environ.get("PANE") or "-", _head,
            hashlib.sha256(_ident.encode("utf-8", "replace")).hexdigest()[:12]))
except Exception:
    pass                                     # logging must never wedge the guard

sys.stderr.write(
    "BLOCKED by block-credential-dump.sh\n\n"
    f"  reader:  {reader}\n"
    f"  because: {path_note}\n\n"
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
    "If `because:` names a file and a line, the secret is IN THAT FILE, whatever the\n"
    "path looks like. To edit the file without reading the value:\n"
    "  grep -n 'anchor' FILE                # locate, no value printed\n"
    "  sed -i '' 'NNNs|.*|<redacted>|' FILE # replace line NNN in place\n\n"
    "If you genuinely must see raw content, do it in a shell the user controls\n"
    "(prefix the command with ! in the prompt) so it never enters the transcript.\n"
)
sys.exit(2)
PYEOF

printf '%s' "$payload" | python3 -c "$PY"
rc=$?
[ "$rc" -eq 2 ] && exit 2
exit 0
