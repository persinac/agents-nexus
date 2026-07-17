#!/usr/bin/env python3
"""Propose Claude Code permission-allowlist additions from recent transcripts.

A scheduled "port" of the interactive `/fewer-permission-prompts` skill, in
**propose-only** mode: it never edits settings.json on its own.

Pipeline:
  1. Deterministic extraction — walk ~/.claude/projects/**/*.jsonl modified in
     the last N days, tally Bash command prefixes and MCP tool calls, and drop
     anything already covered by ~/.claude/settings.json permissions.allow.
  2. LLM safety judgment — hand the short candidate list to headless `claude`
     (the skill's actual value-add) to decide which are safe read-only/idempotent
     rules and to phrase the settings rule strings.
  3. Propose + ping — write ~/.tmux/permission-suggestions.{json,md} and post a
     Slack summary. The user reviews, then applies with `--apply`.

Apply (separate, explicit, human-triggered):
  python3 permission-suggest.py --apply
    Merges the proposed rules in permission-suggestions.json into
    ~/.claude/settings.json permissions.allow (dedup, with a .bak backup).

Stdlib only — runs under launchd (Mac) / systemd (Linux) with system python3.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

LOOKBACK_DAYS = int(os.getenv("PERM_SUGGEST_LOOKBACK_DAYS", "7"))
MIN_COUNT = int(os.getenv("PERM_SUGGEST_MIN_COUNT", "3"))
MAX_CANDIDATES = 50
CLAUDE_TIMEOUT = int(os.getenv("PERM_SUGGEST_CLAUDE_TIMEOUT", "300"))

HOME = Path(os.environ.get("HOME", Path.home()))
PROJECTS_DIR = HOME / ".claude" / "projects"
SETTINGS_FILE = HOME / ".claude" / "settings.json"
OUT_DIR = Path(os.getenv("TMUX_HOME", HOME / ".tmux"))
OUT_JSON = OUT_DIR / "permission-suggestions.json"
OUT_MD = OUT_DIR / "permission-suggestions.md"

# Tools whose first *two* tokens form the meaningful prefix (e.g. `git status`).
SUBCOMMAND_TOOLS = {
    "git", "npm", "pnpm", "yarn", "docker", "kubectl", "gh", "glab", "task",
    "cargo", "go", "terraform", "tf", "brew", "systemctl", "aws", "gcloud",
    "uv", "pip", "poetry", "make", "pnpx", "npx",
}

# Shell control-flow / builtins that are not real binaries to allowlist.
SHELL_KEYWORDS = {
    "for", "while", "if", "then", "else", "elif", "fi", "do", "done", "case",
    "esac", "function", "select", "until", "time", "exec", "eval", "source",
    ".", "{", "(", "[[", "test",
}


def nexus_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load KEY=VALUE pairs from the repo-root .env (for the Slack webhook)."""
    env_file = nexus_dir() / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def existing_allow() -> set[str]:
    if not SETTINGS_FILE.exists():
        return set()
    try:
        cfg = json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    return set(cfg.get("permissions", {}).get("allow", []))


def bash_prefix(command: str) -> str | None:
    """Derive a coarse rule key from a raw Bash command string."""
    cmd = command.strip()
    if not cmd:
        return None
    # Ignore obvious compound/piped commands — too risky to generalize.
    first = cmd.split()
    if not first:
        return None
    binary = first[0]
    # Strip env-var assignments and leading paths.
    if "=" in binary or binary in {"sudo", "env"}:
        return None
    base = binary.rsplit("/", 1)[-1]
    if base in SHELL_KEYWORDS:
        return None
    if not re.match(r"^[a-zA-Z][\w.-]*$", base):
        return None
    if base in SUBCOMMAND_TOOLS and len(first) > 1:
        sub = first[1]
        if re.match(r"^[a-zA-Z][\w.:-]*$", sub):
            return f"{base} {sub}"
    return base


def iter_recent_transcripts():
    if not PROJECTS_DIR.is_dir():
        return
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime >= cutoff:
                yield path
        except OSError:
            continue


def extract_tool_uses(path: Path, bash: Counter, mcp: Counter,
                      examples: dict[str, str]) -> None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or '"tool_use"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {})
            if name == "Bash":
                cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                key = bash_prefix(cmd)
                if key:
                    rule = f"Bash({key}:*)"
                    bash[rule] += 1
                    examples.setdefault(rule, cmd.strip().splitlines()[0][:120])
            elif name.startswith("mcp__"):
                mcp[name] += 1
                examples.setdefault(name, name)


