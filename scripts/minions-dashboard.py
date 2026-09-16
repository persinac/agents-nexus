#!/usr/bin/env python3
"""Local read-only status page for the cron minions and the factory on 127.0.0.1."""
import argparse
import datetime as dt
import html
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
NEXUS = Path(os.environ.get("AGENTS_NEXUS_DIR") or Path(__file__).resolve().parent.parent)
STATE = Path(os.environ.get("NEXUS_CRON_STATE") or HOME / ".local/state/nexus-cron")
LOGS = Path(os.environ.get("NEXUS_LOG_DIR") or HOME / "Library/Logs/agents-nexus")
REGISTRY = Path(os.environ.get("NEXUS_TMUX_DIR") or HOME / ".tmux") / "registry"
LA_DIR = HOME / "Library/LaunchAgents"
PREFIX = "com.agents-nexus."
PORT = int(os.environ.get("MINIONS_DASHBOARD_PORT", "8312"))
REFRESH_S = 15
CACHE_S = 10
ROWS = 15
DETAIL_ROWS = 100
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MISSION_ID = re.compile(r"^[0-9a-f]{32}$")
LOOP_LOG = re.compile(r"^swarm-loop-(?:(.+)-)?mr(\d+)\.log$")
DAY_NAMES = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
LEDGER_COLUMNS = ["ts", "key", "action", "project", "mr", "author", "mission", "outcome", "class", "tier", "decision",
                  "confidence", "size", "cost", "minutes", "hours", "mode", "dispatched", "keys", "silent_hours",
                  "findings", "kinds", "sha", "reason", "summary", "title"]
PILL_COLUMNS = {"action", "outcome", "class", "tier", "decision", "mode", "status"}
NUM_COLUMNS = {"cost", "minutes", "confidence", "findings", "silent_hours"}
PALETTE = {
    "ok": "#4cc38a", "warn": "#f5a524", "bad": "#f0625d", "info": "#7aa2f7", "purple": "#c678dd",
    "teal": "#2ac3de", "dim": "#8a919e",
}
STATUS_TONE = {
    "done": "ok", "converged": "ok", "closed": "ok", "triaged": "ok", "weighed": "ok", "pass": "ok",
    "merged": "ok", "ready": "ok", "act now": "ok", "claim": "ok", "promote": "info", "reviewed": "ok",
    "dispatch": "info", "timeout": "warn",
    "running": "warn", "dispatched": "warn", "verifying": "warn", "planning": "warn", "silent": "warn",
    "would-close": "warn", "needs-info": "warn", "defer": "warn", "partial": "warn", "unverified": "warn",
    "schedule": "info", "in_progress": "info", "resumed": "info", "propose": "info", "dry-run": "info",
    "failed": "bad", "abandoned": "bad", "escalated": "bad", "error": "bad", "blocked": "bad", "fail": "bad",
    "abandon": "bad", "release": "bad", "blocker": "bad", "major": "warn", "minor": "dim",
    "decline": "dim", "stale": "dim", "out-of-scope": "dim", "skipped": "dim", "no-class": "dim",
    "not loaded": "bad", "live": "ok",
}
MISSION_SQL = (
    "SELECT m.id, m.status, m.jira_key, m.goal, m.route, m.model, m.orchestrator_effort, m.replan_count,"
    " m.started_at, m.finished_at,"
    " (SELECT max(e.ts) FROM mission_events e WHERE e.mission_id = m.id) AS last_event,"
    " (SELECT max((e.payload->>'round')::int) FROM mission_events e"
    "   WHERE e.mission_id = m.id AND e.event_type = 'round') AS round,"
    " (SELECT string_agg(s.subtask_key || ':' || s.status, ' ' ORDER BY s.subtask_key)"
    "    FROM mission_subtasks s WHERE s.mission_id = m.id) AS subtasks,"
    " (SELECT e.payload->>'url' FROM mission_events e"
    "   WHERE e.mission_id = m.id AND e.event_type = 'mr' ORDER BY e.ts DESC LIMIT 1) AS mr"
    " FROM missions m"
    " WHERE m.finished_at IS NULL OR m.finished_at > now() - interval '7 days'"
    " ORDER BY (m.finished_at IS NULL) DESC, coalesce(m.finished_at, m.started_at, m.created_at) DESC"
    " LIMIT 20")
DETAIL_SQL = "SELECT * FROM missions WHERE id = %s"
SUBTASKS_SQL = ("SELECT subtask_key, goal, repo, profile, status, effort, attempt, worker, result, updated_at"
                " FROM mission_subtasks WHERE mission_id = %s ORDER BY subtask_key")
EVENTS_SQL = "SELECT ts, event_type, subtask_id, payload FROM mission_events WHERE mission_id = %s ORDER BY ts"


def now_local():
    return dt.datetime.now().astimezone()


def ago(ts, now=None):
    if ts is None:
        return ""
    now = now or now_local()
    if isinstance(ts, (int, float)):
        ts = dt.datetime.fromtimestamp(ts).astimezone()
    if ts.tzinfo is None:
        ts = ts.astimezone()
    secs = int((now - ts).total_seconds())
    future = secs < 0
    secs = abs(secs)
    if secs < 90:
        val = f"{secs}s"
    elif secs < 5400:
        val = f"{secs // 60}m"
    elif secs < 172800:
        val = f"{secs // 3600}h"
    else:
        val = f"{secs // 86400}d"
    return f"in {val}" if future else f"{val} ago"


