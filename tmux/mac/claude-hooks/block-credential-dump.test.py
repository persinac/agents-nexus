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
]

def run(cmd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    p = subprocess.run([HOOK], input=payload, capture_output=True, text=True)
    return p.returncode

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

# non-Bash tools must pass straight through
rc = run.__self__ if False else subprocess.run(
    [HOOK],
    input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "talos-config/kubeconfig"}}),
    capture_output=True, text=True).returncode
print(f"  passthrough (non-Bash tool): {'ok' if rc == 0 else 'FAIL'}")
fails += rc != 0

print(f"\n  RESULT: {'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}"
      f"  ({len(MUST_BLOCK)} block + {len(MUST_ALLOW)} allow + 1 passthrough)")
raise SystemExit(1 if fails else 0)