def collect_candidates() -> list[dict]:
    bash: Counter = Counter()
    mcp: Counter = Counter()
    examples: dict[str, str] = {}
    for path in iter_recent_transcripts():
        extract_tool_uses(path, bash, mcp, examples)

    allowed = existing_allow()
    merged = bash + mcp
    candidates = []
    for rule, count in merged.most_common():
        if count < MIN_COUNT or rule in allowed:
            continue
        candidates.append({"rule": rule, "count": count,
                           "example": examples.get(rule, rule)})
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def claude_bin() -> str:
    if os.getenv("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    from shutil import which
    return which("claude") or str(HOME / ".local/bin/claude")


def judge_safety(candidates: list[dict]) -> list[dict]:
    """Ask headless claude which candidates are safe to auto-allow."""
    listing = "\n".join(
        f"- {c['rule']}  (used {c['count']}x; e.g. `{c['example']}`)"
        for c in candidates
    )
    prompt = f"""You are auditing Claude Code permission rules for auto-allowlisting.

Below are frequently-used tool calls from recent sessions that are NOT yet in the
user's permission allowlist. Decide which are safe to auto-allow.

SAFE = read-only or clearly idempotent/non-destructive (status/list/read/search,
diff, log, describe, get). UNSAFE = anything that writes, deletes, pushes,
deploys, authenticates, installs, mutates remote state, or runs arbitrary code.
When in doubt, exclude it.

Candidates:
{listing}

Return ONLY a JSON object, no prose, of this exact shape:
{{"allow": [{{"rule": "<exact rule string from the list>", "reason": "<<=8 words>"}}]}}
Include only the SAFE ones. Keep the rule strings byte-for-byte from the list."""

    cmd = [claude_bin(), "--print", "--dangerously-skip-permissions"]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[perm-suggest] claude invocation failed: {e}", file=sys.stderr)
        return []

    out = proc.stdout.strip()
    if not out:
        print(f"[perm-suggest] claude returned empty output (rc={proc.returncode})",
              file=sys.stderr)
        return []

    # Tolerate code fences / surrounding prose.
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        print("[perm-suggest] no JSON object in claude output", file=sys.stderr)
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        print("[perm-suggest] could not parse claude JSON", file=sys.stderr)
        return []

    counts = {c["rule"]: c["count"] for c in candidates}
    valid_rules = set(counts)
    approved = []
    for item in data.get("allow", []):
        rule = item.get("rule", "")
        if rule in valid_rules:  # guard against hallucinated rules
            approved.append({"rule": rule, "reason": item.get("reason", ""),
                             "count": counts[rule]})
    return approved


def write_outputs(approved: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps([a["rule"] for a in approved], indent=2) + "\n")

    now = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime())
    lines = [
        f"# Permission suggestions — {now}",
        "",
        f"{len(approved)} safe rule(s) proposed from the last {LOOKBACK_DAYS} days "
        "of transcripts, not yet in your allowlist.",
        "",
        "Apply with: `python3 scripts/permission-suggest.py --apply` "
        "(or `task perm:suggest:apply`)",
        "",
    ]
    for a in sorted(approved, key=lambda x: -x["count"]):
        lines.append(f"- `{a['rule']}` — {a['reason']} _(seen {a['count']}x)_")
    lines.append("")
    OUT_MD.write_text("\n".join(lines))


def post_slack(approved: list[dict]) -> None:
    webhook = (os.getenv("SLACK_PERMISSIONS_WEBHOOK")
               or os.getenv("SLACK_OBS_TIDY_WEBHOOK"))
    if not webhook:
        return
    top = "\n".join(f"• `{a['rule']}` — {a['reason']}"
                    for a in sorted(approved, key=lambda x: -x["count"])[:10])
    extra = f"\n…and {len(approved) - 10} more" if len(approved) > 10 else ""
    text = (f"*Permission suggestions* — {len(approved)} safe rule(s) ready to "
            f"allowlist\n{top}{extra}\n\n"
            "Review `~/.tmux/permission-suggestions.md`, then "
            "`task perm:suggest:apply` to apply.")
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001 — best-effort ping
        print(f"[perm-suggest] slack post failed: {e}", file=sys.stderr)


def apply() -> int:
    if not OUT_JSON.exists():
        print("[perm-suggest] no proposals file — run without --apply first")
        return 1
    rules = json.loads(OUT_JSON.read_text())
    if not rules:
        print("[perm-suggest] no proposed rules to apply")
        return 0
    cfg = {}
    if SETTINGS_FILE.exists():
        cfg = json.loads(SETTINGS_FILE.read_text())
        SETTINGS_FILE.with_suffix(".json.bak").write_text(json.dumps(cfg, indent=2) + "\n")
    cfg.setdefault("permissions", {}).setdefault("allow", [])
    before = set(cfg["permissions"]["allow"])
    added = [r for r in rules if r not in before]
    cfg["permissions"]["allow"] = sorted(before | set(rules))
    SETTINGS_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"[perm-suggest] applied {len(added)} new rule(s) to {SETTINGS_FILE}")
    for r in added:
        print(f"  + {r}")
    return 0


def main() -> int:
    load_env()
    if "--apply" in sys.argv[1:]:
        return apply()

    candidates = collect_candidates()
    if not candidates:
        print("[perm-suggest] no new frequent commands to propose")
        return 0
    approved = judge_safety(candidates)
    if not approved:
        print("[perm-suggest] no candidates judged safe")
        return 0
    write_outputs(approved)
    post_slack(approved)
    print(f"[perm-suggest] proposed {len(approved)} rule(s) -> {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
