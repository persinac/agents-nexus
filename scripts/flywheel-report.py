#!/usr/bin/env python3
"""Flywheel report: agent spend, retries, pointed-at context and skill usage from transcript metadata."""
import argparse
import collections
import datetime as dt
import html
import json
import operator
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path.home()
CLAUDE = HOME / ".claude"
PROJECTS = CLAUDE / "projects"
PRICES = REPO / "scripts" / "routing-prices.json"
OUT_DIR = HOME / ".local" / "share" / "agents-nexus" / "flywheel"
DATE_SUFFIX = re.compile(r"-\d{8}$")
COMMAND_TAG = re.compile(r"<command-name>/([A-Za-z0-9:_-]+)</command-name>")
HIGH_TIER = ("opus", "fable")
DOWNGRADE_MIN_SESSIONS = 5
DOWNGRADE_MIN_HIGH_SHARE = 0.8
DOWNGRADE_MAX_MEDIAN_OUT = 400
RETRY_FLAG_RATE = 0.3
RETRY_FLAG_MIN_N = 3
USAGE_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
FINISHED_STATUSES = {"done", "partial", "abandoned", "gated", "failed", "escalated"}
HTML_PATH = OUT_DIR / "flywheel-report.html"
HTML_TITLE = "Flywheel report — daily"
DEPOSIT_TAGS = ("flywheel", "agent-cost", "conductor", "repo:agents-nexus")
CLAIMS = {
    "Downgrade candidate": "A seat pays high-tier rates for short output",
    "Inherited expensive model": "Reviewers run on the parent's expensive model",
    "Verify rejections re-run everything": "A verify rejection re-runs every subtask",
    "Retry-heavy profile": "Conductor profiles are retry-heavy",
    "Never-read context": "Pointed-at reference files go unread",
    "Unused skills": "Skills go unused",
    "Unused agents": "Agents go unused",
    "Hook blocks": "Agents keep tripping the same hooks",
    "Ledger missed": "A recorded change missed its prediction",
    "Ledger unreadable": "The change ledger could not be read",
}
LEDGER = REPO / "config" / "flywheel-ledger.yaml"
SERIES = OUT_DIR / "daily-metrics.json"
FREEZE_AFTER_DAYS = 25
HOOK_FINDING_MIN = 20
INTERRUPT = "[Request interrupted by user"
TS_RE = re.compile(r'"timestamp":"([^"]+)"')
HOOK_RE = re.compile(r"hooks/([A-Za-z0-9_-]+)\.(?:sh|py)")
CORRECTION = re.compile(
    r"^(?:(?:well|ok|okay|hmm|so|but|um|uh|ah)[,.\s]+)?"
    r"(?:no\b|nope\b|don'?t\b|do not\b|stop\b|wait\b|hold\b|actually\b|revert\b|undo\b|wrong\b"
    r"|that'?s (?:wrong|not)\b|this is wrong\b|not what\b|i said\b|i asked\b|i didn'?t ask\b"
    r"|i did not ask\b|why did you\b|why are you\b|you shouldn'?t\b|never\b)", re.I)
BUCKETS = (
    ("credentials", re.compile(r"\b(?:secrets?|credentials?|passwords?|api[ -]?keys?)\b|\.env\b", re.I)),
    ("comments", re.compile(r"\b(?:comments?|docstrings?)\b", re.I)),
    ("git", re.compile(r"\b(?:commit|push|merge|force[- ]push|rebase|amend|branch)\w*", re.I)),
    ("scope", re.compile(r"\b(?:didn'?t ask|did not ask|not what i asked|scope|overkill|too much)\b", re.I)),
    ("tests", re.compile(r"\b(?:tests?|testing|pytest|lint\w*)\b", re.I)),
    ("verbosity", re.compile(r"\b(?:shorter|concise|too long|verbose|brief|dense|wordy)\b", re.I)),
)
GROUPS = {"session": "session:", "agent": "agent:", "conductor": "conductor-"}
OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


def load_prices():
    data = json.loads(PRICES.read_text())
    return (data["models"], float(data.get("cache_read_mult", 0.10)),
            float(data.get("cache_write_mult", 1.25)), float(data.get("cache_write_1h_mult", 2.0)))


def resolve_price(model, models):
    return models.get(model) or models.get(DATE_SUFFIX.sub("", model))


def is_high_tier(model):
    return any(t in model for t in HIGH_TIER)


def project_label(cwd):
    if not cwd:
        return "?"
    if "/.tmux/conductor/" in cwd:
        rest = cwd.split("/.tmux/conductor/", 1)[1].strip("/")
        return "conductor-orchestrator" if "/" not in rest else "conductor-worker"
    if "/.worktrees/" in cwd:
        return cwd.split("/.worktrees/", 1)[1].split("/")[0].split("--")[0] + " (worktree)"
    return "~" if Path(cwd) == HOME else Path(cwd).name


def seat_for(path, cwd):
    if path.parent.name == "subagents":
        try:
            agent_type = json.loads(path.with_suffix(".meta.json").read_text()).get("agentType")
        except (OSError, ValueError):
            agent_type = None
        return f"agent:{agent_type or '?'}"
    label = project_label(cwd)
    return label if label.startswith("conductor-") else f"session:{label}"


def inventory():
    skills = {p.parent.name for p in (CLAUDE / "skills").glob("*/SKILL.md")}
    cmd_root = CLAUDE / "commands"
    commands = {str(p.relative_to(cmd_root).with_suffix("")).replace("/", ":")
                for p in cmd_root.rglob("*.md")} if cmd_root.is_dir() else set()
    agents = {p.stem for p in (CLAUDE / "agents").glob("*.md")}
    return skills, commands, agents


def pointed_at():
    files = set(CLAUDE.glob("references/*.md")) | set(CLAUDE.glob("skills/*/references/**/*.md"))
    return {os.path.realpath(p) for p in files}


def new_seat():
    return {"files": set(), "turns": 0, "out_per_turn": [], "effort": collections.Counter(),
            "tokens": collections.defaultdict(collections.Counter)}


def group_of(seat):
    return next((g for g, prefix in GROUPS.items() if seat.startswith(prefix)), "session")


def bucket_of(text):
    return next((name for name, rx in BUCKETS if rx.search(text)), "other")


def new_group():
    return {"turns": 0, "errors": 0, "interrupts": 0,
            "hooks": collections.Counter(), "corrections": collections.Counter()}


def tally_result(r, seat, day, line):
    g = r["daily"][day][group_of(seat)]
    hooks = set(HOOK_RE.findall(line)) if ("hook error" in line or "hook blocking error" in line) else set()
    for hook in hooks:
        g["hooks"][hook] += 1
        r["seat_signals"][seat]["hooks"] += 1
    if not hooks:
        n = line.count('"is_error":true')
        g["errors"] += n
        r["seat_signals"][seat]["errors"] += n


