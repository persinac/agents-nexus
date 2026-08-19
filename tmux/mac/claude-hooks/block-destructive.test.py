#!/usr/bin/env python3
"""Regression suite for block-destructive.sh.

Run from a file, not inline: the guard (correctly) refuses a Bash command that merely
CONTAINS a destructive-looking string, so these cases cannot be passed as shell
arguments. That is not a quirk to route around -- it bit twice while this hook was
being written, and block-credential-dump.test.py's docstring records the same finding.

Nothing here deletes anything. These are command STRINGS handed to the hook's stdin.

WHY THIS HOOK EXISTS: notify-classify.py auto-approves 94.7% of real Bash traffic on
Alex's bar ("as long as we're not deleting production data from a db, or deleting
something from k8s, most of it is fine"). Its _DESTRUCTIVE denylist holds that line --
but it only runs when a PERMISSION PROMPT is raised, and an unattended overnight agent
spawned with --dangerously-skip-permissions raises none. This hook re-applies the same
pattern at PreToolUse, where it fires in every session regardless of permission mode.

The pattern is IMPORTED from notify-classify.py, never copied, so these tests also
guard against the two files drifting apart.
"""
import json
import os
import subprocess
import sys

HOOK = os.path.expanduser("~/.claude/hooks/block-destructive.sh")

# Must be refused (exit 2). A miss here is a destructive command running unattended.
MUST_BLOCK = [
    # production data
    'psql -c "DELETE FROM users WHERE id > 0"',
    'psql $DATABASE_URL -c "DROP TABLE orders"',
    'psql -c "TRUNCATE TABLE events"',
    'dropdb staging',
    'redis-cli FLUSHALL',
    'mongosh --eval "db.events.deleteMany({})"',
    'alembic downgrade -1',
    # kubernetes / infra
    'kubectl delete pod my-pod',
    'kubectl -n prod delete deployment api',
    'kubectl drain node-1 --ignore-daemonsets',
    'kubectl apply --prune -f .',
    'helm uninstall my-release',
    'terraform destroy -auto-approve',
    'pulumi destroy --yes',
    'aws ec2 terminate-instances --instance-ids i-123',
    'gcloud compute instances delete vm-1',
    'docker system prune -af',
    # deletion not spelled `rm`
    'python3 -c "import shutil; shutil.rmtree(\'/data\')"',
    'shred -u secrets.txt',
    # remote branch deletion
    'git push origin --delete feature/x',
    # buried in a compound command -- the denylist matches the whole string
    'cd /tmp && echo ok && kubectl delete ns staging',
]

# Must pass through (exit 0). A failure here is the guard interrupting ordinary work,
# which is the whole thing this project is trying to stop doing.
MUST_ALLOW = [
    'grep -r TODO . | head -20',
    'git add -A && git commit -m "wip"',
    'kubectl get pods -o yaml',
    'kubectl logs deploy/api --tail=100',
    'kubectl apply -f manifests/deployment.yaml',   # apply is not delete
    'kubectl exec -it pod -- ls /app',              # exec is not delete
    'docker run --rm alpine echo hi',               # --rm removes a finished container
    'docker compose up -d',
    'killall node',
    'npm install --force',
    'terraform plan',
    'psql -c "SELECT count(*) FROM users"',
    'psql -c "INSERT INTO events (name) VALUES (\'x\')"',
    'python3 scripts/analyze.py',
    'echo "result" > /tmp/out.txt',
]


def run(cmd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    p = subprocess.run([HOOK], input=payload, capture_output=True, text=True)
    return p.returncode, p.stderr


def main():
    fails = []
    for c in MUST_BLOCK:
        rc, err = run(c)
        if rc != 2:
            fails.append((f"expected BLOCK (2), got {rc}", c))
        elif "NOT guarding" in err:
            fails.append(("hook failed OPEN -- classifier import broken", c))
    for c in MUST_ALLOW:
        rc, err = run(c)
        if rc != 0:
            fails.append((f"expected ALLOW (0), got {rc}", c))

    # A non-Bash tool must never be touched.
    p = subprocess.run([HOOK], text=True, capture_output=True,
                       input=json.dumps({"tool_name": "Read",
                                         "tool_input": {"file_path": "/etc/hosts"}}))
    if p.returncode != 0:
        fails.append((f"non-Bash tool should pass through, got {p.returncode}", "Read"))

    total = len(MUST_BLOCK) + len(MUST_ALLOW) + 1
    if fails:
        print(f"FAIL - {len(fails)} of {total}")
        for why, c in fails:
            print(f"  {why}: {c!r}")
        return 1
    print(f"PASS - {total}/{total} "
          f"({len(MUST_BLOCK)} blocked, {len(MUST_ALLOW)} allowed, 1 pass-through)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
