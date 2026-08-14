#!/usr/bin/env python3
"""Snapshot Langfuse daily LLM cost/usage into a durable Postgres rollup.

Langfuse keeps per-trace cost in ClickHouse `observations`, which we TTL to
10 days (see docs/langfuse-retention.md). This job aggregates those observations
by (day, project, model) and upserts them into `agents.langfuse_cost_daily` in
nexus-postgres BEFORE the source rows age out — so the cost view survives long
after the traces themselves are pruned.

Design:
  - Re-aggregates the last LOOKBACK_DAYS (default 14 > the 10-day TTL) on every
    run and upserts. Idempotent: a finished day always converges to its final
    total, and rows for days that have since aged out of ClickHouse are left
    untouched in Postgres. So Postgres accumulates forever; ClickHouse holds 10d.
  - Talks to both databases via `docker exec` (no DB drivers / host ports needed),
    so it runs identically on the Mac and the Linux mini-pc.
  - Stdlib only — runs under launchd (Mac) / systemd (Linux) with system python3.

Usage:
  python3 scripts/langfuse-cost-snapshot.py
  python3 scripts/langfuse-cost-snapshot.py --dry-run        # print, don't write
  python3 scripts/langfuse-cost-snapshot.py --emit-json PATH # also dump full
                                                             # rollup as JSON
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

# ── Config (env-overridable) ─────────────────────────────────────────────────
LOOKBACK_DAYS = int(os.getenv("LANGFUSE_COST_LOOKBACK_DAYS", "14"))
CH_CONTAINER = os.getenv("LANGFUSE_CLICKHOUSE_CONTAINER", "langfuse-clickhouse")
CH_USER = os.getenv("LANGFUSE_CLICKHOUSE_USER", "clickhouse")
CH_PASSWORD = os.getenv("LANGFUSE_CLICKHOUSE_PASSWORD", "clickhouse")
PG_CONTAINER = os.getenv("NEXUS_POSTGRES_CONTAINER", "nexus-postgres")


def docker_bin() -> str:
    """Resolve the docker CLI — launchd/systemd start with a minimal PATH."""
    cand = os.environ.get("DOCKER_BIN") or shutil.which("docker")
    if cand:
        return cand
    for p in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker", "/usr/bin/docker"):
        if os.path.exists(p):
            return p
    return "docker"


DOCKER = docker_bin()

# ── Prices: scripts/routing-prices.json is the single source of truth ────────
# Cost is computed HERE from the token buckets, not read from Langfuse's
# total_cost column. Langfuse prices a turn only if its own `models` table knows
# the model, and that table lagged claude-opus-5/sonnet-5 by months — 2,813 of
# 2,823 turns came back NULL, and float(None) crashed this job for six weeks
# while the rollup silently froze. The four token buckets are always correct
# (they come straight from the API's usage block), so they are the safe basis.
PRICES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routing-prices.json")
DATE_SUFFIX = re.compile(r"-20[0-9]{6}$")


def load_prices() -> tuple[dict, float, float]:
    """Read the price table, or exit. Deliberately fails loudly: an unreadable
    table must not degrade into pricing every row at zero, which is the silent
    failure this whole change exists to remove."""
    try:
        with open(PRICES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.exit(f"[cost-snapshot] cannot read {PRICES_PATH}: {exc}")

    models = data.get("models") or {}
    if not models:
        sys.exit(f"[cost-snapshot] {PRICES_PATH} lists no models — "
                 "refusing to price all traffic at zero")

    today = dt.date.today().isoformat()
    for name, price in sorted(models.items()):
        expiry = price.get("valid_until")
        if expiry and today > expiry:
            sys.stderr.write(
                f"[cost-snapshot] WARNING: the {name} price expired {expiry} "
                f"(today is {today}). Cost for it is wrong until "
                f"routing-prices.json is updated.\n")

    return (models,
            float(data.get("cache_read_mult", 0.10)),
            float(data.get("cache_write_mult", 1.25)))


def resolve_price(model: str, models: dict) -> dict | None:
    """Exact name, then the name minus a trailing -YYYYMMDD (the API reports
    e.g. claude-haiku-4-5-20251001 while the table is keyed on the alias).

    No tier-keyword fallback, unlike routing-report.py's price(). Matching
    'opus' would price claude-opus-5 off claude-opus-4-8 — invisible today only
    because both are $5/$25, and a plausible wrong number the moment they
    diverge. This rollup is the only durable cost history, so an unrecognised
    model must be visibly unpriced rather than quietly approximated."""
    if model in models:
        return models[model]
    return models.get(DATE_SUFFIX.sub("", model))


def turn_cost(row: dict, price: dict, cache_read_mult: float,
              cache_write_mult: float) -> float:
    """USD for one (day, project, model) group at that model's list rates."""
    inp = price["input"]
    return round((
        row["input_tokens"] * inp
        + row["cache_creation_tokens"] * inp * cache_write_mult
        + row["cache_read_tokens"] * inp * cache_read_mult
        + row["output_tokens"] * price["output"]
    ) / 1_000_000, 6)