def scan(days, sample=False):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    cutoff_iso, cutoff_ts = cutoff.strftime("%Y-%m-%dT%H:%M:%S"), cutoff.timestamp()
    skill_root = os.path.realpath(CLAUDE / "skills") + os.sep
    watched = pointed_at()
    r = {"seats": collections.defaultdict(new_seat), "files": 0,
         "skills": collections.Counter(), "skill_via": collections.defaultdict(collections.Counter),
         "commands": collections.Counter(), "spawns": collections.Counter(),
         "inherit_expensive": collections.Counter(), "reads": collections.Counter(), "last": {},
         "daily": collections.defaultdict(lambda: collections.defaultdict(new_group)),
         "seat_signals": collections.defaultdict(collections.Counter),
         "samples": {"counted": [], "excluded": []}, "cutoff_day": cutoff.date().isoformat()}

    def touch(key, ts):
        if ts > r["last"].get(key, ""):
            r["last"][key] = ts

    for path in PROJECTS.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_ts:
                continue
        except OSError:
            continue
        r["files"] += 1
        msgs, seen_tools, cwd, seat = {}, set(), None, None
        prev_q = after_interrupt = seen_first = False
        with path.open(errors="replace") as fh:
            for line in fh:
                is_asst = '"type":"assistant"' in line
                is_user = not is_asst and '"type":"user"' in line
                if is_user and '"tool_use_id"' in line:
                    if seat and ('"is_error":true' in line or "hook error" in line
                                 or "hook blocking error" in line):
                        tsm = TS_RE.search(line)
                        if tsm and tsm.group(1) >= cutoff_iso:
                            tally_result(r, seat, tsm.group(1)[:10], line)
                    continue
                if not (is_asst or is_user or (cwd is None and '"cwd"' in line)):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cwd = cwd or obj.get("cwd")
                ts = obj.get("timestamp") or ""
                if ts < cutoff_iso:
                    continue
                seat = seat or seat_for(path, cwd)
                msg = obj.get("message") or {}
                if obj.get("type") == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        text = content
                        for name in COMMAND_TAG.findall(content):
                            r["commands"][name] += 1
                            touch(f"cmd:{name}", ts)
                    elif isinstance(content, list):
                        text = " ".join(b.get("text", "") for b in content
                                        if isinstance(b, dict) and b.get("type") == "text")
                    else:
                        text = ""
                    if group_of(seat) != "session":
                        continue
                    g = r["daily"][ts[:10]]["session"]
                    if INTERRUPT in text:
                        g["interrupts"] += 1
                        r["seat_signals"][seat]["interrupts"] += 1
                        after_interrupt = True
                        continue
                    typed = text.strip()
                    if (not typed or typed.startswith("<") or obj.get("isMeta")
                            or obj.get("isCompactSummary")):
                        continue
                    if not seen_first:
                        seen_first = True
                        continue
                    marker = bool(CORRECTION.match(typed))
                    counted = after_interrupt or (marker and not prev_q)
                    after_interrupt = False
                    if counted:
                        bucket = bucket_of(typed)
                        g["corrections"][bucket] += 1
                        r["seat_signals"][seat]["corrections"] += 1
                        if sample:
                            r["samples"]["counted"].append((bucket, typed[:200]))
                    elif sample and marker:
                        r["samples"]["excluded"].append(("answer", typed[:200]))
                    continue
                if obj.get("type") != "assistant" or msg.get("model") in (None, "<synthetic>"):
                    continue
                model = msg["model"]
                mid = msg.get("id")
                if mid:
                    u = msg.get("usage") or {}
                    cw1h = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
                    m = msgs.setdefault(mid, {"model": model, "effort": obj.get("effort"), "day": ts[:10],
                                              **dict.fromkeys(USAGE_FIELDS, 0)})
                    for k, v in (("input", u.get("input_tokens")), ("output", u.get("output_tokens")),
                                 ("cache_read", u.get("cache_read_input_tokens")),
                                 ("cache_write_1h", cw1h),
                                 ("cache_write_5m", (u.get("cache_creation_input_tokens") or 0) - cw1h)):
                        m[k] = max(m[k], v or 0)
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                        prev_q = block["text"].rstrip().rstrip("*_` \n").endswith("?")
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("id") in seen_tools:
                        continue
                    seen_tools.add(block.get("id"))
                    name, inp = block.get("name"), block.get("input") or {}
                    if name == "Skill" and inp.get("skill"):
                        r["skills"][inp["skill"]] += 1
                        r["skill_via"][inp["skill"]]["Skill tool"] += 1
                        touch(f"skill:{inp['skill']}", ts)
                    elif name in ("Agent", "Task"):
                        kind = inp.get("subagent_type") or "general-purpose"
                        r["spawns"][kind] += 1
                        touch(f"agent:{kind}", ts)
                        if "model" not in inp and is_high_tier(model):
                            r["inherit_expensive"][kind] += 1
                    elif name == "Read" and inp.get("file_path"):
                        rp = os.path.realpath(os.path.expanduser(inp["file_path"]))
                        if rp in watched:
                            r["reads"][rp] += 1
                            touch(f"read:{rp}", ts)
                        elif rp.startswith(skill_root) and rp.endswith("/SKILL.md"):
                            sk = rp[len(skill_root):].split(os.sep)[0]
                            r["skills"][sk] += 1
                            r["skill_via"][sk]["Read SKILL.md"] += 1
                            touch(f"skill:{sk}", ts)
        if msgs:
            s = r["seats"][seat]
            s["files"].add(str(path))
            for m in msgs.values():
                t = s["tokens"][m["model"]]
                for k in USAGE_FIELDS:
                    t[k] += m[k]
                s["turns"] += 1
                r["daily"][m["day"]][group_of(seat)]["turns"] += 1
                s["out_per_turn"].append(m["output"])
                if m["effort"]:
                    s["effort"][m["effort"]] += 1
    r["watched"] = watched
    return r