def elapsed(start, end=None):
    if start is None:
        return ""
    end = end or now_local()
    secs = int((end - start).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def parse_ts(text):
    if not text:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts.astimezone() if ts.tzinfo else ts.replace(tzinfo=now_local().tzinfo)


def clock(ts, now=None):
    if ts is None:
        return ""
    now = now or now_local()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    ts = ts.astimezone(now.tzinfo)
    return ts.strftime("%H:%M") if ts.date() == now.date() else ts.strftime("%a %H:%M")


def fmt_schedule(pl):
    if pl.get("KeepAlive"):
        return "daemon"
    if "StartInterval" in pl:
        s = int(pl["StartInterval"])
        if s % 3600 == 0:
            return f"every {s // 3600}h"
        if s % 60 == 0:
            return f"every {s // 60}m"
        return f"every {s}s"
    entries = pl.get("StartCalendarInterval")
    if not entries:
        return "at load" if pl.get("RunAtLoad") else "manual"
    entries = entries if isinstance(entries, list) else [entries]
    times = sorted({f"{e['Hour']:02d}:{e.get('Minute', 0):02d}" for e in entries if "Hour" in e})
    days = sorted({int(e["Weekday"]) % 7 for e in entries if "Weekday" in e})
    if len(times) > 3:
        tstr = f"{times[0]}–{times[-1]} ({len(times)}x)"
    else:
        tstr = ",".join(times) or "?"
    if days == [1, 2, 3, 4, 5]:
        return f"weekdays {tstr}"
    if days:
        return f"{','.join(DAY_NAMES[d] for d in days)} {tstr}"
    if any("Day" in e for e in entries):
        return f"monthly {tstr}"
    return f"daily {tstr}"


def next_fire(pl, now, last_run=None):
    if pl.get("KeepAlive"):
        return None
    if "StartInterval" in pl:
        if last_run is None:
            return None
        return dt.datetime.fromtimestamp(last_run).astimezone() + dt.timedelta(seconds=int(pl["StartInterval"]))
    entries = pl.get("StartCalendarInterval")
    if not entries:
        return None
    entries = entries if isinstance(entries, list) else [entries]
    best = None
    for e in entries:
        if "Hour" not in e:
            continue
        for offset in range(0, 8):
            day = now.date() + dt.timedelta(days=offset)
            if "Weekday" in e and day.isoweekday() % 7 != int(e["Weekday"]) % 7:
                continue
            if "Day" in e and day.day != int(e["Day"]):
                continue
            cand = dt.datetime.combine(day, dt.time(int(e["Hour"]), int(e.get("Minute", 0))), now.tzinfo)
            if cand > now:
                if best is None or cand < best:
                    best = cand
                break
    return best


def read_plist(path):
    """plistlib first; plutil second, because expat rejects a `--` inside an XML comment and launchd does not."""
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except Exception:
        pass
    try:
        out = subprocess.run(["plutil", "-convert", "xml1", "-o", "-", str(path)],
                             capture_output=True, timeout=5).stdout
        return plistlib.loads(out) if out else {}
    except Exception:
        return {}


def launchctl_table():
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith(PREFIX):
            pid = int(parts[0]) if parts[0].isdigit() else None
            try:
                status = int(parts[1])
            except ValueError:
                status = None
            table[parts[2]] = (pid, status)
    return table


def descriptions():
    """launchd/descriptions.json, filled in from the cron-minions registry table for jobs it does not name."""
    try:
        desc = json.loads((NEXUS / "launchd" / "descriptions.json").read_text())
    except (OSError, ValueError):
        desc = {}
    for label, write in registry_rows().items():
        desc.setdefault(label, write)
    return desc


def registry_rows():
    """Job label to its 'one write' cell from docs/cron-minions.md, when that doc is installed."""
    out = {}
    try:
        lines = (NEXUS / "docs" / "cron-minions.md").read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        if not ln.startswith(f"| `{PREFIX}"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split(" | ")]
        if len(cells) >= 3:
            out[cells[0].strip("`")] = re.sub(r"`([^`]*)`", r"\1", cells[2])[:220]
    return out


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def stamps(name, today, week):
    found = {"today": False, "week": False, "fail": None, "files": []}
    for p in sorted(STATE.glob(f"{name}.*")):
        if p.suffix in (".jsonl", ".md") or p.is_dir():
            continue
        found["files"].append(p.name)
        kind = p.name[len(name) + 1:]
        if kind == f"daily.{today}":
            found["today"] = True
        elif kind == f"weekly.{week}":
            found["week"] = True
        elif kind == f"fail.{today}":
            try:
                found["fail"] = p.read_text(errors="replace").strip()[:200] or "failed"
            except OSError:
                found["fail"] = "failed"
    return found


def tail_lines(path, n):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 65536 * max(1, n // 100))
            fh.seek(size - block)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def ledger(name, now, keep=ROWS):
    path = STATE / f"{name}.jsonl"
    if not path.exists():
        return None
    rows = []
    for line in tail_lines(path, 4000):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    today = now.date()
    week_ago = now - dt.timedelta(days=7)
    summary = {"name": name, "rows": len(rows), "rows_today": 0, "rows_7d": 0, "cost_today": 0.0, "cost_7d": 0.0,
               "last_ts": None, "recent": [], "columns": [], "week": []}
    for row in rows:
        ts = parse_ts(row.get("ts"))
        cost = row.get("cost") if isinstance(row.get("cost"), (int, float)) else 0.0
        if not ts:
            continue
        if ts.date() == today:
            summary["rows_today"] += 1
            summary["cost_today"] += cost
        if ts >= week_ago:
            summary["rows_7d"] += 1
            summary["cost_7d"] += cost
            summary["week"].append(row)
        if summary["last_ts"] is None or ts > summary["last_ts"]:
            summary["last_ts"] = ts
    recent = [flatten(r) for r in rows[-keep:]][::-1]
    keys = {k for r in recent for k in r}
    summary["columns"] = [c for c in LEDGER_COLUMNS if c in keys]
    summary["recent"] = recent
    return summary


def flatten(row):
    out = {}
    for k, v in row.items():
        if k == "findings" and isinstance(v, list):
            out["findings"] = len(v)
            kinds = {}
            for f in v:
                kind = f.get("kind", "?") if isinstance(f, dict) else "?"
                kinds[kind] = kinds.get(kind, 0) + 1
            out["kinds"] = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
        elif isinstance(v, list):
            out[k] = " ".join(str(x) for x in v)[:90]
        elif isinstance(v, dict):
            out[k] = json.dumps(v)[:90]
        elif isinstance(v, str):
            out[k] = v if len(v) <= 90 else v[:87] + "..."
        else:
            out[k] = v
    return out


def jobs(now, keep=ROWS):
    table = launchctl_table()
    desc = descriptions()
    today = now.date().isoformat()
    week = now.strftime("%G-W%V")
    out = []
    for path in sorted(LA_DIR.glob(f"{PREFIX}*.plist")):
        label = path.name[:-6]
        name = label[len(PREFIX):]
        pl = read_plist(path)
        pid, status = table.get(label, (None, None))
        candidates = [LOGS / f"{name}.log", LOGS / f"{name}.launchd.log"]
        for key in ("StandardOutPath", "StandardErrorPath"):
            if pl.get(key):
                candidates.append(Path(pl[key]))
        last_run = max((m for m in (mtime(c) for c in candidates) if m), default=None)
        st = stamps(name, today, week)
        led = ledger(name, now, keep)
        out.append({
            "label": label, "name": name, "schedule": fmt_schedule(pl), "loaded": label in table,
            "pid": pid, "last_exit": status, "last_run": last_run,
            "next": next_fire(pl, now, last_run), "description": desc.get(label, ""),
            "stamps": st, "ledger": led, "log": (LOGS / f"{name}.log").exists(),
            "minion": bool(led or st["files"]),
        })
    return out


def open_db():
    sys.path.insert(0, str(NEXUS / "agent-runner"))
    from conductor_db import Db
    return Db()


def localize(row, keys):
    for k in keys:
        if row.get(k) is not None:
            row[k] = row[k].astimezone()
    return row


def missions():
    try:
        db = open_db()
    except Exception as exc:
        return [], f"missions unavailable: {type(exc).__name__}: {exc}"
    try:
        rows = db._rows(db.conn.execute(MISSION_SQL))
    except Exception as exc:
        return [], f"missions query failed: {exc}"
    finally:
        db.close()
    for r in rows:
        r["id"] = str(r["id"])
        localize(r, ("started_at", "finished_at", "last_event"))
    return rows, None


def mission_detail(mid):
    db = open_db()
    try:
        rows = db._rows(db.conn.execute(DETAIL_SQL, (mid,)))
        if not rows:
            return None
        mission = localize(rows[0], ("created_at", "updated_at", "started_at", "finished_at"))
        mission["subtasks"] = [localize(s, ("updated_at",)) for s in db._rows(db.conn.execute(SUBTASKS_SQL, (mid,)))]
        mission["events"] = [localize(e, ("ts",)) for e in db._rows(db.conn.execute(EVENTS_SQL, (mid,)))]
    finally:
        db.close()
    return mission


def loops(now):
    out = []
    for path in LOGS.glob("swarm-loop-*.log"):
        m = LOOP_LOG.match(path.name)
        if not m:
            continue
        lines = [ln for ln in tail_lines(path, 400) if ln.strip()]
        exits = [ln for ln in lines if "exit state:" in ln]
        if exits:
            state = exits[-1].split("exit state:", 1)[1].strip()
        elif any("queue done" in ln for ln in lines):
            state = "done"
        else:
            state = "running"
        modified = mtime(path)
        if state == "running" and modified and now.timestamp() - modified > 3 * 3600:
            state = "silent"
        started = None
        for ln in lines:
            if "queue start" in ln:
                started = parse_ts(ln[:19].replace(" ", "T"))
                break
        last = lines[-1] if lines else ""
        out.append({"mr": int(m.group(2)), "project": m.group(1) or "", "state": state,
                    "started": started, "modified": modified,
                    "last": re.sub(r"^\S+ \S+\s+", "", last)[:120], "log": path.name})
    out.sort(key=lambda r: r["modified"] or 0, reverse=True)
    return out[:10]


def panes(now):
    out = []
    if not REGISTRY.is_dir():
        return out
    for path in REGISTRY.iterdir():
        if not path.is_file():
            continue
        entry = {}
        try:
            for ln in path.read_text(errors="replace").splitlines():
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    entry[k.strip()] = v.strip()
        except OSError:
            continue
        at = int(entry["AT"]) if entry.get("AT", "").isdigit() else None
        out.append({"slot": entry.get("SLOT", path.name), "name": entry.get("NAME", ""),
                    "workspace": entry.get("WORKSPACE", ""), "cwd": entry.get("CWD", ""), "at": at})
    out.sort(key=lambda r: r["at"] or 0, reverse=True)
    return out[:25]


def week_stats(ledgers, missions_rows, loop_rows, now):
    week_ago = now - dt.timedelta(days=7)
    stats = {"rows": 0, "cost": 0.0, "act_now": 0, "weighed": 0, "weigh_cost": 0.0, "triaged": 0,
             "closed": 0, "claims": 0, "mrs": 0, "converged": 0, "reviews": 0, "review_dispatches": 0}
    for led in ledgers:
        stats["rows"] += led["rows_7d"]
        stats["cost"] += led["cost_7d"]
        for row in led["week"]:
            cost = row.get("cost") if isinstance(row.get("cost"), (int, float)) else 0.0
            if row.get("tier"):
                stats["weighed"] += 1
                stats["weigh_cost"] += cost
                if row.get("tier") == "ACT NOW":
                    stats["act_now"] += 1
            if row.get("outcome") == "triaged":
                stats["triaged"] += 1
            if row.get("decision") == "closed":
                stats["closed"] += 1
            if row.get("action") == "claim":
                stats["claims"] += 1
            if row.get("action") == "reviewed":
                stats["reviews"] += 1
            if row.get("action") == "dispatch" and row.get("mr"):
                stats["review_dispatches"] += 1
    for m in missions_rows:
        when = m.get("finished_at") or m.get("started_at")
        if m.get("mr") and when and when >= week_ago:
            stats["mrs"] += 1
    for l in loop_rows:
        if l["state"] == "CONVERGED" and l["modified"] and l["modified"] >= week_ago.timestamp():
            stats["converged"] += 1
    stats["cost_per_act_now"] = stats["weigh_cost"] / stats["act_now"] if stats["act_now"] else None
    return stats


def collect():
    time.tzset()
    now = now_local()
    js = jobs(now)
    ms, m_err = missions()
    lp = loops(now)
    state = {"now": now, "jobs": js, "missions": ms, "loops": lp, "panes": panes(now),
             "errors": [e for e in (m_err,) if e]}
    ledgers = [j["ledger"] for j in js if j["ledger"]]
    state["cost_today"] = sum(l["cost_today"] for l in ledgers)
    state["cost_7d"] = sum(l["cost_7d"] for l in ledgers)
    state["rows_today"] = sum(l["rows_today"] for l in ledgers)
    state["week"] = week_stats(ledgers, ms, lp, now)
    return state


def to_json(state):
    def default(o):
        if isinstance(o, (dt.datetime, dt.date)):
            return o.isoformat()
        return str(o)
    slim = {k: v for k, v in state.items()}
    slim["jobs"] = [{**j, "ledger": j["ledger"] and {k: v for k, v in j["ledger"].items() if k != "week"}} for j in state["jobs"]]
    return json.dumps(slim, default=default, indent=1)


def asset(suffix):
    try:
        return Path(__file__).resolve().with_suffix(suffix).read_text()
    except OSError:
        return ""


def esc(v):
    return html.escape("" if v is None else str(v))


def money(v):
    return f"${v:,.2f}"


def tone_of(value):
    return STATUS_TONE.get(str(value).strip().lower(), "")


def pill(value, tone=None):
    if value in (None, ""):
        return ""
    tone = tone or tone_of(value) or "dim"
    return f"<span class='pill {tone}'>{esc(value)}</span>"


def job_dot(j):
    if not j["loaded"]:
        return "bad", "not loaded"
    if j["stamps"]["fail"]:
        return "bad", "failed today"
    if j["last_exit"] not in (None, 0):
        return "warn", f"last exit {j['last_exit']}"
    if j["pid"]:
        return "ok", f"running pid {j['pid']}"
    return "ok", "idle"


def page(title, body, now, live=True):
    script = f"<script>{asset('.js')}</script>" if live else ""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{esc(title)}</title>"
            f"<style>{asset('.css')}</style></head><body data-refresh='{REFRESH_S}'><main>"
            f"<nav class='nav'><a href='/'>Minions</a><a href='/#missions'>Missions</a><a href='/#loops'>Loops</a>"
            f"<a href='/#ledgers'>Ledgers</a><a href='/state.json'>state.json</a>"
            f"<span class='clock' id='clock'>{esc(now.strftime('%a %Y-%m-%d %H:%M:%S %Z'))}</span></nav>"
            f"{body}</main>{script}</body></html>")


def tiles_html(items):
    return "<div class='tiles'>" + "".join(
        f"<div class='tile'><div class='n {cls}'>{esc(n)}</div><div class='l'>{l}</div></div>" for n, l, cls in items) + "</div>"


def cell(col, row, now):
    value = row.get(col, "")
    if col == "ts":
        return f"<td class='dim'>{esc(clock(parse_ts(value), now))}</td>"
    if col == "cost" and isinstance(value, (int, float)):
        return f"<td class='num'>{esc(money(value))}</td>"
    if col in PILL_COLUMNS:
        return f"<td>{pill(value)}</td>"
    if col == "key":
        return f"<td><b>{esc(value)}</b></td>"
    if col == "mr" and value != "":
        url = str(row.get("url") or "")
        text = f"!{esc(value)}"
        return f"<td><a href='{esc(url)}'>{text}</a></td>" if url.startswith("https://") else f"<td>{text}</td>"
    if col == "project":
        return f"<td class='dim'>{esc(str(value).rsplit('/', 1)[-1])}</td>"
    if col == "sha":
        return f"<td class='mono dim'>{esc(str(value)[:8])}</td>"
    return f"<td class='{'num' if col in NUM_COLUMNS else ''}'>{esc(value)}</td>"


def ledger_table(led, now):
    cols = led["columns"]
    rows = ["<table><tr>" + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr>"]
    for r in led["recent"]:
        rows.append("<tr>" + "".join(cell(c, r, now) for c in cols) + "</tr>")
    rows.append("</table>")
    return "".join(rows)


def render_overview(state):
    now = state["now"]
    js = state["jobs"]
    minions = [j for j in js if j["minion"]]
    others = [j for j in js if not j["minion"]]
    open_missions = [m for m in state["missions"] if m["finished_at"] is None]
    done_missions = [m for m in state["missions"] if m["finished_at"] is not None]
    running_loops = [l for l in state["loops"] if l["state"] in ("running", "silent")]
    fresh_panes = [p for p in state["panes"] if p["at"] and now.timestamp() - p["at"] < 6 * 3600]
    failing = [j for j in js if j["loaded"] and (j["stamps"]["fail"] or j["last_exit"] not in (None, 0))]
    upcoming = sorted((j for j in js if j["next"] and j["loaded"]), key=lambda j: j["next"])
    wk = state["week"]
    parts = []
    for err in state["errors"]:
        parts.append(f"<div class='warn'>{esc(err)}</div>")
    tiles = [
        (len(open_missions), "open missions", "warn" if open_missions else ""),
        (len(running_loops), "review loops live", "warn" if running_loops else ""),
        (len(fresh_panes), "panes active (6h)", ""),
        (money(state["cost_today"]), "spent today", ""),
        (state["rows_today"], "ledger rows today", ""),
        (len(failing), "jobs failing", "bad" if failing else "ok"),
    ]
    if upcoming:
        nxt = upcoming[0]
        tiles.append((clock(nxt["next"], now), f"next: {esc(nxt['name'])}", ""))
    parts.append(f"<section data-live='tiles'>{tiles_html(tiles)}</section>")
    week = [
        (money(wk["cost"]), "spent, 7 days", ""),
        (wk["rows"], "ledger rows, 7 days", ""),
        (wk["triaged"], "tickets triaged", ""),
        (f"{wk['act_now']} / {wk['weighed']}", "ACT NOW of weighed", ""),
        (money(wk["cost_per_act_now"]) if wk["cost_per_act_now"] is not None else "—", "weigh spend per ACT NOW", ""),
        (wk["claims"], "tickets claimed", ""),
        (wk["mrs"], "MRs opened", "ok" if wk["mrs"] else ""),
        (wk["converged"], "loops converged", "ok" if wk["converged"] else ""),
        (f"{wk['reviews']} / {wk['review_dispatches']}", "swarm reviews done / started", ""),
        (wk["closed"], "stale tickets closed", ""),
    ]
    parts.append(f"<h2>Last 7 days</h2><section data-live='week'>{tiles_html(week)}</section>")

    parts.append("<h2 id='minions'>Cron minions</h2><section data-live='minions'><table><tr><th></th><th>job</th><th>schedule</th>"
                 "<th>last run</th><th>next</th><th>today</th><th class='num'>rows today</th><th class='num'>cost today</th>"
                 "<th class='num'>7d</th><th>purpose</th></tr>")
    for j in minions:
        cls, why = job_dot(j)
        led = j["ledger"]
        st = j["stamps"]
        if st["fail"]:
            today = pill("fail") + f" <span class='dim'>{esc(st['fail'][:80])}</span>"
        elif st["today"] or st["week"]:
            today = pill("done")
        else:
            today = "<span class='dim'>—</span>"
        parts.append(
            f"<tr><td><span class='dot {cls}' title='{esc(why)}'></span></td>"
            f"<td><a href='/minion/{esc(j['name'])}'><b>{esc(j['name'])}</b></a></td>"
            f"<td>{esc(j['schedule'])}</td><td>{esc(ago(j['last_run'], now))}</td><td>{esc(clock(j['next'], now))}</td>"
            f"<td>{today}</td><td class='num'>{led['rows_today'] if led else ''}</td>"
            f"<td class='num'>{money(led['cost_today']) if led and led['cost_today'] else ''}</td>"
            f"<td class='num'>{money(led['cost_7d']) if led and led['cost_7d'] else ''}</td>"
            f"<td class='dim'>{esc(j['description'])}</td></tr>")
    parts.append("</table></section>")

    parts.append("<h2 id='missions'>Missions</h2><section data-live='missions'>")
    if not state["missions"]:
        parts.append("<div class='dim'>none in the last 7 days</div>")
    else:
        parts.append("<table><tr><th>ticket</th><th>status</th><th>goal</th><th>route</th><th>model</th><th class='num'>round</th>"
                     "<th>subtasks</th><th>started</th><th>last event</th><th>MR</th></tr>")
        for m in open_missions + done_missions:
            openm = m["finished_at"] is None
            silent = openm and m["last_event"] and (now - m["last_event"]).total_seconds() > 3 * 3600
            mr = m.get("mr") or ""
            mr_html = f"<a href='{esc(mr)}'>!{esc(mr.rsplit('/', 1)[-1])}</a>" if mr.startswith("https://") else ""
            when = m["last_event"] if openm else m["finished_at"]
            subs = " ".join(pill(s.split(":", 1)[1]) if ":" in s else esc(s) for s in (m.get("subtasks") or "").split())
            parts.append(
                f"<tr><td><a href='/mission/{esc(m['id'])}'><b>{esc(m.get('jira_key') or m['id'][:8])}</b></a></td>"
                f"<td>{pill('silent' if silent else m['status'])}</td><td>{esc((m.get('goal') or '')[:110])}</td>"
                f"<td>{esc(m.get('route') or '')}</td><td class='dim'>{esc((m.get('model') or '').replace('claude-', ''))}</td>"
                f"<td class='num'>{esc(m.get('round') if m.get('round') is not None else '')}</td>"
                f"<td>{subs}</td><td>{esc(ago(m['started_at'], now))}</td>"
                f"<td class='{'warn' if silent else ''}'>{esc(ago(when, now))}</td><td>{mr_html}</td></tr>")
        parts.append("</table>")
    parts.append("</section>")

    parts.append("<h2 id='loops'>Review loops</h2><section data-live='loops'>")
    if not state["loops"]:
        parts.append("<div class='dim'>no loop logs</div>")
    else:
        parts.append("<table><tr><th>MR</th><th>project</th><th>state</th><th>started</th><th>last line</th><th>updated</th></tr>")
        for l in state["loops"]:
            parts.append(f"<tr><td><b>!{l['mr']}</b> <a class='dim' href='/log/{esc(l['log'][:-4])}'>log</a></td><td>{esc(l['project'])}</td>"
                         f"<td>{pill(l['state'])}</td><td>{esc(ago(l['started'], now))}</td>"
                         f"<td class='mono dim'>{esc(l['last'])}</td><td>{esc(ago(l['modified'], now))}</td></tr>")
        parts.append("</table>")
    parts.append("</section>")

    parts.append("<h2 id='panes'>Fleet panes</h2><section data-live='panes'>")
    if not state["panes"]:
        parts.append("<div class='dim'>registry empty</div>")
    else:
        parts.append("<table><tr><th>slot</th><th>name</th><th>workspace</th><th>cwd</th><th>registered</th></tr>")
        for p in state["panes"]:
            stale = p["at"] and now.timestamp() - p["at"] > 6 * 3600
            parts.append(f"<tr class='{'dim' if stale else ''}'><td class='mono'>{esc(p['slot'])}</td><td>{esc(p['name'])}</td>"
                         f"<td>{esc(p['workspace'])}</td><td class='mono dim'>{esc(p['cwd'].replace(str(HOME), '~'))}</td><td>{esc(ago(p['at'], now))}</td></tr>")
        parts.append("</table>")
    parts.append("</section>")

    parts.append("<h2 id='ledgers'>Ledgers</h2><section data-live='ledgers'>")
    for j in minions:
        led = j["ledger"]
        if not led or not led["recent"]:
            continue
        parts.append(f"<details id='ledger-{esc(j['name'])}'><summary><b>{esc(j['name'])}</b> · {led['rows']} rows · last {esc(ago(led['last_ts'], now))}"
                     f" · <a href='/minion/{esc(j['name'])}'>all</a></summary>{ledger_table(led, now)}</details>")
    parts.append("</section>")

    parts.append("<h2>Other launchd jobs</h2><section data-live='others'><table><tr><th></th><th>job</th><th>schedule</th><th>last run</th><th>next</th><th>purpose</th></tr>")
    for j in others:
        cls, why = job_dot(j)
        parts.append(f"<tr><td><span class='dot {cls}' title='{esc(why)}'></span></td><td>{esc(j['name'])}"
                     f"{' ' + pill(why) if cls != 'ok' else ''}</td><td>{esc(j['schedule'])}</td>"
                     f"<td>{esc(ago(j['last_run'], now))}</td><td>{esc(clock(j['next'], now))}</td><td class='dim'>{esc(j['description'])}</td></tr>")
    parts.append("</table></section>")
    return page("minions", "".join(parts), now)


def render_minion(job, now):
    led = job["ledger"]
    cls, why = job_dot(job)
    head = (f"<h1><span class='dot {cls}'></span>{esc(job['name'])}</h1>"
            f"<div class='sub'>{esc(job['description'])}</div>")
    facts = [
        (esc(job["schedule"]), "schedule", ""),
        (esc(ago(job["last_run"], now)) or "—", "last run", ""),
        (esc(clock(job["next"], now)) or "—", "next fire", ""),
        (pill(why), "launchd", ""),
    ]
    if led:
        facts += [(led["rows_today"], "rows today", ""), (money(led["cost_today"]), "spent today", ""),
                  (led["rows_7d"], "rows, 7 days", ""), (money(led["cost_7d"]), "spent, 7 days", "")]
    parts = [head, f"<section data-live='facts'>{tiles_html(facts)}</section>"]
    st = job["stamps"]
    if st["files"]:
        parts.append("<h2>Stamps</h2><section data-live='stamps'><div class='mono dim'>" + "<br>".join(esc(f) for f in st["files"][-12:]) + "</div></section>")
    if led and led["recent"]:
        parts.append(f"<h2>Ledger · last {len(led['recent'])} of {led['rows']}</h2><section data-live='ledger'>{ledger_table(led, now)}</section>")
    if job["log"]:
        lines = tail_lines(LOGS / f"{job['name']}.log", 150)
        parts.append(f"<h2>Log · last {len(lines)} lines · <a href='/log/{esc(job['name'])}'>raw</a></h2>"
                     f"<section data-live='log'><pre>{esc(chr(10).join(lines))}</pre></section>")
    return page(job["name"], "".join(parts), now)


def event_summary(event_type, payload):
    p = payload if isinstance(payload, dict) else {}
    if event_type == "created":
        return p.get("goal", "")
    if event_type == "classified":
        return f"{p.get('route', '')} · {p.get('type', '')} · {' '.join(p.get('repos') or [])}"
    if event_type == "planned":
        return p.get("strategy", "")
    if event_type == "round":
        return f"round {p.get('round')} · worker effort {p.get('worker_effort', '')}"
    if event_type == "workspace":
        return f"{p.get('repo', '')} @ {p.get('branch', '')}"
    if event_type == "dispatched":
        return f"{p.get('subtask', '')} via {p.get('via', '')} · {p.get('profile', '')}"
    if event_type == "worker_started":
        return f"{p.get('subtask', '')} on {p.get('worker', '')} · effort {p.get('effort', '')}"
    if event_type == "worker_done":
        return f"{p.get('status', '')}: {(p.get('handoff') or '')[:220]}"
    if event_type == "verify_started":
        return f"round {p.get('round')}"
    if event_type == "verdict":
        v = p.get("verdict") if isinstance(p.get("verdict"), dict) else p
        findings = v.get("findings") or []
        return f"{'pass' if v.get('pass') else 'fail'} · {len(findings)} finding(s) · {v.get('recommendation', '')}"
    if event_type == "replan":
        return f"{len(p.get('findings') or [])} finding(s) fed back"
    if event_type == "synthesized":
        return (p.get("artifact") or "").splitlines()[0][:200] if p.get("artifact") else ""
    if event_type == "committed":
        return " · ".join(f"{b.get('repo', '')}:{b.get('branch', '')} @ {(b.get('head') or '')[:8]}" for b in p.get("branches") or [])
    if event_type == "mr":
        return p.get("url", "")
    if event_type == "jira":
        return f"{p.get('key', '')}{' · ' + str(p['error']) if p.get('error') else ''}"
    if event_type == "reported":
        return " ".join(p.get("targets") or [])
    if event_type == "abandoned":
        return f"by {p.get('by', '')} after {p.get('silent_hours', '?')}h silent"
    text = json.dumps(p, default=str)
    return text[:200]


def verify_rounds(events):
    rounds = {}
    order = []

    def bucket(n):
        if n not in rounds:
            rounds[n] = {"num": n, "effort": "", "workers": [], "verdict": None, "replan": None, "started": None}
            order.append(n)
        return rounds[n]

    current = 0
    for e in events:
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        et = e["event_type"]
        if et == "round":
            current = p.get("round", current)
            b = bucket(current)
            b["effort"] = p.get("worker_effort", "")
            b["started"] = e["ts"]
        elif et == "worker_done":
            bucket(current)["workers"].append({"status": p.get("status", ""), "handoff": p.get("handoff") or "", "ts": e["ts"]})
        elif et == "verdict":
            n = p.get("round", current)
            v = p.get("verdict") if isinstance(p.get("verdict"), dict) else p
            bucket(n)["verdict"] = {"pass": bool(v.get("pass")), "findings": v.get("findings") or [],
                                    "recommendation": v.get("recommendation", ""), "check": v.get("check"), "ts": e["ts"]}
        elif et == "replan":
            bucket(p.get("round", current))["replan"] = {"findings": p.get("findings") or [], "ts": e["ts"]}
    return [rounds[n] for n in order]


def render_rounds(rounds, now):
    if not rounds:
        return ""
    parts = ["<h2>Verify rounds</h2><div class='rounds'>"]
    for i, r in enumerate(rounds):
        v = r["verdict"]
        head = f"<span class='round-num'>Round {r['num']}</span>"
        if r["effort"]:
            head += f" <span class='dim'>worker effort {esc(r['effort'])}</span>"
        verdict_pill = pill("pass" if v["pass"] else "fail") if v else pill("running", "warn")
        parts.append(f"<div class='round'><div class='round-head'>{head}{verdict_pill}</div><div class='flow'>")
        for w in r["workers"]:
            parts.append(f"<div class='agent eng'><span class='role'>worker</span> {pill(w['status'])}"
                         f"<span class='meta'>{esc(clock(w['ts'], now))}</span>"
                         f"<details><summary>handoff</summary><div class='handoff'>{esc(w['handoff'])}</div></details></div>")
        if v:
            sev = {}
            for f in v["findings"]:
                s = (f.get("severity") if isinstance(f, dict) else None) or "minor"
                sev[s] = sev.get(s, 0) + 1
            chips = " ".join(pill(f"{n} {s}", tone_of(s) or "dim") for s, n in sorted(sev.items()))
            check = v.get("check") if isinstance(v.get("check"), dict) else None
            check_txt = "" if not check else (" · check " + ("green" if check.get("ok") else "red" if check.get("ran") else "not run"))
            parts.append(f"<span class='arrow'>→</span><div class='agent rev'><span class='role'>verifier</span> {chips}"
                         f"<span class='meta'>{esc(clock(v['ts'], now))}{esc(check_txt)}"
                         f"{' · ' + esc(v['recommendation']) if v['recommendation'] else ''}</span>")
            if v["findings"]:
                parts.append("<details><summary>findings</summary><table class='findings'>")
                for f in v["findings"]:
                    if not isinstance(f, dict):
                        continue
                    parts.append(f"<tr><td>{pill(f.get('severity') or 'minor')}</td><td class='dim'>{esc(f.get('lens') or '')}</td>"
                                 f"<td>{esc(f.get('what') or '')}<div class='mono dim'>{esc(f.get('where') or '')}</div>"
                                 f"{('<div class=dim>' + esc(f.get('fix_hint')) + '</div>') if f.get('fix_hint') else ''}</td></tr>")
                parts.append("</table></details>")
            parts.append("</div>")
        parts.append("</div></div>")
        if i < len(rounds) - 1:
            parts.append("<div class='down'>↓</div>")
    parts.append("</div>")
    return "".join(parts)


def render_mission(m, now):
    events = m["events"]
    by_type = {}
    for e in events:
        by_type.setdefault(e["event_type"], []).append(e)
    mr = next((e["payload"].get("url") for e in reversed(by_type.get("mr", [])) if isinstance(e.get("payload"), dict) and e["payload"].get("url")), None)
    head = (f"<h1>{esc(m.get('jira_key') or m['id'][:8])} {pill(m['status'])}</h1>"
            f"<div class='sub'>{esc(m.get('goal') or '')}</div>")
    facts = [
        (esc(m.get("route") or ""), "route", ""),
        (esc((m.get("model") or "").replace("claude-", "")) or "—", "model", ""),
        (esc(m.get("orchestrator_effort") or "—"), "orchestrator effort", ""),
        (esc(m.get("replan_count") if m.get("replan_count") is not None else "—"), "replans", ""),
        (esc(elapsed(m.get("started_at"), m.get("finished_at"))) or "—", "duration", ""),
        (esc(clock(m.get("started_at"), now)), "started", ""),
    ]
    if mr:
        facts.append((f"<a href='{esc(mr)}'>!{esc(mr.rsplit('/', 1)[-1])}</a>", "merge request", ""))
    parts = [head, f"<section data-live='facts'>{tiles_html(facts)}</section>"]
    plan = m.get("plan") if isinstance(m.get("plan"), dict) else {}
    if plan.get("strategy"):
        parts.append(f"<h2>Plan</h2><p>{esc(plan['strategy'])}</p>")
    if m["subtasks"]:
        parts.append("<h2>Subtasks</h2><section data-live='subtasks'><table><tr><th>key</th><th>goal</th><th>repo</th><th>profile</th>"
                     "<th>status</th><th>effort</th><th class='num'>attempt</th><th>worker</th><th>updated</th></tr>")
        for s in m["subtasks"]:
            parts.append(f"<tr><td class='mono'>{esc(s['subtask_key'])}</td><td>{esc((s.get('goal') or '')[:160])}</td>"
                         f"<td>{esc(s.get('repo') or '')}</td><td>{esc(s.get('profile') or '')}</td><td>{pill(s.get('status'))}</td>"
                         f"<td>{esc(s.get('effort') or '')}</td><td class='num'>{esc(s.get('attempt') or '')}</td>"
                         f"<td class='dim'>{esc(s.get('worker') or '')}</td><td>{esc(ago(s.get('updated_at'), now))}</td></tr>")
        parts.append("</table></section>")
    parts.append(f"<section data-live='rounds'>{render_rounds(verify_rounds(events), now)}</section>")
    art = next((e["payload"].get("artifact") for e in by_type.get("synthesized", []) if isinstance(e.get("payload"), dict)), None)
    if art:
        parts.append(f"<h2>Report</h2><details id='report'><summary>synthesized artifact</summary><pre>{esc(art)}</pre></details>")
    parts.append(f"<h2>Timeline · {len(events)} events</h2><section data-live='timeline'><table><tr><th>when</th><th>event</th><th>subtask</th><th>detail</th></tr>")
    for e in reversed(events):
        parts.append(f"<tr><td class='dim'>{esc(e['ts'].strftime('%H:%M:%S') if e['ts'].date() == now.date() else e['ts'].strftime('%a %H:%M:%S'))}</td>"
                     f"<td class='mono'>{esc(e['event_type'])}</td><td class='mono dim'>{esc((e.get('subtask_id') or '')[:8])}</td>"
                     f"<td class='dim'>{esc(event_summary(e['event_type'], e.get('payload')))}</td></tr>")
    parts.append("</table></section>")
    return page(m.get("jira_key") or m["id"][:8], "".join(parts), now)


class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.at = 0.0
        self.state = None

    def get(self):
        with self.lock:
            if self.state is None or time.time() - self.at > CACHE_S:
                self.state = collect()
                self.at = time.time()
            return self.state


CACHE = Cache()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def not_found(self):
        self.send(404, "not found", "text/plain; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self.send(200, render_overview(CACHE.get()))
        if path == "/state.json":
            return self.send(200, to_json(CACHE.get()), "application/json")
        if path == "/healthz":
            return self.send(200, json.dumps({"ok": True, "ts": now_local().isoformat()}), "application/json")
        if path.startswith("/log/"):
            return self.send_file(LOGS / f"{path[5:]}.log", path[5:])
        if path.startswith("/ledger/"):
            return self.send_file(STATE / f"{path[8:]}.jsonl", path[8:])
        if path.startswith("/minion/"):
            return self.send_minion(path[8:])
        if path.startswith("/mission/"):
            return self.send_mission(path[9:])
        self.not_found()

    def send_file(self, target, name):
        if not SAFE_NAME.match(name) or not target.is_file():
            return self.not_found()
        self.send(200, "\n".join(tail_lines(target, 400)), "text/plain; charset=utf-8")

    def send_minion(self, name):
        if not SAFE_NAME.match(name):
            return self.not_found()
        now = now_local()
        job = next((j for j in jobs(now, DETAIL_ROWS) if j["name"] == name), None)
        if not job:
            return self.not_found()
        self.send(200, render_minion(job, now))

    def send_mission(self, mid):
        if not MISSION_ID.match(mid):
            return self.not_found()
        try:
            m = mission_detail(mid)
        except Exception as exc:
            return self.send(503, f"missions unavailable: {esc(exc)}", "text/plain; charset=utf-8")
        if not m:
            return self.not_found()
        self.send(200, render_mission(m, now_local()))


def ensure_psycopg():
    try:
        import psycopg
        return psycopg
    except ImportError:
        pass
    venv = NEXUS / "agent-runner" / ".venv" / "bin" / "python"
    if venv.exists() and not os.environ.get("MINIONS_DASHBOARD_REEXEC"):
        os.environ["MINIONS_DASHBOARD_REEXEC"] = "1"
        os.execv(str(venv), [str(venv), *sys.argv])


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        epilog="Sources: ~/Library/LaunchAgents and launchctl, NEXUS_CRON_STATE ledgers and stamps, "
               "NEXUS_LOG_DIR job and loop logs, the Conductor DB via agent-runner (re-execs into its "
               ".venv when psycopg is missing), and the NEXUS_TMUX_DIR pane registry. Port: MINIONS_DASHBOARD_PORT.")
    ap.add_argument("mode", choices=["serve", "render", "json"], help="serve the pages, render the overview once, or dump the state")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--out")
    args = ap.parse_args()
    ensure_psycopg()
    if args.mode == "json":
        print(to_json(collect()))
        return
    if args.mode == "render":
        html_page = render_overview(collect())
        if args.out:
            Path(args.out).write_text(html_page)
            print(args.out)
        else:
            sys.stdout.write(html_page)
        return
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"minions dashboard on http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
