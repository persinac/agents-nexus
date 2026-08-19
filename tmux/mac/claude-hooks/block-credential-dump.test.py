#!/usr/bin/env python3
"""Regression suite for block-credential-dump.sh.

Run from a file, not inline: the guard (correctly) refuses a Bash command that merely
CONTAINS a dangerous-looking string, so the cases cannot be passed as shell arguments.
Nothing here reads a credential -- these are command STRINGS handed to the hook's stdin.
"""
import json, subprocess, os

HOOK = os.path.expanduser("~/.claude/hooks/block-credential-dump.sh")

MUST_BLOCK = [
    "cat talos-config/cluster-secrets.yaml",
    "head -50 ~/.kube/config",
    "tail -20 talos-config/talosconfig",
    "cat .git/config",
    "base64 -d < talos-config/kubeconfig",
    "strings talos-config/talos-upgrade/talosconfig",
    "cat ~/.aws/credentials",
    "less .env",
    "cat certs/server.pem",
    "grep -n -A6 'cni:' talos-config/controlplane.yaml",   # the actual 2026-07-26 incident
    "grep -B3 token talos-config/worker.yaml",
    "xxd talos-config/kubeconfig",
    # --- self-dumping: no path named, no broad reader (2026-08-12 incident class) ---
    "git remote -v",                                       # the actual 2026-08-12 incident
    "git remote -v | head -2",
    "git remote --verbose",
    "git remote show origin",
    "git remote get-url origin",
    "git config --list",
    "git config -l",
    "git config --get remote.origin.url",
    "env",
    "env | grep AWS",
    "printenv",
    # command-position env must still be caught after the 2026-08-13 anchoring fix
    "sudo env",
    "cd /tmp; env",
    "ls /tmp && printenv > /tmp/dump",
    # (?m): env on a non-final line was missed while $ meant end-of-STRING only
    "ls /tmp\nenv\necho done",
    "doppler secrets",
    "doppler secrets download --no-file",
    "kubectl get secret my-secret -o yaml",
    "kubectl get secrets -n kube-system -o json",
    "helm get values my-release",
    "npm config list",
    # Intentionally conservative: even redirected-to-null, this stays blocked. Detecting
    # "output is discarded" reliably is not worth the regex, and a guard that fails closed
    # on a harmless edge case is behaving correctly.
    "git remote -v > /dev/null 2>&1",
    # --- heredoc bodies are stripped as data, but an INTERPRETER body still executes ---
    "bash <<'EOF'\ngit remote -v\nEOF",
    "sh <<'EOF'\ncat talos-config/kubeconfig\nEOF",
    # --- an unrestricted interpreter reads a file as completely as cat (2026-08-19) ---
    # A live NATS admin credential reached a durable transcript through the FIRST case
    # below. The CRED half matched (`\.env\b` hits ".env.example"); the BROAD half did
    # not, because no interpreter was listed there -- so both halves passed and the
    # command sailed through. `python3 -c` was ITEM 4 on this hook's own "Do this
    # instead" list at the time, so the guard recommended the tool that defeated it.
    "python3 -c \"print(open('ui-integration-tests/.env.example').read())\"",
    "python3 -c \"[print(l) for l in open('.env').read().splitlines()]\"",
    "python3 -c \"import pathlib; print(pathlib.Path('.env.example').read_text())\"",
    "node -e \"console.log(require('fs').readFileSync('.env','utf8'))\"",
    "ruby -e 'puts File.read(\".env\")'",
    "python3 -c \"print(open('talos-config/controlplane.yaml').read())\"",
]