def seat_rows(seats, models, cr_mult, cw_mult, cw1h_mult):
    rows, unpriced = [], collections.Counter()
    cr_tokens = cr_usd = 0.0
    for name, s in seats.items():
        cost, high_out, all_out = 0.0, 0, 0
        for model, t in s["tokens"].items():
            all_out += t["output"]
            if is_high_tier(model):
                high_out += t["output"]
            price = resolve_price(model, models)
            if price is None:
                unpriced[model] += sum(t.values())
                continue
            inp = price["input"]
            cr_tokens += t["cache_read"]
            cr_usd += t["cache_read"] * inp * price.get("cache_read_mult", cr_mult) / 1e6
            cost += (t["input"] * inp + t["cache_write_5m"] * inp * cw_mult
                     + t["cache_write_1h"] * inp * cw1h_mult
                     + t["cache_read"] * inp * price.get("cache_read_mult", cr_mult)
                     + t["output"] * price["output"]) / 1e6
        rows.append({"seat": name, "sessions": len(s["files"]), "turns": s["turns"], "cost": cost,
                     "high_share": high_out / all_out if all_out else 0.0,
                     "median_out": statistics.median(s["out_per_turn"]) if s["out_per_turn"] else 0,
                     "effort": s["effort"].most_common(1)[0][0] if s["effort"] else "-",
                     "models": ", ".join(sorted(s["tokens"]))})
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return rows, unpriced, (cr_usd / cr_tokens if cr_tokens else 0.0)


def missions(days):
    sys.path.insert(0, str(REPO / "agent-runner"))
    try:
        from conductor_db import Db
    except ImportError as e:
        return None, f"unavailable: `{e.name}` not importable — run under agent-runner/.venv/bin/python"
    try:
        db = Db()
    except Exception as e:
        return None, f"unavailable: cannot connect ({type(e).__name__})"
    window = "now() - interval '1 day' * %s"
    findings = ("case when jsonb_typeof(e.payload->'findings') = 'array' "
                "then e.payload->'findings' else '[]'::jsonb end")
    q = {
        "profiles": f"""select profile, count(*),
                count(*) filter (where status = 'done'),
                count(*) filter (where status in ('blocked', 'error')),
                count(*) filter (where attempt >= 2),
                count(*) filter (where effort = 'xhigh')
            from agents.mission_subtasks where created_at > {window}
            group by profile order by 2 desc""",
        "missions": f"""select type, status, count(*), coalesce(sum(replan_count), 0)
            from agents.missions where created_at > {window}
            group by 1, 2 order by 3 desc""",
        "severity": f"""select coalesce(f->>'severity', '?'), count(*)
            from agents.mission_events e, jsonb_array_elements({findings}) f
            where e.event_type = 'replan' and e.ts > {window}
            group by 1 order by 2 desc""",
        "where": f"""select coalesce(nullif(f->>'where', ''), '?'), count(*)
            from agents.mission_events e, jsonb_array_elements({findings}) f
            where e.event_type = 'replan' and e.ts > {window}
            group by 1 order by 2 desc limit 10""",
        "cause": f"""select coalesce(sum(case when wf then 1 else 0 end), 0),
                coalesce(sum(case when wf then 0 else 1 end), 0)
            from (select exists (select 1 from jsonb_array_elements({findings}) f
                                 where f->>'what' like 'subtask ended %%') as wf
                  from agents.mission_events e
                  where e.event_type = 'replan' and e.ts > {window}) t""",
    }
    try:
        out = {k: db.conn.execute(sql, (days,)).fetchall() for k, sql in q.items()}
    except Exception as e:
        first = str(e).splitlines()[0] if str(e) else ""
        return None, f"unavailable: query failed ({type(e).__name__}: {first})"
    finally:
        db.close()
    return out, None


def utc_today():
    return dt.datetime.now(dt.timezone.utc).date()


def merge_series(daily, cutoff_day):
    store = json.loads(SERIES.read_text()) if SERIES.exists() else {}
    today = utc_today().isoformat()
    frozen = (utc_today() - dt.timedelta(days=FREEZE_AFTER_DAYS)).isoformat()
    for day, groups in daily.items():
        if day <= cutoff_day or day >= today or (day < frozen and day in store):
            continue
        store[day] = {g: {"turns": v["turns"], "errors": v["errors"], "interrupts": v["interrupts"],
                          "hooks": dict(v["hooks"]), "corrections": dict(v["corrections"])}
                      for g, v in groups.items()}
    SERIES.parent.mkdir(parents=True, exist_ok=True)
    SERIES.write_text(json.dumps(store, indent=1, sort_keys=True) + "\n")
    return store


def load_ledger():
    if not LEDGER.exists():
        return [], None
    try:
        import yaml
    except ImportError:
        return [], "PyYAML missing — run under agent-runner/.venv/bin/python"
    try:
        return (yaml.safe_load(LEDGER.read_text()) or {}).get("entries") or [], None
    except yaml.YAMLError as e:
        return [], f"{LEDGER.name}: {type(e).__name__}"


def ledger_rate(store, metric, days):
    kind = metric["kind"]
    groups = ["session"] if kind in ("corrections", "interrupts") else metric.get("seats") or list(GROUPS)
    num = den = covered = 0
    for day in days:
        if day not in store:
            continue
        covered += 1
        for g in groups:
            v = store[day].get(g)
            if not v:
                continue
            den += v["turns"]
            if kind == "hook":
                num += v["hooks"].get(metric["name"], 0)
            elif kind == "hooks":
                num += sum(v["hooks"].values())
            elif kind == "corrections":
                num += (v["corrections"].get(metric["bucket"], 0) if metric.get("bucket")
                        else sum(v["corrections"].values()))
            elif kind == "interrupts":
                num += v["interrupts"]
            elif kind == "tool_errors":
                num += v["errors"]
    return (1000 * num / den if den else None), covered


def metric_label(metric):
    kind, seats = metric["kind"], metric.get("seats")
    base = {"hook": f"`{metric.get('name')}` blocks", "hooks": "all hook blocks",
            "corrections": f"corrections ({metric.get('bucket') or 'all'})", "interrupts": "interrupts",
            "tool_errors": "tool errors"}.get(kind, kind)
    scope = "session" if kind in ("corrections", "interrupts") else "+".join(seats) if seats else "all seats"
    return f"{base} / 1k {scope} turns"


def evaluate_ledger(entries, store, rows, blended):
    today = utc_today()
    out = []
    for e in entries:
        date = e["date"] if isinstance(e["date"], dt.date) else dt.date.fromisoformat(str(e["date"]))
        before = [(date - dt.timedelta(days=i)).isoformat() for i in range(1, int(e.get("baseline_days", 30)) + 1)]
        after = [(date + dt.timedelta(days=i)).isoformat() for i in range(max((today - date).days, 0))]
        base, base_days = ledger_rate(store, e["metric"], before)
        now, now_days = ledger_rate(store, e["metric"], after)
        p, need = e.get("predict") or {}, int(e.get("reconcile_after_days", 14))
        target = p.get("value")
        if target is None and "baseline_mult" in p and base is not None:
            target = base * p["baseline_mult"]
        if e.get("status") != "shipped":
            verdict = "planned"
        elif now_days < need:
            verdict = f"pending ({now_days}/{need} days)"
        elif now is None or target is None:
            verdict = "no data"
        else:
            verdict = "held" if OPS[p.get("op", "<=")](now, target) else "missed"
        prefixes = tuple(GROUPS[s] for s in e.get("applies_to", []) if s in GROUPS) or tuple(GROUPS.values())
        turns = sum(x["turns"] for x in rows if x["seat"].startswith(prefixes))
        tokens = e.get("inherited_tokens")
        pred = (f"{p.get('op', '<=')} {p['value']}" if "value" in p
                else f"{p.get('op', '<=')} {p.get('baseline_mult')}× baseline")
        out.append({"id": e.get("id", "?"), "status": e.get("status", "?"), "change": e.get("change", ""),
                    "metric": metric_label(e["metric"]), "baseline": base, "baseline_days": base_days,
                    "since": now, "since_days": now_days, "prediction": pred, "target": target,
                    "verdict": verdict, "cost": tokens * turns * blended if tokens else None})
    return out


