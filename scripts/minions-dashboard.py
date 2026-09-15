#!/usr/bin/env python3
"""Local read-only status page for the cron minions and the factory on 127.0.0.1."""
import argparse
import datetime as dt
import glob
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
REFRESH_S = 30
CACHE_S = 10
ROWS = 15
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LOOP_LOG = re.compile(r"^swarm-loop-(?:(.+)-)?mr(\d+)\.log$")
DAY_NAMES = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
LEDGER_COLUMNS = ["ts", "key", "action", "mission", "outcome", "class", "tier", "decision", "confidence",
                  "size", "cost", "minutes", "mode", "dispatched", "keys", "silent_hours", "findings",
                  "kinds", "reason", "summary"]
MISSION_SQL = (
    "SELECT m.id, m.status, m.jira_key, m.goal, m.route, m.started_at, m.finished_at,"
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
    sign = "" if secs >= 0 else "in "
    secs = abs(secs)
    if secs < 90:
        val = f"{secs}s"
    elif secs < 5400:
        val = f"{secs // 60}m"
    elif secs < 172800:
        val = f"{secs // 3600}h"
    else:
        val = f"{secs // 86400}d"
    return f"{sign}{val}" if sign else f"{val} ago"


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
            if cand > now and (best is None or cand < best):
                best = cand
            if cand > now:
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
    try:
        return json.loads((NEXUS / "launchd" / "descriptions.json").read_text())
    except (OSError, ValueError):
        return {}


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


def ledger(name, now):
    path = STATE / f"{name}.jsonl"
    if not path.exists():
        return None
    rows = []
    for line in tail_lines(path, 2000):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    today = now.date()
    week_ago = now - dt.timedelta(days=7)
    summary = {"name": name, "rows": len(rows), "rows_today": 0, "cost_today": 0.0, "cost_7d": 0.0,
               "last_ts": None, "recent": [], "columns": []}
    for row in rows:
        ts = parse_ts(row.get("ts"))
        cost = row.get("cost") if isinstance(row.get("cost"), (int, float)) else 0.0
        if ts:
            if ts.date() == today:
                summary["rows_today"] += 1
                summary["cost_today"] += cost
            if ts >= week_ago:
                summary["cost_7d"] += cost
            if summary["last_ts"] is None or ts > summary["last_ts"]:
                summary["last_ts"] = ts
    recent = [flatten(r) for r in rows[-ROWS:]][::-1]
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


def jobs(now):
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
        led = ledger(name, now)
        out.append({
            "label": label, "name": name, "schedule": fmt_schedule(pl), "loaded": label in table,
            "pid": pid, "last_exit": status, "last_run": last_run,
            "next": next_fire(pl, now, last_run), "description": desc.get(label, ""),
            "stamps": st, "ledger": led, "log": (LOGS / f"{name}.log").exists(),
            "minion": bool(led or st["files"]),
        })
    return out


def missions():
    try:
        sys.path.insert(0, str(NEXUS / "agent-runner"))
        from conductor_db import Db
        db = Db()
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
        for k in ("started_at", "finished_at", "last_event"):
            if r.get(k) is not None:
                r[k] = r[k].astimezone()
    return rows, None


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


def collect():
    now = now_local()
    js = jobs(now)
    ms, m_err = missions()
    state = {"now": now, "jobs": js, "missions": ms, "loops": loops(now), "panes": panes(now),
             "errors": [e for e in (m_err,) if e]}
    ledgers = [j["ledger"] for j in js if j["ledger"]]
    state["cost_today"] = sum(l["cost_today"] for l in ledgers)
    state["cost_7d"] = sum(l["cost_7d"] for l in ledgers)
    state["rows_today"] = sum(l["rows_today"] for l in ledgers)
    return state


def to_json(state):
    def default(o):
        if isinstance(o, (dt.datetime, dt.date)):
            return o.isoformat()
        return str(o)
    return json.dumps(state, default=default, indent=1)


def stylesheet():
    try:
        return (Path(__file__).resolve().with_suffix(".css")).read_text()
    except OSError:
        return ""


def esc(v):
    return html.escape("" if v is None else str(v))


def money(v):
    return f"${v:,.2f}"


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


def render(state):
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
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta http-equiv='refresh' content='{REFRESH_S}'>"
             f"<title>minions</title><style>{stylesheet()}</style></head><body><main>",
             f"<h1>Minions</h1><div class='sub'>{esc(now.strftime('%a %Y-%m-%d %H:%M:%S %Z'))} · refreshes every {REFRESH_S}s · "
             f"<a href='/state.json'>state.json</a></div>"]
    for err in state["errors"]:
        parts.append(f"<div class='warn'>{esc(err)}</div>")
    tiles = [
        (len(open_missions), "open missions", ""),
        (len(running_loops), "review loops live", ""),
        (len(fresh_panes), "panes active (6h)", ""),
        (money(state["cost_today"]), f"spent today · {money(state['cost_7d'])} 7d", ""),
        (state["rows_today"], "ledger rows today", ""),
        (len(failing), "jobs failing", "bad" if failing else "ok"),
    ]
    if upcoming:
        nxt = upcoming[0]
        tiles.append((clock(nxt["next"], now), f"next: {esc(nxt['name'])}", ""))
    parts.append("<div class='tiles'>" + "".join(
        f"<div class='tile'><div class='n {cls}'>{esc(n)}</div><div class='l'>{l}</div></div>" for n, l, cls in tiles) + "</div>")

    parts.append("<h2>Cron minions</h2><table><tr><th></th><th>job</th><th>schedule</th><th>last run</th><th>next</th>"
                 "<th>today</th><th class='num'>rows today</th><th class='num'>cost today</th><th class='num'>7d</th><th>purpose</th></tr>")
    for j in minions:
        cls, why = job_dot(j)
        led = j["ledger"]
        st = j["stamps"]
        today = "<span class='bad'>fail</span>" if st["fail"] else ("<span class='ok'>done</span>" if st["today"] or st["week"] else "<span class='dim'>—</span>")
        if st["fail"]:
            today += f" <span class='dim'>{esc(st['fail'][:80])}</span>"
        log_link = f"<a href='/log/{esc(j['name'])}'>log</a>" if j["log"] else ""
        led_link = f" · <a href='/ledger/{esc(j['name'])}'>ledger</a>" if led else ""
        parts.append(
            f"<tr><td><span class='dot {cls}' title='{esc(why)}'></span></td>"
            f"<td><b>{esc(j['name'])}</b><br><span class='dim'>{log_link}{led_link}</span></td>"
            f"<td>{esc(j['schedule'])}</td><td>{esc(ago(j['last_run'], now))}</td><td>{esc(clock(j['next'], now))}</td>"
            f"<td>{today}</td><td class='num'>{led['rows_today'] if led else ''}</td>"
            f"<td class='num'>{money(led['cost_today']) if led and led['cost_today'] else ''}</td>"
            f"<td class='num'>{money(led['cost_7d']) if led and led['cost_7d'] else ''}</td>"
            f"<td class='dim'>{esc(j['description'])}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Missions</h2>")
    if not state["missions"]:
        parts.append("<div class='dim'>none in the last 7 days</div>")
    else:
        parts.append("<table><tr><th>ticket</th><th>status</th><th>goal</th><th>route</th><th class='num'>round</th>"
                     "<th>subtasks</th><th>started</th><th>last event</th><th>MR</th></tr>")
        for m in open_missions + done_missions:
            openm = m["finished_at"] is None
            silent = openm and m["last_event"] and (now - m["last_event"]).total_seconds() > 3 * 3600
            cls = "bad" if m["status"] in ("failed", "abandoned", "escalated") else ("ok" if m["status"] == "done" else ("warn" if silent else ""))
            mr = m.get("mr") or ""
            mr_html = f"<a href='{esc(mr)}'>!{esc(mr.rsplit('/', 1)[-1])}</a>" if mr.startswith("https://") else ""
            when = m["last_event"] if openm else m["finished_at"]
            parts.append(
                f"<tr><td><b>{esc(m.get('jira_key') or '')}</b><br><span class='dim mono'>{esc(m['id'][:8])}</span></td>"
                f"<td class='{cls}'>{esc(m['status'])}{' · open' if openm else ''}</td><td>{esc((m.get('goal') or '')[:110])}</td>"
                f"<td>{esc(m.get('route') or '')}</td><td class='num'>{esc(m.get('round') if m.get('round') is not None else '')}</td>"
                f"<td class='mono'>{esc(m.get('subtasks') or '')}</td><td>{esc(ago(m['started_at'], now))}</td>"
                f"<td class='{'warn' if silent else ''}'>{esc(ago(when, now))}</td><td>{mr_html}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>Review loops</h2>")
    if not state["loops"]:
        parts.append("<div class='dim'>no loop logs</div>")
    else:
        parts.append("<table><tr><th>MR</th><th>project</th><th>state</th><th>started</th><th>last line</th><th>updated</th></tr>")
        for l in state["loops"]:
            cls = "ok" if l["state"] == "CONVERGED" else ("warn" if l["state"] == "silent" else ("bad" if l["state"] not in ("running", "done") else ""))
            parts.append(f"<tr><td><b>!{l['mr']}</b> <a class='dim' href='/log/{esc(l['log'][:-4])}'>log</a></td><td>{esc(l['project'])}</td>"
                         f"<td class='{cls}'>{esc(l['state'])}</td><td>{esc(ago(l['started'], now))}</td>"
                         f"<td class='mono dim'>{esc(l['last'])}</td><td>{esc(ago(l['modified'], now))}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>Fleet panes</h2>")
    if not state["panes"]:
        parts.append("<div class='dim'>registry empty</div>")
    else:
        parts.append("<table><tr><th>slot</th><th>name</th><th>workspace</th><th>cwd</th><th>registered</th></tr>")
        for p in state["panes"]:
            stale = p["at"] and now.timestamp() - p["at"] > 6 * 3600
            parts.append(f"<tr class='{'dim' if stale else ''}'><td class='mono'>{esc(p['slot'])}</td><td>{esc(p['name'])}</td>"
                         f"<td>{esc(p['workspace'])}</td><td class='mono dim'>{esc(p['cwd'].replace(str(HOME), '~'))}</td><td>{esc(ago(p['at'], now))}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>Ledgers</h2>")
    for j in minions:
        led = j["ledger"]
        if not led or not led["recent"]:
            continue
        cols = led["columns"]
        parts.append(f"<details><summary><b>{esc(j['name'])}</b> · {led['rows']} rows · last {esc(ago(led['last_ts'], now))}</summary><table><tr>"
                     + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr>")
        for r in led["recent"]:
            cells = []
            for c in cols:
                v = r.get(c, "")
                if c == "ts":
                    v = clock(parse_ts(v), now)
                elif c == "cost" and isinstance(v, (int, float)):
                    v = money(v)
                cells.append(f"<td class='{'num' if c in ('cost', 'minutes', 'confidence', 'findings', 'silent_hours') else ''}'>{esc(v)}</td>")
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("</table></details>")

    parts.append("<h2>Other launchd jobs</h2><table><tr><th></th><th>job</th><th>schedule</th><th>last run</th><th>next</th><th>purpose</th></tr>")
    for j in others:
        cls, why = job_dot(j)
        parts.append(f"<tr><td><span class='dot {cls}' title='{esc(why)}'></span></td><td>{esc(j['name'])}"
                     f"{' <span class=dim>(' + esc(why) + ')</span>' if cls != 'ok' else ''}</td><td>{esc(j['schedule'])}</td>"
                     f"<td>{esc(ago(j['last_run'], now))}</td><td>{esc(clock(j['next'], now))}</td><td class='dim'>{esc(j['description'])}</td></tr>")
    parts.append("</table></main></body></html>")
    return "".join(parts)


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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self.send(200, render(CACHE.get()))
        if path == "/state.json":
            return self.send(200, to_json(CACHE.get()), "application/json")
        if path.startswith("/log/"):
            return self.send_file(LOGS / f"{path[5:]}.log", path[5:])
        if path.startswith("/ledger/"):
            return self.send_file(STATE / f"{path[8:]}.jsonl", path[8:])
        self.send(404, "not found", "text/plain; charset=utf-8")

    def send_file(self, target, name):
        if not SAFE_NAME.match(name) or not target.is_file():
            return self.send(404, "not found", "text/plain; charset=utf-8")
        self.send(200, "\n".join(tail_lines(target, 400)), "text/plain; charset=utf-8")


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
    ap.add_argument("mode", choices=["serve", "render", "json"], help="serve the page, render it once, or dump the state")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--out")
    args = ap.parse_args()
    ensure_psycopg()
    if args.mode == "json":
        print(to_json(collect()))
        return
    if args.mode == "render":
        page = render(collect())
        if args.out:
            Path(args.out).write_text(page)
            print(args.out)
        else:
            sys.stdout.write(page)
        return
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"minions dashboard on http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
