#!/usr/bin/env python3
"""Alerts (Slack, cooldown-gated) when the notify-classify gate stops absorbing prompts. Exit 1 = anomalies."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
TMUX = HOME / ".tmux"
APPROVE_LOG = TMUX / "auto-approve.log"
ASKED_LOG = TMUX / "notify-asked.log"
REPEAT_LOG = TMUX / "notify-repeat.log"
DEBUG_LOG = TMUX / "notification-debug.log"
CLASSIFY_PY = TMUX / ".classify-venv" / "bin" / "python"
HERDR_PLUGINS = HOME / ".config" / "herdr" / "plugins.json"
PRESENCE_LOG = (HOME / ".local/state/herdr/plugins/nexus.presence/presence.log")

STATE = TMUX / "notify-pipeline-state.json"
HEALTH_LOG = TMUX / "notify-pipeline-health.log"

LOG_CAPS = {
    DEBUG_LOG: 5 * 1024 * 1024,
    ASKED_LOG: 2 * 1024 * 1024,
    PRESENCE_LOG: 1 * 1024 * 1024,
}

MISSED_PY = Path(__file__).resolve().parent / "notify-missed-check.py"

MIN_SAMPLE = 20
DEGRADED_PCT = 50.0
SPIKE_FACTOR = 4.0
SPIKE_MIN_ASKS = 10
COOLDOWN_SECS = 6 * 3600
DEBUG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


def epochs(path: Path, field: int = 0, want: str | None = None) -> list[int]:
    out: list[int] = []
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                f = line.split()
                if len(f) <= field or not f[0].isdigit():
                    continue
                if want and not (len(f) > 1 and f[1].startswith(want)):
                    continue
                out.append(int(f[0]))
    except FileNotFoundError:
        return []
    return out


def debug_prompt_epochs() -> list[int]:
    out: list[int] = []
    try:
        with DEBUG_LOG.open(errors="replace") as fh:
            for line in fh:
                if "type=permission_prompt" not in line:
                    continue
                m = DEBUG_TS.match(line)
                if not m:
                    continue
                try:
                    out.append(int(time.mktime(time.strptime(
                        m.group(1), "%Y-%m-%d %H:%M:%S"))))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    return out


def presence_enabled() -> bool | None:
    try:
        data = json.loads(HERDR_PLUGINS.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for p in data:
        if p.get("plugin_id") == "nexus.presence":
            return bool(p.get("enabled"))
    return None


def missed_check(window_h: float) -> dict:
    """notify-missed-check.py's JSON, or {} if it cannot run. Never raises: a broken
    sub-check must not take the health check down with it."""
    if not MISSED_PY.exists():
        return {}
    try:
        out = subprocess.run([sys.executable, str(MISSED_PY), "--hours", str(window_h),
                              "--json"], capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout or "{}")
    except Exception:
        return {}


def collect(window_h: float) -> tuple[list[dict], dict]:
    now = int(time.time())
    lo = now - int(window_h * 3600)

    approved = [t for t in epochs(APPROVE_LOG, 2, "auto-approve") if t >= lo]
    asked = [t for t in epochs(ASKED_LOG, 1) if t >= lo]
    repeats = [t for t in epochs(REPEAT_LOG, 2, "repeat") if t >= lo]
    prompts = [t for t in debug_prompt_epochs() if t >= lo]

    decided = len(approved) + len(asked)
    handled = (len(approved) / decided * 100) if decided else None

    all_asked = epochs(ASKED_LOG, 1)
    base_lo = now - 7 * 86400
    base = [t for t in all_asked if base_lo <= t < lo]
    base_rate = len(base) / max(1.0, (lo - base_lo) / 3600)
    win_rate = len(asked) / max(window_h, 0.01)

    misresolved = {k: v for k, v in (missed_check(window_h).get("by_outcome") or {}).items()
                   if k != "ok" and v}

    facts = {
        "window_hours": window_h,
        "misresolved": misresolved or None,
        "auto_approved": len(approved),
        "asked_human": len(asked),
        "repeat_suppressed": len(repeats),
        "prompts_seen": len(prompts),
        "handled_pct": round(handled, 1) if handled is not None else None,
        "ask_rate_per_h": round(win_rate, 2),
        "baseline_ask_rate_per_h": round(base_rate, 2),
        "presence_enabled": presence_enabled(),
    }

    issues: list[dict] = []

    if decided >= MIN_SAMPLE and handled is not None and handled < DEGRADED_PCT:
        issues.append({
            "id": "classifier-degraded",
            "msg": (f"classifier handled only {handled:.0f}% of {decided} prompts "
                    f"in {window_h:g}h (expected >{DEGRADED_PCT:.0f}%)"),
            "hint": "check notify-classify.py's LLM tier — a dead "
                    "ANTHROPIC_API_BASE makes it fall through to asking",
        })

    if len(prompts) >= MIN_SAMPLE and not approved:
        issues.append({
            "id": "classifier-silent",
            "msg": (f"{len(prompts)} permission prompts in {window_h:g}h but zero "
                    f"auto-approvals"),
            "hint": "hook-notification.sh may not be reaching the classifier",
        })

    if not CLASSIFY_PY.exists():
        issues.append({
            "id": "classifier-venv-missing",
            "msg": f"classifier interpreter missing: {CLASSIFY_PY}",
            "hint": "every prompt reaches you until this is restored",
        })

    for path, cap in LOG_CAPS.items():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > cap * 1.2:
            issues.append({
                "id": f"log-unrotated:{path.name}",
                "msg": f"{path.name} is {size/1048576:.1f}MB, cap {cap/1048576:.0f}MB",
                "hint": "the writer's rotation check is not firing",
            })

    if facts["presence_enabled"]:
        issues.append({
            "id": "presence-reenabled",
            "msg": "nexus.presence is enabled again — it duplicates "
                   "hook-notification.sh's desktop toast",
            "hint": "herdr plugin disable nexus.presence (see PR #86: "
                    "`herdr plugin link` re-enables)",
        })

    if misresolved:
        issues.append({
            "id": "gate-misresolved",
            "msg": (f"{sum(misresolved.values())} prompt(s) in {window_h:g}h were not owed: "
                    + ", ".join(f"{v} {k}" for k, v in sorted(misresolved.items()))),
            "hint": "run scripts/notify-missed-check.py; wrong-tool means the classifier "
                    "judged a different call than the one blocked",
        })

    if (len(asked) >= SPIKE_MIN_ASKS and base_rate > 0
            and win_rate > base_rate * SPIKE_FACTOR):
        issues.append({
            "id": "ask-spike",
            "msg": (f"{win_rate:.1f} asks/h vs {base_rate:.1f}/h 7-day baseline "
                    f"({win_rate/base_rate:.1f}x)"),
            "hint": "something regressed toward asking, or a new tool class "
                    "is unclassified",
        })

    return issues, facts


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def post_slack(issues: list[dict], facts: dict) -> bool:
    webhook = (os.getenv("SLACK_NOTIFY_PIPELINE_WEBHOOK")
               or os.getenv("SLACK_OBS_TIDY_WEBHOOK"))
    if not webhook:
        return False
    body = "\n".join(f"• {i['msg']}\n    → {i['hint']}" for i in issues)
    text = (f"*Notification pipeline — {len(issues)} issue(s)*\n{body}\n\n"
            f"_last {facts['window_hours']:g}h: {facts['auto_approved']} "
            f"auto-approved, {facts['asked_human']} reached you_")
    req = urllib.request.Request(
        webhook, data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:  # noqa: BLE001 — best-effort ping
        print(f"[notify-check] slack post failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-hours", type=float, default=6.0)
    ap.add_argument("--cooldown-hours", type=float, default=COOLDOWN_SECS / 3600)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only: no Slack, no state or history write")
    ap.add_argument("--force", action="store_true", help="ignore the cooldown")
    args = ap.parse_args()

    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    issues, facts = collect(args.window_hours)
    now = int(time.time())
    state = load_state()
    seen = state.get("last_alerted", {})
    cooldown = args.cooldown_hours * 3600

    fresh = [i for i in issues
             if args.force or now - int(seen.get(i["id"], 0)) >= cooldown]

    if args.json:
        print(json.dumps({"facts": facts, "issues": issues,
                          "alerting_on": [i["id"] for i in fresh]}, indent=2))
    else:
        pct = facts["handled_pct"]
        print(f"window {facts['window_hours']:g}h — {facts['auto_approved']} "
              f"auto-approved, {facts['asked_human']} reached you, "
              f"{pct if pct is not None else 'n/a'}% handled")
        if not issues:
            print("healthy: no anomalies")
        for i in issues:
            tag = "ALERT" if i in fresh else "known"
            print(f"  [{tag}] {i['msg']}")
            print(f"          → {i['hint']}")

    if args.dry_run:
        return 1 if issues else 0

    if fresh and post_slack(fresh, facts):
        for i in fresh:
            seen[i["id"]] = now
        state["last_alerted"] = seen
        try:
            STATE.write_text(json.dumps(state, indent=2))
        except OSError:
            pass

    try:
        if HEALTH_LOG.exists() and HEALTH_LOG.stat().st_size > 1048576:
            HEALTH_LOG.replace(HEALTH_LOG.with_suffix(".log.1"))
        with HEALTH_LOG.open("a") as fh:
            fh.write(f"{now} {json.dumps({'facts': facts, 'issues': [i['id'] for i in issues]})}\n")
    except OSError:
        pass

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