def fmt_rate(x):
    return "-" if x is None else f"{x:.2f}"


def per(n, d):
    return f"{1000 * n / d:.2f}" if d else "-"


def fmt_cost(x):
    return "-" if x is None else ("−" if x < 0 else "+") + f"${abs(x):,.0f}"


def short(path):
    for root, label in ((str(CLAUDE), "~/.claude"), (str(REPO), "agents-nexus"), (str(HOME), "~")):
        if path.startswith(root):
            return label + path[len(root):]
    return path


def analyze(days, r, rows, unpriced, mdata, merr, ledger, ledger_err, store):
    skills, commands, agents = inventory()
    last = r["last"]
    findings, retry_flags = [], []
    sig = {"turns": 0, "session_turns": 0, "errors": 0, "interrupts": 0,
           "hooks": collections.Counter(), "corrections": collections.Counter()}
    for groups in r["daily"].values():
        for g, v in groups.items():
            sig["turns"] += v["turns"]
            sig["session_turns"] += v["turns"] if g == "session" else 0
            sig["errors"] += v["errors"]
            sig["interrupts"] += v["interrupts"]
            sig["hooks"].update(v["hooks"])
            sig["corrections"].update(v["corrections"])
    if ledger_err:
        findings.append(("Ledger unreadable", ledger_err))
    for e in ledger:
        if e["verdict"] == "missed":
            findings.append(("Ledger missed", f"`{e['id']}` predicted {e['prediction']}; measured "
                             f"{fmt_rate(e['since'])} ({e['metric']})."))
    downgrade = [x for x in rows if not x["seat"].startswith("session:")
                 and x["sessions"] >= DOWNGRADE_MIN_SESSIONS
                 and x["high_share"] >= DOWNGRADE_MIN_HIGH_SHARE
                 and x["median_out"] <= DOWNGRADE_MAX_MEDIAN_OUT]
    for x in downgrade[:5]:
        findings.append(("Downgrade candidate", f"`{x['seat']}` — ${x['cost']:,.0f} list, "
                         f"{x['high_share']:.0%} high-tier output, median {x['median_out']:.0f} out-tokens/turn."))
    for kind, n in r["inherit_expensive"].most_common(3):
        findings.append(("Inherited expensive model",
                         f"`{kind}` spawned {n}× from an Opus/Fable parent with no `model`."))
    cause = mdata["cause"][0] if mdata and mdata["cause"] else None
    if cause and cause[1]:
        findings.append(("Verify rejections re-run everything",
                         f"{cause[1]} of {sum(cause)} replans had every subtask done; "
                         f"each one re-dispatched all subtasks (conductor.py run_and_verify)."))
    for hook, n in sig["hooks"].most_common(2):
        if n >= HOOK_FINDING_MIN:
            findings.append(("Hook blocks", f"`{hook}` blocked {n:,} tool calls "
                             f"({1000 * n / max(sig['turns'], 1):.1f} per 1k turns)."))
    if mdata:
        for prof, n, done, failed, retried, escalated in mdata["profiles"]:
            if n >= RETRY_FLAG_MIN_N and retried / n >= RETRY_FLAG_RATE:
                retry_flags.append(prof)
                findings.append(("Retry-heavy profile", f"`{prof}` — {retried}/{n} subtasks "
                                 f"re-dispatched, {escalated} escalated to xhigh."))
    refs = sorted(((p, r["reads"][p], last.get(f"read:{p}", "-")[:10]) for p in r["watched"]),
                  key=lambda x: (x[1], x[0]))
    dead_refs = sum(1 for _, n, _ in refs if n == 0)
    if dead_refs:
        findings.append(("Never-read context",
                         f"{dead_refs} of {len(refs)} pointed-at reference files had 0 reads."))
    skill_rows = []
    for s in sorted(skills, key=lambda s: (-(r["skills"][s] + r["commands"][s]), s)):
        via = dict(r["skill_via"][s])
        if r["commands"][s]:
            via["slash"] = r["commands"][s]
        seen = max(last.get(f"skill:{s}", ""), last.get(f"cmd:{s}", ""))[:10] or "-"
        skill_rows.append((s, r["skills"][s] + r["commands"][s],
                           ", ".join(f"{k} {v}" for k, v in via.items()) or "-", seen))
    agent_rows = [(a, r["spawns"][a], last.get(f"agent:{a}", "-")[:10])
                  for a in sorted(agents, key=lambda a: (-r["spawns"][a], a))]
    unused_skills = sum(1 for row in skill_rows if row[1] == 0)
    unused_agents = sum(1 for row in agent_rows if row[1] == 0)
    if unused_skills:
        findings.append(("Unused skills",
                         f"{unused_skills} of {len(skill_rows)} local skills had 0 invocations."))
    if unused_agents:
        findings.append(("Unused agents",
                         f"{unused_agents} of {len(agent_rows)} local agents were never spawned."))
    building = [(s, n, rp) for t, s, n, rp in mdata["missions"] if t == "building"] if mdata else []
    return {
        "days": days, "date": dt.date.today().isoformat(), "files": r["files"],
        "turns": sum(x["turns"] for x in rows), "total_cost": sum(x["cost"] for x in rows),
        "session_cost": sum(x["cost"] for x in rows if x["seat"].startswith("session:")),
        "rows": rows, "unpriced": unpriced, "findings": findings, "retry_flags": retry_flags,
        "inherit": r["inherit_expensive"], "spawns": r["spawns"],
        "swarm_inherit": sum(n for k, n in r["inherit_expensive"].items() if k.startswith("swarm-")),
        "swarm_spawns": sum(n for k, n in r["spawns"].items() if k.startswith("swarm-")),
        "swarm_cost": sum(x["cost"] for x in rows if x["seat"].startswith("agent:swarm-")),
        "mdata": mdata, "merr": merr, "cause": cause,
        "partial": sum(n for s, n, _ in building if s == "partial"),
        "finished": sum(n for s, n, _ in building if s in FINISHED_STATUSES),
        "partial_replans": sum(rp for s, _, rp in building if s == "partial"),
        "refs": refs, "dead_refs": dead_refs, "skill_rows": skill_rows,
        "unused_skills": unused_skills, "agent_rows": agent_rows,
        "command_rows": [(c, r["commands"][c]) for c in sorted(commands, key=lambda c: (-r["commands"][c], c))],
        "other": [(c, n) for c, n in r["commands"].most_common() if c not in skills and c not in commands],
        "sig": sig, "seat_turns": {x["seat"]: x["turns"] for x in rows},
        "signal_rows": sorted(r["seat_signals"].items(), key=lambda x: (-sum(x[1].values()), x[0]))[:12],
        "ledger": ledger, "ledger_err": ledger_err,
        "history": (len(store), min(store) if store else None, max(store) if store else None),
    }