# ── ClickHouse: aggregate observations → one row per (day, project, model) ───
CH_QUERY = f"""
SELECT toString(toDate(start_time))                 AS day,
       project_id,
       coalesce(provided_model_name, 'unknown')     AS model,
       count()                                       AS observations,
       sum(usage_details['input'])                   AS input_tokens,
       sum(usage_details['output'])                  AS output_tokens,
       sum(usage_details['cache_creation_input_tokens']) AS cache_creation_tokens,
       sum(usage_details['cache_read_input_tokens']) AS cache_read_tokens,
       sum(usage_details['total'])                   AS total_tokens,
       toJSONString(CAST(sumMap(cost_details)  AS Map(String, Float64))) AS cost_json,
       toJSONString(CAST(sumMap(usage_details) AS Map(String, UInt64)))  AS usage_json
FROM observations
WHERE type = 'GENERATION'
  AND start_time >= now() - INTERVAL {LOOKBACK_DAYS} DAY
GROUP BY day, project_id, model
ORDER BY day
"""


def run(cmd: list[str], *, stdin: str | None = None) -> str:
    res = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        sys.stderr.write(f"[cost-snapshot] command failed ({res.returncode}): {' '.join(cmd[:4])}…\n")
        sys.stderr.write(res.stderr.strip() + "\n")
        sys.exit(res.returncode)
    return res.stdout


def fetch_rows() -> tuple[list[dict], list[str]]:
    """Returns (rows, unpriced_models). An unpriced row keeps its token counts
    and carries total_cost=None → SQL NULL, so it is visibly missing a price
    rather than indistinguishable from a genuinely free day."""
    models, cache_read_mult, cache_write_mult = load_prices()
    out = run([
        DOCKER, "exec", CH_CONTAINER, "clickhouse-client",
        "--user", CH_USER, "--password", CH_PASSWORD,
        "--format", "JSONEachRow", "-q", CH_QUERY,
    ])
    rows: list[dict] = []
    unpriced: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        row = {
            "day": r["day"],
            "project_id": r["project_id"],
            "model": r["model"],
            "observations": int(r["observations"]),
            "input_tokens": int(r["input_tokens"]),
            "output_tokens": int(r["output_tokens"]),
            "cache_creation_tokens": int(r["cache_creation_tokens"]),
            "cache_read_tokens": int(r["cache_read_tokens"]),
            "total_tokens": int(r["total_tokens"]),
            # nested objects so jsonb_to_recordset lands them straight into jsonb
            "cost_details": json.loads(r["cost_json"]),
            "usage_details": json.loads(r["usage_json"]),
        }
        price = resolve_price(row["model"], models)
        if price is None:
            unpriced.add(row["model"])
            row["total_cost"] = None
        else:
            row["total_cost"] = turn_cost(row, price, cache_read_mult, cache_write_mult)
        rows.append(row)
    return rows, sorted(unpriced)


