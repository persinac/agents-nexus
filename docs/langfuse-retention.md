# Langfuse data retention (self-hosted, OSS)

Langfuse's native **Data Retention Policies** feature is gated behind a paid
Enterprise license key, so it is **not available** on our OSS `langfuse/langfuse:3`
stack. We do retention ourselves with a ClickHouse TTL.

## Where the data lives (v3)

A trace is spread across three stores:

| Store | Contents | Grows? | Retention handled by |
|---|---|---|---|
| **ClickHouse** | `traces`, `observations`, `scores` (the UI data) | yes | **TTL (this doc)** |
| **MinIO** `events/` + `media/` | raw ingestion blobs + attachments | yes (big) | *left alone — PoC, intentional* |
| **Postgres** | projects, API keys, prompts, datasets (metadata) | no per-trace rows | n/a |

Postgres holds no per-trace rows in v3, so it needs no pruning.

## ClickHouse TTL (the retention mechanism)

10-day row-level TTL on the three trace tables:

```sql
ALTER TABLE traces       MODIFY TTL toDateTime(timestamp)  + INTERVAL 10 DAY;
ALTER TABLE observations MODIFY TTL toDateTime(start_time) + INTERVAL 10 DAY;
ALTER TABLE scores       MODIFY TTL toDateTime(timestamp)  + INTERVAL 10 DAY;
```

Apply / re-apply with:

```bash
task langfuse:retention                 # default 10 days
RETENTION_DAYS=14 task langfuse:retention
```

Notes:
- **Idempotent.** `MODIFY TTL` just rewrites the table's TTL clause.
- ClickHouse default `materialize_ttl_after_modify=1` immediately mutates
  existing parts, so old rows are purged on apply (not only on future merges).
- Tables are partitioned monthly (`toYYYYMM(...)`), so a 10-day window is
  enforced by **row-level** TTL merges rather than whole-part drops — fine at
  our scale (~20k rows/50 days reclaimed traces 2.5 GiB → ~780 MiB).

## ⚠️ Re-apply after every Langfuse upgrade

`task langfuse:update` can run schema migrations that **recreate** a table and
silently drop the custom TTL (Langfuse doesn't know about it). After any
upgrade, run `task langfuse:retention` again. Verify with:

```bash
docker exec langfuse-clickhouse clickhouse-client --user clickhouse \
  --password "$LANGFUSE_CLICKHOUSE_PASSWORD" -q \
  "SELECT name, extract(create_table_query,'TTL [^S]*') FROM system.tables
   WHERE database='default' AND name IN ('traces','observations','scores')"
```

## Cost history is preserved separately

The TTL deletes per-trace cost too. To keep long-term spend history (the
daily-cost-by-model view) past the 10-day window, a separate snapshot job
aggregates `observations` into a durable table before it ages out — see
[cost-snapshot](#cost-snapshot) below.

<a id="cost-snapshot"></a>
## Cost snapshot

Spend history is the one thing worth keeping past the trace TTL. Langfuse's own
cost view is computed live from `observations`, so once that table is pruned the
native UI only ever shows the last 10 days. The snapshot job rolls cost/usage up
into Postgres so the history survives indefinitely.

**Pipeline** (`scripts/langfuse-cost-snapshot.py`, stdlib only):
- Aggregates ClickHouse `observations` (type `GENERATION`) by `(day, project, model)`
  over the last `LANGFUSE_COST_LOOKBACK_DAYS` (default 14, > the 10-day TTL).
- Upserts into `agents.langfuse_cost_daily` (migration `003`) — `ON CONFLICT`, so
  it's idempotent. Finished days converge to their final total; days that later
  age out of ClickHouse are left untouched in Postgres. Postgres accumulates
  forever; ClickHouse holds 10 days.
- Talks to both DBs via `docker exec` — no drivers/host-ports — so it runs the
  same on Mac and the Linux mini-pc.

**Schedule** (daily, ~04:17 local):
- Mac: `launchd/com.agents-nexus.langfuse-cost-snapshot.plist`
  (auto-installed by `task launchd:install:all`, or `task launchd:install:langfuse-cost-snapshot`).
- Linux: `tmux/linux/systemd/langfuse-cost-snapshot.{service,timer}`
  (auto-installed by `tmux/linux/install.sh`).

**Run / view manually:**
```bash
task langfuse:cost-snapshot     # run the snapshot now (idempotent)
task langfuse:cost              # print recent daily spend by model from Postgres
```

**Viewing the rollup:**
- `task langfuse:cost` prints recent daily spend by model straight from the
  `agents.langfuse_cost_daily` Postgres rollup.
