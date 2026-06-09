# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Suggest Bash permission-allowlist rules from recent Claude Code transcripts.

Pipeline:
  1. Scan agents-nexus session transcripts (~/.claude/projects/*agents-nexus*).
  2. Extract every Bash command run in the last N hours.
  3. Normalize each command segment to a candidate rule (program + subcommand).
  4. Drop anything already covered by settings.json / settings.local.json.
  5. Drop destructive verbs (push, reset, rm, docker stop/rm, kill, ...).
  6. Rank what's left by frequency (count + distinct sessions).
  7. Write a dated candidates report (JSON) + a `latest.json`.
  8. Post a summary to Slack (incoming webhook).

Approval is intentionally MANUAL — nothing is added to settings here.
Run `/allowlist-review` inside Claude Code to review and apply candidates.

Usage:
  uv run scripts/allowlist-suggest.py                 # last 26h, post to Slack
  uv run scripts/allowlist-suggest.py --since-hours 24
  uv run scripts/allowlist-suggest.py --all --no-slack  # full history, local only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

PROJECTS_DIR = Path.home() / ".claude" / "projects"
PROJECT_GLOB = "*agents-nexus*"
REPO_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = Path.home() / ".claude" / "allowlist-candidates"

# Settings files searched for already-granted rules (project first, then user).
SETTINGS_FILES = [
    REPO_DIR / ".claude" / "settings.json",
    REPO_DIR / ".claude" / "settings.local.json",
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
]

# Programs whose 2nd token is a meaningful subcommand worth keying on.
SUBCMD_PROGS = {
    "git", "docker", "npm", "yarn", "pnpm", "kubectl", "cargo", "go", "uv",
    "pip", "pip3", "brew", "gh", "glab", "psql", "poetry", "make", "terraform",
    "aws", "python", "python3", "node", "task", "talosctl",
}

# Shell plumbing / pure read builtins / loop keywords — not worth allowlisting.
PLUMBING = {
    "echo", "cd", "ls", "cat", "grep", "head", "tail", "for", "while", "if",
    "sleep", "find", "sed", "awk", "wc", "sort", "uniq", "tr", "cut", "xargs",
    "do", "done", "then", "fi", "else", "elif", "printf", "pwd", "export",
    "true", "false", "test", "set", "read", "case", "esac", "function",
    "break", "continue", "return", "local", "eval", "source", "trap", "exit",
    "shift", "wait", "exec", "unset", "declare", "let", "time", "command", "env",
}

# Destructive single-token programs — never suggest ("all except destructive").
DESTRUCTIVE_PROGS = {
    "rm", "rmdir", "mv", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff",
    "kill", "pkill", "killall", "truncate", "shred", "chmod", "chown", "chgrp",
    "fdisk", "diskutil", "tee", "ln",
}

# Destructive program+subcommand pairs.
DESTRUCTIVE_PAIRS = {
    "git push", "git reset", "git clean", "git rebase", "git checkout",
    "git restore", "git stash", "git branch", "git merge", "git revert",
    "docker rm", "docker rmi", "docker stop", "docker kill", "docker prune",
    "docker system", "docker volume", "docker network", "docker compose down",
    "kubectl delete", "kubectl drain", "kubectl cordon", "kubectl apply",
    "npm publish", "terraform apply", "terraform destroy", "task docker:reset",
}

# Substrings that mark a segment as destructive regardless of leading program.
DESTRUCTIVE_RE = re.compile(
    r"""\brm\s+-[a-z]*[rf]      # rm -rf / rm -f
        | --force | --hard
        | \bdrop\s+(table|database|schema)
        | \bdelete\s+from
        | \btruncate\s+table
        | \bmkfs\b
        | >\s*/(?:dev/sd|etc|usr|bin|System)   # overwrite system paths
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A valid leading program token (filters heredoc/python noise like `from`, `#`).
PROG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._+-]*$")