def render_md(a):
    out = [f"# Flywheel report — {a['date']}\n",
           f"Window: last {a['days']} days · {a['files']:,} transcript files · {a['turns']:,} assistant turns · "
           f"${a['total_cost']:,.0f} at list price (notional for subscription traffic).\n",
           "Built from tool names, skill names, model ids, token counts and allowlisted "
           "file paths only. No message text is read.\n",
           "## Top findings\n"]
    out.extend(f"{i}. **{h}:** {d}" for i, (h, d) in enumerate(a["findings"], 1))
    if not a["findings"]:
        out.append("Nothing crossed a threshold.")

    out.append("\n## 1. Spend by seat\n")
    out.append("| Seat | Sessions | Turns | High-tier out share | Median out/turn | Top effort | List $ |")
    out.append("|---|---:|---:|---:|---:|---|---:|")
    for x in a["rows"][:25]:
        out.append(f"| `{x['seat']}` | {x['sessions']:,} | {x['turns']:,} | {x['high_share']:.0%} | "
                   f"{x['median_out']:.0f} | {x['effort']} | {x['cost']:,.0f} |")
    if a["inherit"]:
        out.append("\n**Spawns with no `model` from an Opus/Fable parent** (inherits the expensive model):\n")
        out.append("| subagent_type | Spawns | of total |")
        out.append("|---|---:|---:|")
        for kind, n in a["inherit"].most_common(15):
            out.append(f"| `{kind}` | {n} | {a['spawns'][kind]} |")
    if a["unpriced"]:
        out.append("\n**Unpriced models** (add to `scripts/routing-prices.json`; excluded from $):\n")
        out.extend(f"- `{m}` — {n:,} tokens" for m, n in a["unpriced"].most_common())

    out.append("\n## 2. Retries and verify failures (Conductor)\n")
    m = a["mdata"]
    if a["merr"]:
        out.append(f"Missions DB {a['merr']}.")
    else:
        out.append("| Profile | Subtasks | Done | Blocked/error | Re-dispatched | Escalated to xhigh |")
        out.append("|---|---:|---:|---:|---:|---:|")
        for prof, n, done, failed, retried, escalated in m["profiles"]:
            flag = " ⚑" if prof in a["retry_flags"] else ""
            out.append(f"| `{prof}`{flag} | {n} | {done} | {failed} | {retried} | {escalated} |")
        out.append("\n| Mission type | Status | Missions | Replans |")
        out.append("|---|---|---:|---:|")
        out.extend(f"| {t} | {s} | {n} | {rp} |" for t, s, n, rp in m["missions"])
        if a["cause"]:
            wf, vr = a["cause"]
            out.append(f"\nReplan cause: {wf} after a subtask failed, {vr} after reviewers rejected "
                       "a mission whose subtasks were all done (those re-run every subtask).")
        if m["severity"]:
            out.append("\nReplan findings by severity: " + ", ".join(f"{s} {n}" for s, n in m["severity"]))
        if m["where"]:
            out.append("\nMost-cited finding locations:\n")
            out.extend(f"- `{w}` — {n}" for w, n in m["where"])

    out.append("\n## 3. Pointed-at context\n")
    out.append("`Read` calls against reference files that CLAUDE.md and skills point at. "
               "Injected context (CLAUDE.md, conventions.md) is not measurable this way.\n")
    out.append("| File | Reads | Last read |")
    out.append("|---|---:|---|")
    out.extend(f"| `{short(p)}` | {n} | {seen} |" for p, n, seen in a["refs"])

    out.append("\n## 4. Surface usage\n")
    out.append("| Local skill | Invocations | Via | Last used |")
    out.append("|---|---:|---|---|")
    out.extend(f"| `{s}` | {n} | {via} | {seen} |" for s, n, via, seen in a["skill_rows"])
    out.append("\n| Local agent | Spawns | Last spawned |")
    out.append("|---|---:|---|")
    out.extend(f"| `{ag}` | {n} | {seen} |" for ag, n, seen in a["agent_rows"])
    if a["command_rows"]:
        out.append("\n| Local command | Invocations |")
        out.append("|---|---:|")
        out.extend(f"| `/{c}` | {n} |" for c, n in a["command_rows"])
    if a["other"]:
        out.append("\nOther slash commands (built-in or plugin): "
                   + ", ".join(f"`/{c}` {n}" for c, n in a["other"][:20]))

    s = a["sig"]
    out.append("\n## 5. Corrections (all usage)\n")
    out.append("Hook blocks and tool errors come from every seat. Interrupts and human corrections come from "
               "interactive sessions; corrections are counted by a local heuristic and no text leaves the script.\n")
    out.append("| Signal | Count | Per 1k turns |")
    out.append("|---|---:|---:|")
    out.append(f"| Hook blocks | {sum(s['hooks'].values()):,} | {per(sum(s['hooks'].values()), s['turns'])} |")
    out.append(f"| Tool errors (excluding hooks) | {s['errors']:,} | {per(s['errors'], s['turns'])} |")
    out.append(f"| User interrupts | {s['interrupts']:,} | {per(s['interrupts'], s['session_turns'])} (session) |")
    out.append(f"| Human corrections | {sum(s['corrections'].values()):,} | "
               f"{per(sum(s['corrections'].values()), s['session_turns'])} (session) |")
    if s["hooks"]:
        out.append("\n| Hook | Blocks | Per 1k turns |")
        out.append("|---|---:|---:|")
        out.extend(f"| `{h}` | {n:,} | {per(n, s['turns'])} |" for h, n in s["hooks"].most_common())
    if s["corrections"]:
        out.append("\n| Correction bucket | Count |")
        out.append("|---|---:|")
        out.extend(f"| {b} | {n} |" for b, n in s["corrections"].most_common())
    if a["signal_rows"]:
        out.append("\n| Seat | Turns | Hook blocks | Tool errors | Interrupts | Corrections |")
        out.append("|---|---:|---:|---:|---:|---:|")
        out.extend(f"| `{seat}` | {a['seat_turns'].get(seat, 0):,} | {c['hooks']} | {c['errors']} | "
                   f"{c['interrupts']} | {c['corrections']} |" for seat, c in a["signal_rows"])

    days_n, first, last_day = a["history"]
    out.append("\n## 6. Change ledger\n")
    out.append(f"Entries from `config/flywheel-ledger.yaml`. History: {days_n} UTC days stored"
               + (f" ({first} → {last_day})" if first else "") + ", frozen after "
               f"{FREEZE_AFTER_DAYS} days so baselines outlive transcript retention.\n")
    if a["ledger_err"]:
        out.append(f"Ledger unreadable: {a['ledger_err']}.")
    elif not a["ledger"]:
        out.append("No entries.")
    else:
        out.append("| Entry | Status | Metric | Baseline | Since change | Prediction | Verdict | ≈ Cost / 30 days |")
        out.append("|---|---|---|---:|---:|---|---|---:|")
        for e in a["ledger"]:
            out.append(f"| `{e['id']}` | {e['status']} | {e['metric']} | {fmt_rate(e['baseline'])} "
                       f"({e['baseline_days']}d) | {fmt_rate(e['since'])} ({e['since_days']}d) | "
                       f"{e['prediction']} | {e['verdict']} | {fmt_cost(e['cost'])} |")
        out.append("")
        out.extend(f"- `{e['id']}`: {e['change']}" for e in a["ledger"])
    return "\n".join(out) + "\n"