MUST_ALLOW = [
    "grep -c cni talos-config/controlplane.yaml",
    "sha256sum talos-config/kubeconfig | cut -c1-16",
    "git ls-files talos-config/",
    "kubectl get pods -A",
    "cat notes/green-but-dead.md",
    "head -20 pulumi/__main__.py",
    "ls -la talos-config/",
    "git status --porcelain",
    "grep -oE 'name: none' talos-config/controlplane.yaml",
    "wc -l talos-config/kubeconfig",
    # --- targeted forms of the self-dumping commands must NOT be swept up ---
    "printenv PATH",
    "printenv HOME | wc -c",
    "git config --get user.email",
    "git config user.name",
    "git remote add origin https://github.com/flippin-balls/x.git",
    "git remote rename origin upstream",
    # the trello-read skill captures into a var rather than printing -- must keep working
    "export TRELLO_API_KEY=$(doppler secrets get TRELLO_API_KEY --project infrastructure --config prd --plain)",
    "doppler secrets get PINBALL_DB_HOST --plain",
    "doppler run -- uv run fbf device status",
    "kubectl get secret my-secret -o name",
    "kubectl get pods -o yaml",
    # --- a heredoc body is DATA: documenting a blocked command must not trip the guard.
    # The first case is verbatim the commit this hook refused to let us make (2026-08-12);
    # the ".sh" in the staged path is what made the naive INTERP regex misfire.
    "git add hooks/block-credential-dump.sh && git commit -F - <<'EOF'\nfix: git remote -v printed a PAT\nEOF",
    "cat > notes.md <<'EOF'\nNever run cat talos-config/kubeconfig.\nEOF",
    # --- 2026-08-13: a PATH whose last token ends in "env" is not a bare `env` ---
    # \b matches after any non-word char, so these all tripped the self-dumping rule.
    "ls /tmp/my-env",
    "ls /etc/env",
    "mkdir -p build/my-env",
    "cd ~/projects/scratch-env",
    "python3 -m venv .venv",
    "du -sh /var/lib/my.env",
    # --- the interpreter rule needs BOTH halves: an interpreter AND a read primitive ---
    # An interpreter that reads nothing, or reads a non-credential path, is ordinary work.
    # Over-blocking every `python3 -c` near the word .env would make the guard unusable.
    "python3 -c \"print(1 + 1)\"",
    "python3 -c \"import os; print(os.path.exists('.env'))\"",   # asserts, never reads
    "python3 -c \"print(open('README.md').read())\"",            # read, not a cred path
    "node -e \"console.log(process.argv)\"",
    # --- PROSE that discusses the guarded constructs (2026-08-19) ---
    # The first cut of the interpreter rule used an unbounded [\s\S]* span and fired on
    # a plain status message that merely mentioned python and open()/read_text near a
    # .env -- it blocked the very message reporting the fix. Requiring the -c/-e
    # inline-code flag is what separates "a command that reads a file" from "a sentence
    # about commands that read files".
    "echo 'the guard now treats python and node readFileSync on .env as broad readers'",
    # `\bhead\s` matched the --head FLAG of gh pr create; with any .env in the body the
    # guard refused an ordinary PR. `-` is a non-word char, so \b matches inside a flag.
    # Same defect class as \brm\b firing on `docker run --rm`.
    "gh pr create --head fix/x --title t --body 'mentions .env handling'",
    "gh pr view --head feature/dotenv-loader",
    "curl -sI --head https://example.com/.env",
    "git commit -F - <<'EOF'\ndocs: note that an interpreter open() on .env is guarded now\nEOF",
    # --- the replacement advice must itself be allowed, or the guard contradicts itself ---
    "grep -oE '^[A-Z_]+=' .env.example",
    "grep -c NATS_ADMIN_PASSWORD .env.example",
    "sha256sum .env.example | cut -c1-16",
]

# Verdict alone is not enough: a block attributed to the wrong rule sends the reader to
# the wrong fix. Each case asserts what the hook's stderr must (and must not) say.
# (command, substring required, substring forbidden or None)
MUST_BLOCK_BECAUSE = [
    # 2026-08-13 mis-attribution: `cat` on a .env path is a CRED-path-plus-broad-reader
    # violation, but the over-broad bare-env rule claimed it first and named the wrong cause.
    ("cat /tmp/nope.env", "reader:", "bare env"),
    ("head -5 config/prod.env", "matched", "bare env"),
    # the bare-env rule itself must still fire, and still say so
    ("env | grep AWS", "bare env", None),
    ("printenv", "bare env", None),
    # unchanged: the 2026-08-12 PAT incident keeps its own attribution
    ("git remote -v", "git remote", None),
]

def run_full(cmd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    p = subprocess.run([HOOK], input=payload, capture_output=True, text=True)
    return p.returncode, p.stderr


def run(cmd):
    return run_full(cmd)[0]

fails = 0
print("  MUST BLOCK:")
for c in MUST_BLOCK:
    rc = run(c)
    ok = rc == 2
    fails += not ok
    print(f"    {'ok  ' if ok else 'MISS'}  {c[:64]}")

print("  MUST ALLOW:")
for c in MUST_ALLOW:
    rc = run(c)
    ok = rc == 0
    fails += not ok
    print(f"    {'ok  ' if ok else 'FP  '}  {c[:64]}")

print("  MUST BLOCK, FOR THE RIGHT REASON:")
for c, want, forbid in MUST_BLOCK_BECAUSE:
    rc, err = run_full(c)
    ok = rc == 2 and want in err and (forbid is None or forbid not in err)
    fails += not ok
    why = "" if ok else (
        f"  <- rc={rc}"
        + ("" if want in err else f" missing {want!r}")
        + ("" if forbid is None or forbid not in err else f" wrongly blamed {forbid!r}")
    )
    print(f"    {'ok  ' if ok else 'MISS'}  {c[:44]:<44} [{want}]{why}")

# non-Bash tools must pass straight through
rc = run.__self__ if False else subprocess.run(
    [HOOK],
    input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "talos-config/kubeconfig"}}),
    capture_output=True, text=True).returncode
print(f"  passthrough (non-Bash tool): {'ok' if rc == 0 else 'FAIL'}")
fails += rc != 0

print(f"\n  RESULT: {'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}"
      f"  ({len(MUST_BLOCK)} block + {len(MUST_ALLOW)} allow"
      f" + {len(MUST_BLOCK_BECAUSE)} attribution + 1 passthrough)")
raise SystemExit(1 if fails else 0)
