# Langfuse data retention (self-hosted, OSS)

Langfuse's native **Data Retention Policies** feature is gated behind a paid
Enterprise license key, so it is **not available** on our OSS
`langfuse/langfuse:4.43.0` stack (both compose files). We do retention ourselves
with a ClickHouse TTL.

The cost snapshot and `routing-report.py` read v4's `events_core`. On a stack
still running v3 they fail with "table does not exist".

## Where the data lives (v4)

A trace is spread across three stores:

| Store | Contents | Grows? | Retention handled by |
|---|---|---|---|
| **ClickHouse** | `events_core` + `events_full` (the UI data) | yes | **TTL (this doc)** |
| **MinIO** `events/` + `media/` | raw ingestion blobs + attachments | yes (big) | *left alone — PoC, intentional* |
| **Postgres** | projects, API keys, prompts, datasets (metadata) | no per-trace rows | n/a |

v4 writes every observation to `events_core` (costs, tokens, model) and
`events_full` (inputs and outputs). It leaves the v3 tables `traces`,
`observations` and `scores` empty, so anything still reading them sees no data.
`events_full` is the big one: 268 MiB for 3,888 rows (about 2 days) on 2026-09-25,
against 1 MiB for `events_core`.

Postgres holds no per-trace rows, so it needs no pruning.

## A working TTL is not durability — the rollup is

**A stack recreate empties ClickHouse regardless of the TTL.** On 2026-08-13 the
TTL was verified correctly applied at 10 days, and ClickHouse still held only
**2 days** of observations: the containers had been recreated ~26h earlier and the
history went with them. So the 10-day window is a *ceiling*, not a guarantee —
any `docker compose down -v`, volume prune, or image rebuild resets it to zero.

The only durable cost history is **`agents.langfuse_cost_daily` in
nexus-postgres**, written by `scripts/langfuse-cost-snapshot.py`. That makes the
snapshot job, not the TTL, the thing that protects history — and it means a dead
snapshot job is a silent data-loss bug, not just a missing dashboard. It crashed
from 2026-07-01 to 2026-08-13 and six weeks of cost history is gone permanently:
it aged out of ClickHouse and never reached the rollup.

It happened again in September 2026, in two gaps:

- **2026-08-20 → ~09-16:** ClickHouse 26.x read integer timestamps as seconds, so
  every row landed around the year 2105. The snapshot exited 0 and wrote one
  `2105-01-05` bucket ($1,256). That bucket was deleted on 2026-09-25. The spend
  can't be split back into days, because ClickHouse was wiped to fix the timestamps.
- **~09-16 → 09-23:** the proxy sent empty Langfuse keys, got 401s, and nothing was
  ingested.

The snapshot now exits non-zero on future-dated days rather than writing them.

Practical consequences:

- **Check the job is green** (`launchctl list | grep langfuse-cost-snapshot`,
  second column `0`) before trusting any cost figure. A red job means the window
  you can still recover is shrinking daily.
- **Snapshot before deliberately recreating the stack** if the current window
  matters — once the volumes are gone there is no second copy.
- `LANGFUSE_COST_LOOKBACK_DAYS` defaults to **14**, deliberately wider than the
  10-day TTL, so a run re-aggregates everything ClickHouse still has and days
  already in Postgres are left untouched.

## ClickHouse TTL (the retention mechanism)

10-day row-level TTL on the two v4 event tables and the three v3 tables. The task
skips any table that doesn't exist, so it also works on a stack still on v3:

```sql
ALTER TABLE events_core  MODIFY TTL toDateTime(start_time) + INTERVAL 10 DAY;
ALTER TABLE events_full  MODIFY TTL toDateTime(start_time) + INTERVAL 10 DAY;
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
- `events_core` has a full-text index, so any `ALTER` on it fails with
  `SUPPORT_IS_DISABLED` unless the session sets `enable_full_text_index=1`. The
  task passes it as a client flag.
- The task authenticates with the container's own `CLICKHOUSE_USER` /
  `CLICKHOUSE_PASSWORD`, so the password never leaves the container.
- ClickHouse default `materialize_ttl_after_modify=1` immediately mutates
  existing parts, so old rows are purged on apply (not only on future merges).
- Tables are partitioned monthly (`toYYYYMM(...)`), so a 10-day window is
  enforced by **row-level** TTL merges rather than whole-part drops — fine at
  our scale (~20k rows/50 days reclaimed traces 2.5 GiB → ~780 MiB).

## ⚠️ Re-apply after every Langfuse upgrade or ClickHouse wipe

`task langfuse:update` can run schema migrations that **recreate** a table and
silently drop the custom TTL (Langfuse doesn't know about it). Wiping the
ClickHouse volume does the same: Langfuse's migrations rebuild the tables
without it. The 2026-09-24 wipe left every table with no TTL until 09-25. After
either, run `task langfuse:retention` again. Verify with:

```bash
docker exec langfuse-clickhouse sh -c 'clickhouse-client --user "$CLICKHOUSE_USER" \
  --password "$CLICKHOUSE_PASSWORD" -q "SELECT name, extract(create_table_query, '"'"'TTL [^S]*'"'"')
  FROM system.tables WHERE database='"'"'default'"'"' AND name LIKE '"'"'events_%'"'"'"'
```

ClickHouse is pinned to `25.12` in both compose files. Check timestamps before
moving it to 26.x: that release produced the 2105 dates above.

## Cost history is preserved separately

The TTL deletes per-trace cost too. To keep long-term spend history (the
daily-cost-by-model view) past the 10-day window, a separate snapshot job
aggregates `events_core` into a durable table before it ages out — see
[cost-snapshot](#cost-snapshot) below.

<a id="cost-snapshot"></a>
## Cost snapshot

Spend history is the one thing worth keeping past the trace TTL. Langfuse's own
cost view is computed live from `events_core`, so once that table is pruned the
native UI only ever shows the last 10 days. The snapshot job rolls cost/usage up
into Postgres so the history survives indefinitely.

**Pipeline** (`scripts/langfuse-cost-snapshot.py`, stdlib only):
- Aggregates ClickHouse `events_core FINAL` (type `GENERATION`) by `(day, project, model)`
  over the last `LANGFUSE_COST_LOOKBACK_DAYS` (default 14, > the 10-day TTL).
  `FINAL` matters: the table is a `ReplacingMergeTree`, so without it an
  unmerged row version is counted twice.
- Exits non-zero, writing nothing, if any day is later than tomorrow.
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