def esc(x):
    return html.escape(str(x), quote=False)


def inline(text):
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)


def code(x):
    return f"<code>{esc(x)}</code>"


def table(cols, rows, total=None):
    head = "".join(f'<th class="num">{esc(c)}</th>' if num else f"<th>{esc(c)}</th>" for c, num in cols)

    def tr(cells, cls=""):
        tds = "".join(f'<td class="num">{v}</td>' if num else f"<td>{v}</td>"
                      for v, (_, num) in zip(cells, cols))
        return f"<tr{cls}>{tds}</tr>"
    body = "".join(tr(r) for r in rows) + (tr(total, ' class="total"') if total else "")
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def tile(key, value, sub, bad=None):
    cls = "" if bad is None else (" warn" if bad else " ok")
    return (f'<div class="tile"><div class="tile-k">{esc(key)}</div>'
            f'<div class="tile-v{cls}">{esc(value)}</div><div class="tile-s">{esc(sub)}</div></div>')


def verdict_chip(verdict):
    cls = ("done" if verdict == "held" else "block" if verdict == "missed"
           else "moot" if verdict == "no data" else "watch")
    return f'<span class="chip {cls}">{esc(verdict)}</span>'


def section(sid, n, title, body):
    return (f'<section id="{sid}"><div class="sec-head"><div class="eyebrow">{n:02d}</div>'
            f"<h2>{esc(title)}</h2></div>{body}</section>")


def theme_head(title):
    out = subprocess.run([doc_vault_bin(), "theme"], capture_output=True, text=True, check=True).stdout
    head = out.split("<body>", 1)[0]
    if 'name="doc-theme"' not in head:
        raise RuntimeError("doc-vault theme returned no doc-theme head")
    return re.sub(r"<title>.*?</title>", f"<title>{esc(title)}</title>", head, count=1, flags=re.S)