# ── Postgres: upsert via jsonb_to_recordset (dollar-quoted, injection-safe) ──
UPSERT_SQL = """
INSERT INTO agents.langfuse_cost_daily AS t
    (day, project_id, model, observations, total_cost,
     input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
     total_tokens, cost_details, usage_details, updated_at)
SELECT day, project_id, model, observations, total_cost,
       input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
       total_tokens, cost_details, usage_details, now()
FROM jsonb_to_recordset($json${payload}$json$::jsonb) AS x(
       day date, project_id text, model text, observations bigint,
       total_cost numeric, input_tokens bigint, output_tokens bigint,
       cache_creation_tokens bigint, cache_read_tokens bigint, total_tokens bigint,
       cost_details jsonb, usage_details jsonb)
ON CONFLICT (day, project_id, model) DO UPDATE SET
       observations          = EXCLUDED.observations,
       total_cost            = EXCLUDED.total_cost,
       input_tokens          = EXCLUDED.input_tokens,
       output_tokens         = EXCLUDED.output_tokens,
       cache_creation_tokens = EXCLUDED.cache_creation_tokens,
       cache_read_tokens     = EXCLUDED.cache_read_tokens,
       total_tokens          = EXCLUDED.total_tokens,
       cost_details          = EXCLUDED.cost_details,
       usage_details         = EXCLUDED.usage_details,
       updated_at            = now();
"""


def psql(sql: str) -> str:
    """Run SQL inside nexus-postgres using the container's own PG_* env."""
    return run([
        DOCKER, "exec", "-i", PG_CONTAINER, "sh", "-c",
        'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
        '-v ON_ERROR_STOP=1 -q -f -',
    ], stdin=sql)


def upsert(rows: list[dict]) -> None:
    payload = json.dumps(rows, separators=(",", ":"))
    if "$json$" in payload:  # impossible for our data, but never emit broken SQL
        raise ValueError("payload contains the dollar-quote delimiter")
    psql(UPSERT_SQL.format(payload=payload))


def emit_json(path: str) -> None:
    """Dump the full durable rollup to a JSON file (e.g. a dashboard feed)."""
    out = psql(
        "SELECT coalesce(json_agg(row_to_json(c) ORDER BY day, total_cost DESC), '[]'::json) "
        "FROM (SELECT day, project_id, model, observations, total_cost, "
        "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, "
        "total_tokens FROM agents.langfuse_cost_daily) c;"
    ).strip()
    with open(path, "w") as f:
        f.write(out + "\n")
    print(f"[cost-snapshot] wrote rollup JSON → {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Snapshot Langfuse daily cost → Postgres")
    ap.add_argument("--dry-run", action="store_true", help="aggregate and print, don't write to Postgres")
    ap.add_argument("--emit-json", metavar="PATH", help="also dump the full rollup to a JSON file")
    args = ap.parse_args()

    rows, unpriced = fetch_rows()
    if not rows:
        print("[cost-snapshot] no GENERATION observations in window — nothing to snapshot")
        return

    days = sorted({r["day"] for r in rows})
    total = sum(r["total_cost"] for r in rows if r["total_cost"] is not None)
    print(f"[cost-snapshot] aggregated {len(rows)} (day×model) rows across "
          f"{len(days)} days ({days[0]}…{days[-1]}), ${total:,.2f} total")

    # Counted and named, never silently dropped: a model missing from the price
    # table is the one thing that makes the total quietly too low.
    if unpriced:
        n = sum(1 for r in rows if r["total_cost"] is None)
        sys.stderr.write(
            f"[cost-snapshot] WARNING: {n} row(s) carry NULL cost — no price entry for "
            f"{', '.join(unpriced)}. Token counts are still correct. "
            f"Add the model to {PRICES_PATH} and re-run to price them.\n")

    if args.dry_run:
        for r in rows[:10]:
            cost = "     (none)" if r["total_cost"] is None else f"${r['total_cost']:>10,.4f}"
            print(f"  {r['day']}  {r['model']:<28} {cost}  "
                  f"{r['total_tokens']:>12,} tok  ({r['observations']} obs)")
        if len(rows) > 10:
            print(f"  … and {len(rows) - 10} more")
        print("[cost-snapshot] dry-run — not written")
        return

    upsert(rows)
    print(f"[cost-snapshot] upserted {len(rows)} rows into agents.langfuse_cost_daily")

    if args.emit_json:
        emit_json(args.emit_json)


if __name__ == "__main__":
    main()
