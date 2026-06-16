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
import json
import os
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

# ── ClickHouse: aggregate observations → one row per (day, project, model) ───
CH_QUERY = f"""
SELECT toString(toDate(start_time))                 AS day,
       project_id,
       coalesce(provided_model_name, 'unknown')     AS model,
       count()                                       AS observations,
       toString(round(sum(total_cost), 6))           AS total_cost,
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


def fetch_rows() -> list[dict]:
    out = run([
        DOCKER, "exec", CH_CONTAINER, "clickhouse-client",
        "--user", CH_USER, "--password", CH_PASSWORD,
        "--format", "JSONEachRow", "-q", CH_QUERY,
    ])
    rows: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        rows.append({
            "day": r["day"],
            "project_id": r["project_id"],
            "model": r["model"],
            "observations": int(r["observations"]),
            "total_cost": round(float(r["total_cost"]), 6),
            "input_tokens": int(r["input_tokens"]),
            "output_tokens": int(r["output_tokens"]),
            "cache_creation_tokens": int(r["cache_creation_tokens"]),
            "cache_read_tokens": int(r["cache_read_tokens"]),
            "total_tokens": int(r["total_tokens"]),
            # nested objects so jsonb_to_recordset lands them straight into jsonb
            "cost_details": json.loads(r["cost_json"]),
            "usage_details": json.loads(r["usage_json"]),
        })
    return rows


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

    rows = fetch_rows()
    if not rows:
        print("[cost-snapshot] no GENERATION observations in window — nothing to snapshot")
        return

    days = sorted({r["day"] for r in rows})
    total = sum(r["total_cost"] for r in rows)
    print(f"[cost-snapshot] aggregated {len(rows)} (day×model) rows across "
          f"{len(days)} days ({days[0]}…{days[-1]}), ${total:,.2f} total")

    if args.dry_run:
        for r in rows[:10]:
            print(f"  {r['day']}  {r['model']:<28} ${r['total_cost']:>10,.4f}  "
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