def render_html(a):
    grouped = {}
    for kind, detail in a["findings"]:
        grouped.setdefault(kind, []).append(detail)
    kinds = list(grouped)
    claims = [CLAIMS.get(k, k) for k in kinds]
    headline = "Nothing crossed a threshold"
    if claims:
        headline = claims[0] + (", and " + claims[1][0].lower() + claims[1][1:] if len(claims) > 1 else "")

    def summary(kind):
        if kind == "Inherited expensive model" and a["swarm_spawns"]:
            return (f"{a['swarm_inherit']} of {a['swarm_spawns']} swarm reviewer spawns set no `model`, "
                    "so they ran on the parent's Opus or Fable.")
        return grouped[kind][0]
    dek = " ".join(inline(summary(k)) for k in kinds[:2]) or "No seat, profile or file crossed a threshold."
    m, cause = a["mdata"], a["cause"]
    total = a["total_cost"] or 1
    tiles = "".join([
        tile("list value", f"${a['total_cost'] / 1000:.1f}k", "notional for subscription traffic"),
        tile("interactive sessions", f"{a['session_cost'] / total:.0%}",
             f"${a['session_cost'] / 1000:.1f}k · a /model choice"),
        tile("swarm spawns on parent model", f"{a['swarm_inherit']} / {a['swarm_spawns']}",
             f"${a['swarm_cost'] / 1000:.2f}k of reviewer spend",
             bad=a["swarm_inherit"] > a["swarm_spawns"] / 2),
        tile("replans that re-ran everything", f"{cause[1]} / {sum(cause)}" if cause else "n/a",
             "all subtasks were already done", bad=bool(cause and cause[1]) if cause else None),
        tile("hook blocks", f"{sum(a['sig']['hooks'].values()):,}",
             f"{per(sum(a['sig']['hooks'].values()), a['sig']['turns'])} per 1k turns",
             bad=bool(a["sig"]["hooks"])),
        tile("pointed-at refs never read", f"{a['dead_refs']} / {len(a['refs'])}",
             f"{a['unused_skills']} of {len(a['skill_rows'])} skills also unused",
             bad=bool(a["dead_refs"])),
    ])
    cards = "".join(
        f'<div class="card{" is-key" if i < 2 else ""}"><div class="stripe"></div><div class="card-body">'
        f'<div class="card-top"><h3>{esc(CLAIMS.get(k, k))}</h3>'
        f'<span class="chip watch">{len(grouped[k])} open</span></div>'
        + (f"<p>{inline(grouped[k][0])}</p>" if len(grouped[k]) == 1 else
           '<ul class="tight">' + "".join(f"<li>{inline(d)}</li>" for d in grouped[k]) + "</ul>")
        + "</div></div>" for i, k in enumerate(kinds)
    ) or '<div class="callout calm"><div class="lab">note</div><p>Nothing crossed a threshold.</p></div>'

    seats = table([("Seat", False), ("Sessions", True), ("Turns", True), ("High-tier out", True),
                   ("Median out/turn", True), ("Top effort", False), ("List $", True)],
                  [[code(x["seat"]), f"{x['sessions']:,}", f"{x['turns']:,}", f"{x['high_share']:.0%}",
                    f"{x['median_out']:.0f}", esc(x["effort"]), f"{x['cost']:,.0f}"] for x in a["rows"][:12]])
    if a["inherit"]:
        seats += ("<p>Spawns that passed no <code>model</code> from an Opus or Fable parent, so they "
                  "ran on the parent's model:</p>"
                  + table([("subagent_type", False), ("No model", True), ("Spawns", True)],
                          [[code(k), n, a["spawns"][k]] for k, n in a["inherit"].most_common(10)]))
    if a["unpriced"]:
        seats += ('<div class="callout soft"><div class="lab">unpriced</div><p>'
                  + ", ".join(f"{code(k)} ({n:,} tokens)" for k, n in a["unpriced"].most_common())
                  + " — add to <code>scripts/routing-prices.json</code>. Excluded from $.</p></div>")

    if a["merr"]:
        conductor = f'<div class="callout soft"><div class="lab">missions db</div><p>{esc(a["merr"])}</p></div>'
    else:
        conductor = (
            "<p>Each round dispatches every subtask that is not done. A rejection with nothing left "
            "undone resets them all.</p>"
            '<div class="flow-wrap"><div class="flow"><div class="node">PLAN</div>'
            '<div class="hop"><div class="hop-l">dispatch</div><div class="hop-line"></div></div>'
            '<div class="node">WORKERS</div>'
            '<div class="hop"><div class="hop-l">all done</div><div class="hop-line"></div></div>'
            '<div class="node">REVIEW PANEL</div>'
            '<div class="hop trap"><div class="hop-l">reject → reset all</div><div class="hop-line"></div></div>'
            '<div class="node end">NEXT ROUND</div></div></div>'
            + table([("Profile", False), ("Subtasks", True), ("Done", True), ("Blocked/error", True),
                     ("Re-dispatched", True), ("At xhigh", True)],
                    [[code(p) + (" ⚑" if p in a["retry_flags"] else ""), n, d, fl, rt, x]
                     for p, n, d, fl, rt, x in m["profiles"]])
            + table([("Mission type", False), ("Status", False), ("Missions", True), ("Replans", True)],
                    [[esc(t), esc(s), n, rp] for t, s, n, rp in m["missions"]]))
        if cause:
            conductor += (f"<p>Replan cause: {cause[0]} after a subtask failed, {cause[1]} after a "
                          "rejection with every subtask done.</p>")
        if m["severity"]:
            conductor += ("<p>Replan findings by severity: "
                          + ", ".join(f"{n} {esc(s)}" for s, n in m["severity"]) + ".</p>")

    s = a["sig"]
    hooks_n, corr_n = sum(s["hooks"].values()), sum(s["corrections"].values())
    corrections = (
        "<p>Hook blocks and tool errors come from every seat. Interrupts and human corrections come from "
        "interactive sessions; corrections are counted by a local heuristic and no text leaves the script.</p>"
        + table([("Signal", False), ("Count", True), ("Per 1k turns", True)],
                [["Hook blocks", f"{hooks_n:,}", per(hooks_n, s["turns"])],
                 ["Tool errors (excluding hooks)", f"{s['errors']:,}", per(s["errors"], s["turns"])],
                 ["User interrupts", f"{s['interrupts']:,}", per(s["interrupts"], s["session_turns"]) + " (session)"],
                 ["Human corrections", f"{corr_n:,}", per(corr_n, s["session_turns"]) + " (session)"]])
        + (table([("Hook", False), ("Blocks", True), ("Per 1k turns", True)],
                 [[code(h), f"{n:,}", per(n, s["turns"])] for h, n in s["hooks"].most_common()])
           if s["hooks"] else "")
        + (table([("Correction bucket", False), ("Count", True)],
                 [[esc(b), n] for b, n in s["corrections"].most_common()]) if s["corrections"] else "")
        + (table([("Seat", False), ("Turns", True), ("Hook blocks", True), ("Tool errors", True),
                  ("Interrupts", True), ("Corrections", True)],
                 [[code(seat), f"{a['seat_turns'].get(seat, 0):,}", c["hooks"], c["errors"],
                   c["interrupts"], c["corrections"]] for seat, c in a["signal_rows"]])
           if a["signal_rows"] else ""))

    days_n, first, last_day = a["history"]
    ledger = (f"<p>Entries from <code>config/flywheel-ledger.yaml</code>. History: {days_n} UTC days stored"
              + (f" ({esc(first)} → {esc(last_day)})" if first else "")
              + f", frozen after {FREEZE_AFTER_DAYS} days so baselines outlive transcript retention.</p>")
    if a["ledger_err"]:
        ledger += f'<div class="callout"><div class="lab">ledger</div><p>{esc(a["ledger_err"])}</p></div>'
    elif not a["ledger"]:
        ledger += "<p>No entries.</p>"
    else:
        ledger += (table([("Entry", False), ("Metric", False), ("Baseline", True), ("Since change", True),
                          ("Prediction", False), ("Verdict", False), ("≈ Cost / 30 days", True)],
                         [[code(e["id"]), inline(e["metric"]), f"{fmt_rate(e['baseline'])} ({e['baseline_days']}d)",
                           f"{fmt_rate(e['since'])} ({e['since_days']}d)", esc(e["prediction"]),
                           verdict_chip(e["verdict"]), fmt_cost(e["cost"])] for e in a["ledger"]])
                   + '<ul class="tight">' + "".join(f"<li>{code(e['id'])} ({esc(e['status'])}): {esc(e['change'])}</li>"
                                                     for e in a["ledger"]) + "</ul>")

    limits = (
        '<div class="callout soft"><div class="lab">caution</div><p>Cost is measured; quality is not. '
        "Any model or effort change here needs an eval before it ships.</p></div>"
        '<div class="two"><div class="stack"><h3>What the report sees</h3><ul class="tight">'
        "<li>Every assistant turn's model, effort and token counts.</li>"
        "<li>Every Skill, Agent and Read tool call, by name and path.</li>"
        "<li>Hook blocks, tool errors, interrupts, and heuristic user corrections, as counts.</li>"
        "<li>Conductor subtasks, rounds and replan events.</li></ul></div>"
        '<div class="stack"><h3>What it does not</h3><ul class="tight">'
        "<li>Injected context: CLAUDE.md and conventions.md are never Read.</li>"
        "<li>Parser-gate prompts: they are answered outside the transcript.</li>"
        "<li>Which exact rule a correction was about: buckets are keyword-based.</li>"
        "<li>Whether a cheaper model would have passed review.</li>"
        "<li>Real cash: list price is notional on the subscription.</li></ul></div></div>")

    appendix = (
        '<details><summary>Per-file reads, every skill, agent and command, and method</summary>'
        '<div class="details-body"><h3>Pointed-at reference files</h3>'
        + table([("File", False), ("Reads", True), ("Last read", False)],
                [[code(short(p)), n, esc(seen)] for p, n, seen in a["refs"]])
        + "<h3>Local skills</h3>"
        + table([("Skill", False), ("Invocations", True), ("Via", False), ("Last used", False)],
                [[code(s), n, esc(via), esc(seen)] for s, n, via, seen in a["skill_rows"]])
        + "<h3>Local agents</h3>"
        + table([("Agent", False), ("Spawns", True), ("Last spawned", False)],
                [[code(ag), n, esc(seen)] for ag, n, seen in a["agent_rows"]])
        + ("<h3>Local commands</h3>" + table([("Command", False), ("Invocations", True)],
                                             [[code("/" + c), n] for c, n in a["command_rows"]])
           if a["command_rows"] else "")
        + ("<p>Other slash commands (built-in or plugin): "
           + ", ".join(f"{code('/' + c)} {n}" for c, n in a["other"][:20]) + ".</p>" if a["other"] else "")
        + "<h3>Method</h3><ul class=\"tight\">"
        f"<li>Sources: {a['files']:,} transcript files ({a['turns']:,} assistant turns) plus the "
        "<code>agents.missions</code> tables.</li>"
        "<li>Usage is the max per message id across streamed chunks.</li>"
        "<li>Cache writes split by TTL (5-minute 1.25×, 1-hour 2×); cache reads use each model's "
        "own rate from <code>scripts/routing-prices.json</code>.</li>"
        "<li>Re-dispatched means <code>attempt ≥ 2</code>; the first dispatch sets it to 1.</li>"
        "<li>A human correction is a typed message in an interactive session that follows an interrupt, "
        "or opens with a correction word when the previous assistant turn did not end in a question. "
        "Buckets are keyword matches. Days are UTC.</li>"
        "<li>Seats: <code>agent:&lt;type&gt;</code> from subagent <code>meta.json</code>; "
        "<code>conductor-orchestrator</code> is <code>~/.tmux/conductor/&lt;id&gt;</code>, "
        "<code>conductor-worker</code> anything under it.</li></ul>"
        "<h3>Schedule</h3><p>launchd <code>com.agents-nexus.flywheel-report</code>, daily at 04:37. "
        "Markdown in <code>~/.local/share/agents-nexus/flywheel/</code>, one doc-vault entry versioned "
        "daily. Run now with <code>task flywheel</code>.</p></div></details>")

    toc = [("glance", "At a glance"), ("findings", "Findings"), ("seats", "Spend by seat"),
           ("conductor", "Conductor"), ("corrections", "Corrections"), ("ledger", "Change ledger"),
           ("limits", "Limits"), ("appendix", "Appendix")]
    rail = ('<aside class="rail"><div class="rail-mark">Flywheel <span>/ daily</span></div><nav class="toc">'
            + "".join(f'<a href="#{sid}">{esc(t)}</a>' for sid, t in toc)
            + '</nav><div class="rail-note">Report-only. Built from tool names, model ids and token '
            "counts in Claude Code transcripts. No message text is read.</div></aside>")
    masthead = (f'<header class="masthead"><div class="eyebrow">Flywheel report · {esc(a["date"])} · '
                f'last {a["days"]} days</div><h1>{esc(headline)}</h1><p class="dek">{dek}</p>'
                f'<div class="stamp"><span>{esc(a["date"])}</span><span>scripts/flywheel-report.py</span>'
                f'<span>window: {a["days"]} days</span>'
                "<span>source: ~/.claude/projects transcripts + agents.missions</span></div></header>")
    body = "".join([
        section("glance", 1, "At a glance", f'<div class="tiles">{tiles}</div>'),
        section("findings", 2, "Findings", f'<div class="cards">{cards}</div>'),
        section("seats", 3, "Spend by seat", seats),
        section("conductor", 4, "Conductor", conductor),
        section("corrections", 5, "Corrections", corrections),
        section("ledger", 6, "Change ledger", ledger),
        section("limits", 7, "Limits", limits),
        section("appendix", 8, "Appendix", appendix),
    ])
    head = theme_head(HTML_TITLE)
    theme = re.search(r'name="doc-theme" content="([^"]+)"', head).group(1)
    footer = (f"<footer><span>generated {esc(a['date'])}</span><span>doc-vault: {esc(theme)}</span>"
              "<span>source of record: scripts/flywheel-report.py</span></footer>")
    return (head + '<body>\n<div class="shell">' + rail + "<main>" + masthead
            + body + footer + "</main></div>\n</body>\n</html>\n")