# Shell operator tokens that delimit one command segment from the next.
OPERATORS = {"&&", "||", "|", ";", "&", "|&"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path) -> None:
    """Best-effort .env loader so the script also works run standalone."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def covered_prefixes() -> set[str]:
    """Normalized command prefixes already granted across all settings files."""
    prefixes: set[str] = set()
    bash_re = re.compile(r"^Bash\((.*)\)$")
    for f in SETTINGS_FILES:
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for rule in data.get("permissions", {}).get("allow", []):
            m = bash_re.match(rule.strip())
            if not m:
                continue
            inner = m.group(1).strip()
            inner = re.sub(r":\*$", "", inner)       # git status:*  -> git status
            inner = re.sub(r"\s*\*+$", "", inner)    # docker exec * -> docker exec
            inner = inner.strip()
            if inner:
                prefixes.add(inner)
    return prefixes


def is_covered(key: str, prefixes: set[str]) -> bool:
    first = key.split()[0]
    for p in prefixes:
        if not p:
            continue
        if key == p or key.startswith(p + " "):
            return True
        # A single-token broad grant (e.g. `git`, `curl`) covers its subcommands.
        if " " not in p and first == p:
            return True
    return False


def split_segments(cmd: str) -> list[list[str]]:
    """Tokenize a command (quote-aware) and split into per-segment token lists.

    Uses shlex with punctuation_chars so shell operators are isolated as their
    own tokens while quoted text (e.g. grep alternations `\\|`, SQL, JSON) stays
    intact. Falls back to the leading word only if the command won't tokenize
    (heredocs, unbalanced quotes, `$(...)`).
    """
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        first = cmd.strip().split()
        return [first[:1]] if first else []

    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in OPERATORS or set(tok) <= {"&", "|", ";"} and tok:
            if cur:
                segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def normalize_tokens(toks: list[str]) -> str | None:
    """Return a candidate key ('docker exec', 'curl', ...) or None to skip."""
    i = 0
    # strip leading VAR=val assignments and sudo
    while i < len(toks) and "=" in toks[i] and not toks[i].startswith("-"):
        i += 1
    if i < len(toks) and toks[i] == "sudo":
        i += 1
    if i >= len(toks):
        return None
    prog = toks[i].split("/")[-1]
    if not PROG_RE.match(prog) or prog in PLUMBING or prog in DESTRUCTIVE_PROGS:
        return None
    if prog in SUBCMD_PROGS and i + 1 < len(toks) and not toks[i + 1].startswith("-"):
        sub = toks[i + 1]
        if not re.match(r"^[A-Za-z][\w:./-]*$", sub):
            return prog
        return f"{prog} {sub}"
    return prog


def parse_ts(obj: dict) -> datetime | None:
    ts = obj.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_bash_commands(cutoff: datetime | None):
    """Yield (command, session_id) for Bash tool_use entries after cutoff."""
    if not PROJECTS_DIR.exists():
        return
    cutoff_epoch = cutoff.timestamp() if cutoff else 0
    for proj in PROJECTS_DIR.glob(PROJECT_GLOB):
        for jf in proj.rglob("*.jsonl"):
            # Coarse file-level skip: file untouched in window can't hold new cmds.
            if cutoff and jf.stat().st_mtime < cutoff_epoch:
                continue
            session = jf.stem
            try:
                fh = jf.open()
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if '"Bash"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if cutoff:
                        ts = parse_ts(obj)
                        if ts and ts < cutoff:
                            continue
                    for cmd in _bash_inputs(obj):
                        yield cmd, session


def _bash_inputs(obj):
    """Walk an arbitrary transcript object, yielding Bash .input.command strings."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get("name") == "Bash":
                cmd = (cur.get("input") or {}).get("command")
                if isinstance(cmd, str) and cmd.strip():
                    yield cmd
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since-hours", type=float, default=26.0,
                    help="Only consider commands newer than this (default 26).")
    ap.add_argument("--all", action="store_true",
                    help="Ignore the time window — scan full transcript history.")
    ap.add_argument("--min-count", type=int, default=3,
                    help="Min occurrences to suggest a rule (default 3).")
    ap.add_argument("--min-sessions", type=int, default=2,
                    help="Min distinct sessions a rule must appear in (default 2).")
    ap.add_argument("--top", type=int, default=15,
                    help="Max candidates to report (default 15).")
    ap.add_argument("--no-slack", action="store_true", help="Skip Slack post.")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    load_dotenv(REPO_DIR / ".env.local")
    load_dotenv(REPO_DIR / ".env")

    cutoff = None if args.all else datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    prefixes = covered_prefixes()

    counts: dict[str, int] = defaultdict(int)
    sessions: dict[str, set[str]] = defaultdict(set)
    samples: dict[str, list[str]] = defaultdict(list)
    total_cmds = 0

    for cmd, session in iter_bash_commands(cutoff):
        total_cmds += 1
        seen_in_cmd: set[str] = set()
        if DESTRUCTIVE_RE.search(cmd):
            continue
        for toks in split_segments(cmd):
            key = normalize_tokens(toks)
            if not key or key in DESTRUCTIVE_PAIRS:
                continue
            if is_covered(key, prefixes):
                continue
            if key in seen_in_cmd:
                continue
            seen_in_cmd.add(key)
            counts[key] += 1
            sessions[key].add(session)
            if len(samples[key]) < 2:
                s = cmd.strip().replace("\n", " ")
                samples[key].append(s[:117] + "…" if len(s) > 118 else s)

    candidates = [
        {
            "rule": f"Bash({key}:*)",
            "key": key,
            "count": counts[key],
            "sessions": len(sessions[key]),
            "samples": samples[key],
        }
        for key in counts
        if counts[key] >= args.min_count and len(sessions[key]) >= args.min_sessions
    ]
    candidates.sort(key=lambda c: (-c["count"], -c["sessions"], c["key"]))
    candidates = candidates[: args.top]

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    report = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "date": today,
        "window_hours": None if args.all else args.since_hours,
        "commands_scanned": total_cmds,
        "thresholds": {"min_count": args.min_count, "min_sessions": args.min_sessions},
        "candidates": candidates,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    dated = args.out_dir / f"agents-nexus-{today}.json"
    latest = args.out_dir / "agents-nexus-latest.json"
    dated.write_text(json.dumps(report, indent=2))
    latest.write_text(json.dumps(report, indent=2))

    # ---- stdout summary (also lands in the launchd log) ----
    print(f"Scanned {total_cmds} Bash commands "
          f"({'all history' if args.all else f'last {args.since_hours:g}h'}).")
    if not candidates:
        print("No new allowlist candidates above threshold. Nothing to review.")
    else:
        print(f"{len(candidates)} candidate rule(s) — report: {dated}")
        for c in candidates:
            print(f"  {c['count']:4d}×  {c['sessions']}s  {c['rule']}")

    if not args.no_slack:
        post_to_slack(candidates, today, dated)

    return 0


def post_to_slack(candidates: list[dict], today: str, report_path: Path) -> None:
    webhook = os.environ.get("SLACK_ALLOWLIST_WEBHOOK") or os.environ.get("SLACK_OBS_TIDY_WEBHOOK")
    if not webhook:
        print("No SLACK_ALLOWLIST_WEBHOOK / SLACK_OBS_TIDY_WEBHOOK set — skipping Slack.")
        return
    if not candidates:
        # Stay quiet on empty days rather than nagging.
        return

    lines = [f":lock: *Allowlist candidates — agents-nexus*  ({today})",
             f"{len(candidates)} frequent Bash command(s) not yet allowlisted "
             f"(destructive verbs excluded):", ""]
    for c in candidates:
        lines.append(f"• `{c['rule']}` — {c['count']}× across {c['sessions']} session(s)")
    lines += ["", "Review & approve in Claude Code:  `/allowlist-review`"]
    payload = json.dumps({"text": "\n".join(lines)}).encode()

    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print("Posted candidate summary to Slack.")
    except Exception as e:  # noqa: BLE001 — never fail the nightly job on Slack
        print(f"Slack post failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
