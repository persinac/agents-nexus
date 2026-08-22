#!/usr/bin/env python3
"""nexus-scorecard: the five numbers that say whether the agent system earns its keep.

Little's Law (L = λW) is the frame for the flow sections: for each PR pipeline we
publish throughput (λ, merged/day), cycle time (W, request→merge), and observed
WIP (L, open now), then check L_observed against λ×W. A ratio well above 1 means
items are sitting in queue beyond what throughput and cycle time explain — i.e.
stuck WIP that no per-item metric will surface.

Sections and their sources (all pre-existing; this script adds no instrumentation):
  gate     ~/.tmux/gate-decisions.log   (epoch decision tier pane head sha12)
  flow     gh pr list (per configured repo)
  meta     ~/vault/Checkpoints/YYYY-MM-DD-<project>-checkpoint.md filenames
  minions  minions MCP over SSE (same fastmcp client the herder trigger uses)

Outputs a JSON snapshot to ~/.tmux/scorecard/ (latest.json + dated copy) and a
human-readable summary to stdout. `--json` prints the snapshot instead.
`--html PATH` renders the latest snapshot as a static dashboard page.

SLA targets live in SLAS below — tune them there, not in the render code.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

GATE_LOG = Path.home() / ".tmux" / "gate-decisions.log"
CHECKPOINT_DIR = Path.home() / "vault" / "Checkpoints"
SNAP_DIR = Path.home() / ".tmux" / "scorecard"
REPOS = os.environ.get(
    "NEXUS_SCORECARD_REPOS",
    "persinac/agents-nexus,persinac/minions-suite",
).split(",")
# Sessions maintaining the machinery itself, as opposed to work the machinery
# exists to enable. "infrastructure" is product infra and deliberately not here.
META_PROJECTS = set(
    os.environ.get("NEXUS_SCORECARD_META", "agents-nexus,minions-suite").split(",")
)
MINIONS_MCP_URL = os.environ.get("MINIONS_MCP_URL", "http://127.0.0.1:8321/sse")
MINIONS_VENV_PY = Path(
    os.environ.get(
        "MINIONS_VENV_PY",
        str(Path.home() / "repos/personal/minions-suite/.venv/bin/python3"),
    )
)
# Guard-block counts before this date include test-suite pollution (fixed
# 2026-08-20: both suites now log to a tempdir). Counts before it are shown
# but must not be trended.
GUARD_TRUST_SINCE = datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp()

SLAS = {
    "gate_interrupts_per_1k": {"target": 60, "op": "<=", "unit": "asks/1k commands (7d)"},
    "pr_cycle_p50_hours": {"target": 24, "op": "<=", "unit": "h, fleet 30d"},
    "pr_cycle_p90_hours": {"target": 72, "op": "<=", "unit": "h, fleet 30d"},
    "littles_wip_ratio": {"target": 1.5, "op": "<=", "unit": "L_obs / λW, fleet"},
    "meta_session_ratio": {"target": 0.35, "op": "<=", "unit": "share of sessions, 30d"},
    "minions_mcp_reachable": {"target": True, "op": "==", "unit": "tunnel serves tools"},
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))
    return xs[idx]


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def gate_metrics() -> dict:
    if not GATE_LOG.exists():
        return {"error": f"{GATE_LOG} missing"}
    now = now_utc().timestamp()
    windows = {"7d": now - 7 * 86400, "30d": now - 30 * 86400}
    out: dict = {}
    rows = []
    for line in GATE_LOG.read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            epoch = int(parts[0])
        except ValueError:
            continue
        rows.append((epoch, parts[1], parts[2]))
    for label, since in windows.items():
        w = [r for r in rows if r[0] >= since]
        classifier = [r for r in w if r[1] in ("approve", "ask")]
        asks = sum(1 for r in classifier if r[1] == "ask")
        blocks = [r for r in w if r[1] == "block"]
        tiers: dict[str, int] = {}
        for _, decision, tier in w:
            tiers[f"{decision}:{tier}"] = tiers.get(f"{decision}:{tier}", 0) + 1
        out[label] = {
            "classifier_decisions": len(classifier),
            "asks": asks,
            "interrupts_per_1k": round(asks / len(classifier) * 1000, 1) if classifier else None,
            "guard_blocks": len(blocks),
            "guard_blocks_trusted": sum(1 for r in blocks if r[0] >= GUARD_TRUST_SINCE),
            "by_tier": tiers,
        }
    out["note"] = "guard_blocks before 2026-08-20 include test-suite pollution; trusted = after fix"
    return out


# ---------------------------------------------------------------------------
# flow (Little's Law per repo)
# ---------------------------------------------------------------------------

def gh_json(args: list[str]) -> list | None:
    try:
        proc = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def repo_flow(repo: str, window_days: int = 30) -> dict:
    merged = gh_json(["pr", "list", "-R", repo, "--state", "merged",
                      "--json", "number,createdAt,mergedAt", "--limit", "200"])
    open_prs = gh_json(["pr", "list", "-R", repo, "--state", "open",
                        "--json", "number,createdAt", "--limit", "100"])
    if merged is None or open_prs is None:
        return {"repo": repo, "error": "gh query failed (missing repo or auth?)"}
    since = now_utc() - timedelta(days=window_days)
    cycles_h = []
    n_merged = 0
    for pr in merged:
        merged_at = datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00"))
        if merged_at < since:
            continue
        created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        n_merged += 1
        cycles_h.append((merged_at - created).total_seconds() / 3600)
    lam = n_merged / window_days  # PRs per day
    w_mean_days = (statistics.mean(cycles_h) / 24) if cycles_h else None
    l_expected = lam * w_mean_days if w_mean_days is not None else None
    l_observed = len(open_prs)
    return {
        "repo": repo,
        "window_days": window_days,
        "merged": n_merged,
        "lambda_per_day": round(lam, 2),
        "cycle_p50_h": round(pct(cycles_h, 50), 2) if cycles_h else None,
        "cycle_p90_h": round(pct(cycles_h, 90), 2) if cycles_h else None,
        "cycle_mean_h": round(statistics.mean(cycles_h), 2) if cycles_h else None,
        "wip_open": l_observed,
        "wip_expected": round(l_expected, 2) if l_expected is not None else None,
        "wip_ratio": round(l_observed / l_expected, 2) if l_expected else None,
    }


def fleet_flow(repos: list[str]) -> dict:
    per_repo = [repo_flow(r.strip()) for r in repos if r.strip()]
    ok = [r for r in per_repo if "error" not in r]
    cycles = []
    lam = 0.0
    wip = 0
    for r in ok:
        lam += r["lambda_per_day"]
        wip += r["wip_open"]
        # reconstruct an approximate sample for fleet percentiles: weight the
        # repo median by its merged count (cheap, avoids re-fetching every PR)
        if r["cycle_p50_h"] is not None:
            cycles += [r["cycle_p50_h"]] * r["merged"]
    w_mean_days = None
    weighted = [(r["cycle_mean_h"], r["merged"]) for r in ok if r["cycle_mean_h"] is not None]
    total = sum(n for _, n in weighted)
    if total:
        w_mean_days = sum(c * n for c, n in weighted) / total / 24
    l_expected = lam * w_mean_days if w_mean_days is not None else None
    return {
        "repos": per_repo,
        "fleet": {
            "lambda_per_day": round(lam, 2),
            "cycle_p50_h": pct(cycles, 50),
            "cycle_p90_h": pct(cycles, 90),
            "wip_open": wip,
            "wip_expected": round(l_expected, 2) if l_expected is not None else None,
            "wip_ratio": round(wip / l_expected, 2) if l_expected else None,
        },
    }


# ---------------------------------------------------------------------------
# meta-ratio
# ---------------------------------------------------------------------------

CKPT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)-checkpoint\.md$")


def meta_ratio(window_days: int = 30) -> dict:
    if not CHECKPOINT_DIR.is_dir():
        return {"error": f"{CHECKPOINT_DIR} missing"}
    since = now_utc().date() - timedelta(days=window_days)
    per_project: dict[str, int] = {}
    for f in CHECKPOINT_DIR.iterdir():
        m = CKPT_RE.match(f.name)
        if not m:
            continue
        try:
            day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < since:
            continue
        per_project[m.group(2)] = per_project.get(m.group(2), 0) + 1
    total = sum(per_project.values())
    meta = sum(n for p, n in per_project.items() if p in META_PROJECTS)
    return {
        "window_days": window_days,
        "sessions": total,
        "meta_sessions": meta,
        "meta_ratio": round(meta / total, 2) if total else None,
        "per_project": dict(sorted(per_project.items(), key=lambda kv: -kv[1])),
        "meta_projects": sorted(META_PROJECTS),
    }


# ---------------------------------------------------------------------------
# minions (over the same SSE MCP the herder trigger uses)
# ---------------------------------------------------------------------------

async def _minions_pull() -> dict:
    from fastmcp import Client

    out: dict = {"reachable": True}
    async with Client(MINIONS_MCP_URL) as client:
        for key, tool, args in (
            ("cost_30d", "get_cost_summary", {"days": 30}),
            ("reviews", "get_review_history", {"limit": 20}),
            ("waiting", "peek_engineer_work", {}),
            ("live_claims", "herder_status", {}),
        ):
            try:
                result = await client.call_tool(tool, args)
                out[key] = json.loads(result.content[0].text)
            except Exception as exc:  # per-tool: one failure shouldn't hide the rest
                out[key] = {"error": str(exc)}
    return out


def minions_metrics() -> dict:
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        # Re-exec the pull under the minions venv, which has the SDK.
        if not MINIONS_VENV_PY.exists():
            return {"reachable": False, "error": "fastmcp not importable and minions venv missing"}
        try:
            proc = subprocess.run(
                [str(MINIONS_VENV_PY), __file__, "--minions-json"],
                capture_output=True, text=True, timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"reachable": False, "error": str(exc)}
        if proc.returncode != 0:
            return {"reachable": False, "error": proc.stderr.strip()[-500:]}
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"reachable": False, "error": "venv pull emitted non-JSON"}
    try:
        return asyncio.run(_minions_pull())
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# SLA evaluation
# ---------------------------------------------------------------------------

def evaluate(snapshot: dict) -> dict:
    gate7 = snapshot["gate"].get("7d", {})
    fleet = snapshot["flow"].get("fleet", {})
    observed = {
        "gate_interrupts_per_1k": gate7.get("interrupts_per_1k"),
        "pr_cycle_p50_hours": fleet.get("cycle_p50_h"),
        "pr_cycle_p90_hours": fleet.get("cycle_p90_h"),
        "littles_wip_ratio": fleet.get("wip_ratio"),
        "meta_session_ratio": snapshot["meta"].get("meta_ratio"),
        "minions_mcp_reachable": snapshot["minions"].get("reachable", False),
    }
    results = {}
    for name, sla in SLAS.items():
        value = observed.get(name)
        if value is None:
            status = "no-data"
        elif sla["op"] == "<=":
            status = "ok" if value <= sla["target"] else "breach"
        else:
            status = "ok" if value == sla["target"] else "breach"
        results[name] = {"value": value, "target": sla["target"],
                         "op": sla["op"], "unit": sla["unit"], "status": status}
    return results


# ---------------------------------------------------------------------------
# html renderer (--html PATH): static dashboard from the latest snapshot
# ---------------------------------------------------------------------------

SLA_LABELS = {
    "gate_interrupts_per_1k": "Gate interrupts /1k",
    "pr_cycle_p50_hours": "PR cycle p50",
    "pr_cycle_p90_hours": "PR cycle p90",
    "littles_wip_ratio": "WIP ratio (L/λW)",
    "meta_session_ratio": "Meta-session share",
    "minions_mcp_reachable": "Minions MCP",
}


def _fmt(v, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "up" if v else "down"
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return f"{v}{suffix}"


def _bar_rows(rows: list[tuple[str, int, str, str]]) -> str:
    """rows: (label, count, css_color_var, chip_html). Direct-labeled h-bars."""
    peak = max((n for _, n, _, _ in rows), default=1) or 1
    out = []
    for label, n, var, chip in rows:
        w = max(1.5, n / peak * 100)
        out.append(
            f'<div class="bar-row"><span class="bar-label">{label}{chip}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{w:.1f}%;'
            f'background:var({var})"></span></span>'
            f'<span class="bar-value">{n}</span></div>'
        )
    return "\n".join(out)


def render_html(s: dict) -> str:
    slas = s.get("slas", {})
    tiles = []
    for name, r in slas.items():
        status = r["status"]
        pill = {"ok": "ok", "breach": "breach", "no-data": "no data"}[status]
        tiles.append(
            f'<div class="tile s-{status}"><div class="tile-head">'
            f'<span class="tile-name">{SLA_LABELS.get(name, name)}</span>'
            f'<span class="pill">{pill}</span></div>'
            f'<div class="tile-value">{_fmt(r["value"])}</div>'
            f'<div class="tile-target">target {r["op"]} {_fmt(r["target"])} · {r["unit"]}</div></div>'
        )

    fleet = s["flow"].get("fleet", {})
    lam = fleet.get("lambda_per_day")
    w50 = fleet.get("cycle_p50_h")
    l_obs = fleet.get("wip_open")
    l_exp = fleet.get("wip_expected")
    eq = (
        f'L&nbsp;=&nbsp;λ&nbsp;×&nbsp;W&nbsp;&nbsp;→&nbsp;&nbsp;{_fmt(lam)}/d × '
        f'{_fmt(round(w50 / 24, 3) if w50 is not None else None)}d = '
        f'<b>{_fmt(l_exp)}</b> expected · <b>{_fmt(l_obs)}</b> open now'
    )
    repo_rows = []
    for r in s["flow"].get("repos", []):
        if "error" in r:
            repo_rows.append(
                f'<tr><td class="mono">{r["repo"]}</td>'
                f'<td colspan="6" class="dim">{r["error"]}</td></tr>')
            continue
        repo_rows.append(
            f'<tr><td class="mono">{r["repo"]}</td><td>{r["merged"]}</td>'
            f'<td>{_fmt(r["lambda_per_day"], "/d")}</td><td>{_fmt(r["cycle_p50_h"], "h")}</td>'
            f'<td>{_fmt(r["cycle_p90_h"], "h")}</td><td>{r["wip_open"]}</td>'
            f'<td>{_fmt(r["wip_ratio"])}</td></tr>')

    gate7 = s["gate"].get("7d", {})
    tier_rows = []
    order = sorted(gate7.get("by_tier", {}).items(), key=lambda kv: -kv[1])
    for key, n in order:
        decision = key.split(":", 1)[0]
        var = {"approve": "--c-approve", "ask": "--c-warn", "block": "--c-bad"}.get(decision, "--c-approve")
        tier_rows.append((key, n, var, ""))
    gate_html = _bar_rows(tier_rows) if tier_rows else '<p class="dim">no decisions in window</p>'

    meta = s.get("meta", {})
    meta_rows = []
    for proj, n in list(meta.get("per_project", {}).items())[:9]:
        is_meta = proj in set(meta.get("meta_projects", []))
        chip = ' <span class="chip">meta</span>' if is_meta else ""
        meta_rows.append((proj, n, "--c-meta" if is_meta else "--c-prod", chip))
    meta_html = _bar_rows(meta_rows) if meta_rows else '<p class="dim">no checkpoints in window</p>'

    m = s.get("minions", {})
    reach = m.get("reachable", False)
    waiting = (m.get("waiting") or {}).get("count")
    live = len((m.get("live_claims") or {}).get("live", []))
    cost = (m.get("cost_30d") or {})
    minions_html = f'''
      <div class="kv"><span>MCP tunnel</span><b class="{'okc' if reach else 'badc'}">{'reachable' if reach else 'DOWN'}</b></div>
      <div class="kv"><span>Work waiting</span><b>{_fmt(waiting)}</b></div>
      <div class="kv"><span>Live herder claims</span><b>{live}</b></div>
      <div class="kv"><span>Review cost (30d)</span><b>${_fmt(cost.get("total_cost_usd"))}</b></div>
      <div class="kv"><span>Cost / merged PR</span><b class="dim">baseline pending — needs per-job agent cost aggregation</b></div>'''

    generated = s.get("generated_at", "")
    note = s["gate"].get("note", "")

    template = HTML_TEMPLATE
    for token, value in {
        "__GENERATED__": generated,
        "__TILES__": "\n".join(tiles),
        "__EQUATION__": eq,
        "__REPO_ROWS__": "\n".join(repo_rows),
        "__GATE_BARS__": gate_html,
        "__GATE_NOTE__": note,
        "__META_BARS__": meta_html,
        "__META_SUMMARY__": f'{_fmt(meta.get("meta_sessions"))} of {_fmt(meta.get("sessions"))} sessions '
                            f'(30d) went to the machinery itself',
        "__MINIONS__": minions_html,
    }.items():
        template = template.replace(token, str(value))
    return template


HTML_TEMPLATE = """<title>Nexus Scorecard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#F6F8F7; --surface:#FFFFFF; --ink:#1C2420; --muted:#5D6B65; --line:#DFE6E2;
  --accent:#1E7A5A; --c-meta:#1baf7a; --c-prod:#2a78d6;
  --c-approve:#1baf7a; --c-warn:#9A6A10; --c-bad:#B3413A;
  --ok:#1F7A47; --ok-bg:#E4F2EA; --warn:#9A6A10; --warn-bg:#F6EDDA; --bad:#B3413A; --bad-bg:#F7E6E4;
  --track:#EBEFEC;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#121615; --surface:#1A201D; --ink:#E8ECEA; --muted:#97A49D; --line:#2A322D;
    --accent:#58BD97; --c-meta:#199e70; --c-prod:#3987e5;
    --c-approve:#199e70; --c-warn:#D9A13F; --c-bad:#E07A72;
    --ok:#4CC07E; --ok-bg:#1C2E24; --warn:#D9A13F; --warn-bg:#302A1A; --bad:#E07A72; --bad-bg:#33211F;
    --track:#242B27;
  }
}
:root[data-theme="dark"]{
  --bg:#121615; --surface:#1A201D; --ink:#E8ECEA; --muted:#97A49D; --line:#2A322D;
  --accent:#58BD97; --c-meta:#199e70; --c-prod:#3987e5;
  --c-approve:#199e70; --c-warn:#D9A13F; --c-bad:#E07A72;
  --ok:#4CC07E; --ok-bg:#1C2E24; --warn:#D9A13F; --warn-bg:#302A1A; --bad:#E07A72; --bad-bg:#33211F;
  --track:#242B27;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:400 15px/1.55 Archivo,system-ui,sans-serif}
main{max-width:1080px;margin:0 auto;padding:40px 24px 64px}
.mono,.tile-value,.bar-value,.bar-label,td,.eq{font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-variant-numeric:tabular-nums}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;margin-bottom:8px}
h1{font-size:26px;font-weight:700;margin:0;letter-spacing:-.01em}
.stamp{color:var(--muted);font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px}
.sub{color:var(--muted);margin:0 0 28px;max-width:64ch}
.sla-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:28px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px 12px}
.tile-head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.tile-name{font-size:11.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.pill{font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
  padding:2px 8px;border-radius:99px;white-space:nowrap}
.s-ok .pill{background:var(--ok-bg);color:var(--ok)}
.s-breach .pill{background:var(--bad-bg);color:var(--bad)}
.s-no-data .pill{background:var(--track);color:var(--muted)}
.tile-value{font-size:28px;font-weight:600;margin-top:8px}
.s-breach .tile-value{color:var(--bad)}
.tile-target{font-size:11.5px;color:var(--muted);margin-top:2px}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
article{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px 20px}
article.wide{grid-column:1/-1}
h2{font-size:13px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
  color:var(--muted);margin:0 0 12px}
.eq{background:var(--track);border-radius:6px;padding:10px 14px;font-size:13.5px;margin:0 0 14px}
.eq b{color:var(--accent)}
table{border-collapse:collapse;width:100%;font-size:13px}
th{font:600 11px/1.3 Archivo,system-ui,sans-serif;letter-spacing:.05em;text-transform:uppercase;
  color:var(--muted);text-align:left;padding:6px 10px 6px 0;border-bottom:1px solid var(--line)}
td{padding:7px 10px 7px 0;border-bottom:1px solid var(--line);font-size:12.5px}
tr:last-child td{border-bottom:none}
.table-wrap{overflow-x:auto}
.bar-row{display:flex;align-items:center;gap:10px;margin:7px 0}
.bar-label{flex:0 0 200px;font-size:12px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{flex:1;height:10px;background:var(--track);border-radius:0 4px 4px 0;overflow:hidden}
.bar-fill{display:block;height:100%;border-radius:0 4px 4px 0}
.bar-value{flex:0 0 48px;text-align:right;font-size:12px;color:var(--muted)}
.chip{font:600 9.5px/1 Archivo,system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;
  color:var(--accent);border:1px solid var(--accent);border-radius:99px;padding:1.5px 6px;margin-left:6px;
  vertical-align:1px}
.kv{display:flex;justify-content:space-between;gap:16px;padding:7px 0;border-bottom:1px solid var(--line);
  font-size:13.5px}
.kv:last-child{border-bottom:none}
.kv span{color:var(--muted)}
.kv b{font-weight:600;text-align:right}
.okc{color:var(--ok)}.badc{color:var(--bad)}
.dim{color:var(--muted);font-size:12px}
.note{color:var(--muted);font-size:11.5px;margin-top:10px}
footer{margin-top:28px;color:var(--muted);font-size:12px;line-height:1.7}
footer code{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;
  background:var(--track);border-radius:4px;padding:1px 5px}
@media (prefers-reduced-motion: no-preference){ .bar-fill{transition:width .4s ease} }
</style>
<main>
  <header><h1>Nexus Scorecard</h1><span class="stamp">snapshot __GENERATED__</span></header>
  <p class="sub">Six SLAs for the agent fleet, framed by Little&rsquo;s Law: throughput (λ),
  cycle time (W), and work-in-progress (L) must agree — when observed WIP outruns λ×W,
  something is stuck.</p>

  <section class="sla-grid">
__TILES__
  </section>

  <section class="panels">
    <article class="wide">
      <h2>Flow — PR pipeline, 30 days</h2>
      <p class="eq">__EQUATION__</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Repo</th><th>Merged</th><th>λ</th><th>W p50</th><th>W p90</th><th>L open</th><th>L/λW</th></tr></thead>
        <tbody>
__REPO_ROWS__
        </tbody>
      </table></div>
      <p class="note">W here is PR-open → merge. Agent PRs merge in minutes, so the truer
      request → merge cycle needs minions task timestamps — next wiring step.</p>
    </article>

    <article>
      <h2>Permission gate — decisions by tier, 7 days</h2>
__GATE_BARS__
      <p class="note">__GATE_NOTE__</p>
    </article>

    <article>
      <h2>Sessions by project, 30 days</h2>
__META_BARS__
      <p class="note">__META_SUMMARY__</p>
    </article>

    <article class="wide">
      <h2>Minions suite</h2>
__MINIONS__
    </article>
  </section>

  <footer>
    Sources: <code>~/.tmux/gate-decisions.log</code> · <code>gh pr list</code> ·
    <code>~/vault/Checkpoints/</code> · minions MCP (SSE, herder&rsquo;s client).
    Refresh: <code>scripts/nexus-scorecard.py --html &lt;out&gt;</code>, then republish the artifact.
  </footer>
</main>
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def collect() -> dict:
    snapshot = {
        "generated_at": now_utc().isoformat(timespec="seconds"),
        "gate": gate_metrics(),
        "flow": fleet_flow(REPOS),
        "meta": meta_ratio(),
        "minions": minions_metrics(),
    }
    snapshot["slas"] = evaluate(snapshot)
    return snapshot


def summarize(s: dict) -> str:
    lines = [f"nexus scorecard — {s['generated_at']}", ""]
    for name, r in s["slas"].items():
        mark = {"ok": "✓", "breach": "✗", "no-data": "·"}[r["status"]]
        lines.append(f"  {mark} {name:26} {r['value']!s:>8}  (target {r['op']} {r['target']}, {r['unit']})")
    fleet = s["flow"].get("fleet", {})
    lines += ["", f"  flow: λ={fleet.get('lambda_per_day')}/day  "
                  f"W p50={fleet.get('cycle_p50_h')}h p90={fleet.get('cycle_p90_h')}h  "
                  f"L={fleet.get('wip_open')} open (expected {fleet.get('wip_expected')})"]
    for r in s["flow"].get("repos", []):
        if "error" in r:
            lines.append(f"    {r['repo']}: {r['error']}")
        else:
            lines.append(f"    {r['repo']}: merged={r['merged']} λ={r['lambda_per_day']}/d "
                         f"p50={r['cycle_p50_h']}h L={r['wip_open']} ratio={r['wip_ratio']}")
    meta = s["meta"]
    if "error" not in meta:
        lines += ["", f"  meta: {meta['meta_sessions']}/{meta['sessions']} sessions on "
                      f"{'+'.join(meta['meta_projects'])} = {meta['meta_ratio']}"]
    m = s["minions"]
    lines += ["", f"  minions: {'reachable' if m.get('reachable') else 'UNREACHABLE — ' + str(m.get('error'))[:120]}"]
    if m.get("reachable"):
        waiting = m.get("waiting", {})
        lines.append(f"    waiting={waiting.get('count')} live_claims={len(m.get('live_claims', {}).get('live', []))}")
    return "\n".join(lines)


def main() -> None:
    if "--minions-json" in sys.argv:
        print(json.dumps(asyncio.run(_minions_pull())))
        return
    if "--html" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--html") + 1])
        latest = SNAP_DIR / "latest.json"
        snapshot = json.loads(latest.read_text()) if latest.exists() else collect()
        out.write_text(render_html(snapshot))
        print(f"wrote {out}")
        return
    snapshot = collect()
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / "latest.json").write_text(json.dumps(snapshot, indent=2))
    dated = SNAP_DIR / f"{snapshot['generated_at'][:10]}.json"
    dated.write_text(json.dumps(snapshot, indent=2))
    if "--json" in sys.argv:
        print(json.dumps(snapshot, indent=2))
    else:
        print(summarize(snapshot))


if __name__ == "__main__":
    main()