def doc_vault_bin():
    return shutil.which("doc-vault") or str(HOME / ".local" / "bin" / "doc-vault")


def deposit(path):
    cmd = [doc_vault_bin(), "put", str(path), "--collection", "notes"]
    for t in DEPOSIT_TAGS:
        cmd += ["--tag", t]
    res = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(res.stdout)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        sys.exit(f"doc-vault put failed (exit {res.returncode})")


def print_samples(samples, n):
    for label, rows in (("COUNTED as corrections", samples["counted"]),
                        ("EXCLUDED (answer to a question)", samples["excluded"])):
        print(f"\n=== {label}: showing {min(n, len(rows))} of {len(rows)} ===")
        for bucket, text in random.sample(rows, min(n, len(rows))):
            print(f"[{bucket}] {' '.join(text.split())}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", help="output path, or - for stdout (default: ~/.local/share/agents-nexus/flywheel/)")
    ap.add_argument("--deposit", action="store_true",
                    help=f"also write the themed HTML report to {HTML_PATH} and deposit it in doc-vault")
    ap.add_argument("--sample", type=int, metavar="N",
                    help="print N counted corrections and N excluded answers to this terminal to check the "
                         "heuristic; reads message text, needs a TTY, writes nothing")
    args = ap.parse_args()
    if args.sample:
        if not sys.stdout.isatty():
            sys.exit("--sample prints message text and only runs in an interactive terminal")
        print_samples(scan(args.days, sample=True)["samples"], args.sample)
        return
    models, cr_mult, cw_mult, cw1h_mult = load_prices()
    r = scan(args.days)
    rows, unpriced, blended = seat_rows(r["seats"], models, cr_mult, cw_mult, cw1h_mult)
    mdata, merr = missions(args.days)
    store = merge_series(r["daily"], r["cutoff_day"])
    entries, ledger_err = load_ledger()
    ledger = evaluate_ledger(entries, store, rows, blended)
    a = analyze(args.days, r, rows, unpriced, mdata, merr, ledger, ledger_err, store)
    report = render_md(a)
    if args.out == "-":
        sys.stdout.write(report)
    else:
        path = Path(args.out) if args.out else OUT_DIR / f"flywheel-{a['date']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report)
        print(path)
    if args.deposit:
        HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
        HTML_PATH.write_text(render_html(a))
        print(HTML_PATH)
        deposit(HTML_PATH)


if __name__ == "__main__":
    main()
