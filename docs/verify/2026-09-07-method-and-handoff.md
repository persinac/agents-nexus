# Verifier method + handoff — 2026-09-07

**For:** `alex-nexus/integration/tests/ui-integration-tests`
**From:** `alex-nexus/verify/agents-nexus` (verifier station)

Companion to `docs/verify/2026-09-07-first-run.md` (the two deep-dive cases).
This file is the **method** — the steps I ran, in order, per repo — plus the two gaps
that only an integration test can close.

---

## Why you are getting this

I am **read-only in production by mandate**. Two merged changes have a claimed behaviour
that **cannot be verified by observation**, because in both cases the code path has never
executed in production. Observing harder will not fix that. **They need a test that
exercises the path.** That is your station, not mine.

Both are detailed in §4. Read that first if you read nothing else.

---

## 1. Setup (once per session)

**Read-only Postgres.** Never echoes the credential; `doppler run` injects it and `psql`
reads it from the env.

```bash
cat > q.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec doppler run --project shared-postgres --config prd --silent -- bash -c '
  PGPASSWORD="$PINBALL_DB_GRAFANA_RO_PW" psql \
    -h "$PINBALL_DB_HOST" -p "${PINBALL_DB_PORT:-5432}" \
    -U "$PINBALL_DB_GRAFANA_ROLE" -d "$PINBALL_DB_NAME" \
    -v ON_ERROR_STOP=1 -X -A -F"|" --pset=footer=off -f "$0"
' "$1"
EOF
chmod +x q.sh
```

Connects as `grafana_readonly`. `ON_ERROR_STOP=1` matters — a wrong column name must fail
loudly, not return empty. (It did: I wrote `created_at` for `pinball.storefront_event`,
whose column is `occurred_at`, and got an error rather than a silent zero.)

**Other access:** `aws --profile flashback` (account `162756281464`), `gh`, and — I did not
expect this — **`kubectl` reaches the prod cluster** (`fbf-cp-1`/`fbf-cp-2`). Grafana
alerting lives in ConfigMaps in namespace `observability`, not as `PrometheusRule` CRDs
(that CRD is not installed — my first attempt errored).

---

## 2. The order I ran things, and why that order

### Step 1 — worklist
```bash
python3 scripts/verify-merges.py --days 3
```
24 merges. **Treat its `UNFALSIFIABLE` label as "no `VERIFY:` line", not "unverifiable".**
management-api#112 was labelled UNFALSIFIABLE and turned out to be fully verifiable — its
falsifiable claim was in prose. The label is a worklist hint, not a verdict.

### Step 2 — resolve the merge commit, then **check out that commit, not HEAD**
This is the step that nearly produced a wrong answer, so it goes early.

```bash
gh pr view <n> --repo flippin-balls/<repo> --json title,body,mergeCommit,mergedAt,files
git grep -n -a "<symbol>" <mergeSha> -- 'app/'      # NOT a working-tree grep
git log --oneline <mergeSha>..main                   # is main actually at that commit?
```

In management-api the local checkout was on a **feature branch** ahead of `main`, and on
that branch `app/crud/patron.py` **does** reference `_REAL_CUSTOMER_USER_COND` — work that
is not part of the merge under test. Grepping the working tree would have refuted a claim
that is in fact true. **Always `git grep <sha>`.**

Note `-a` on every grep: a single NUL byte makes GNU grep treat a file as binary and print
nothing, which is byte-identical to "no matches".

### Step 3 — did it actually deploy? (**the taskdef will lie to you**)
```bash
aws --profile flashback ecs describe-services --cluster flashback-fleet --services <svc>
aws --profile flashback ecr describe-images --repository-name <svc> --image-ids imageTag=latest
curl -s https://<svc>.flashbackfleet.com/health
```

**The ECS task definition is not evidence of what is deployed.**
`flashback-fleet-management-api:7` was registered **2026-08-28** and pins the mutable tag
`:latest`. Deploys re-pull `:latest` without creating a revision. Use, in increasing
strength: ECR `imagePushedAt` (the fleet dual-tags, so `:latest` also carries the semver)
→ deployment `createdAt` / `rolloutState=COMPLETED` → `/health` version → the container's
own startup log line.

Cluster is `flashback-fleet` (not `flashback-cluster`). Services: `management-api`,
`storefront-api`, `wallet-api`.

### Step 4 — did the predicted effect occur? (the part that is actually worth doing)
Postgres for schema/data claims; CloudWatch for "was this code path ever entered".

Log group comes off the **current** taskdef — there are two per service and guessing picks
the dead one:
```bash
aws --profile flashback ecs describe-task-definition --task-definition <td> \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['taskDefinition']; \
    print([c['logConfiguration']['options']['awslogs-group'] for c in d['containerDefinitions']])"
aws --profile flashback logs describe-log-streams --log-group-name <lg> \
  --order-by LastEventTime --descending --max-items 6      # confirm coverage spans the deploy
```

### Step 5 — before concluding from silence, prove the thing would have spoken
Non-negotiable, and it changed two of my conclusions:
- storefront-api: 0 telemetry rows. **Does the app log requests at all?** Yes — uvicorn
  access-logs every one. 4,449 events, 4,441 `GET /health`. So the silence is real.
- wallet-api: 0 lines matching `adopt`. **Does the adoption path log?** Yes — I read the
  literal strings (`identity_adopted:`, `identity_adopt:`, `identity_adopt refused:`), and
  confirmed my grep pattern would match them. Only then did the zero mean anything.

### Step 6 — every measurement gets a negative control
A check that cannot fail carries no information. Each of these was run and **did** fail as
intended, proving the positive result was reachable:

| control | expected | got |
|---|---|---|
| `CAST('{"broken":' AS jsonb)` | error | `invalid input syntax for type json` |
| a column that must not exist | 0 | 0 |
| FKs on `play_transaction` other than `user_id` | ≥1 | 3 found |
| does any repo CI ever go red? | yes | `security` failed on store-front + infrastructure |

---

## 3. Results — 24 merges

**Verified CONFIRMED (11).** management-api#112 (`dd5efba`); storefront-api#28 (`02df50c`,
deploy only); database#49 / #48 / #50 / #47; infrastructure#104 / #106; pinball-db#12;
healthcheck#9; store-front#207 + infrastructure#105 (CI gates run, and can go red).

**Two findings (§4).** wallet-api#39; infrastructure#103.

**Cross-merge result worth keeping.** management-api#112 said `_REAL_VENUE_COND` is interim
and should become `AND NOT l.is_internal` once database#49 lands. **It has landed, and the
swap is safe right now** — both predicates resolve to exactly `{4, 11, 12}`:

```sql
SELECT (SELECT count(*) FROM pinball.location WHERE is_active AND location_type='arcade'),
       (SELECT count(*) FROM pinball.location WHERE NOT is_internal);   -- 3 | 3
```
Internal: `{1 alex-garage, 2 matts-garage, 3 test-garage, 13 fbf-test-venue-2,
14 fbf-test-venue-3}`. Neither author could have checked this — database#49 merged after
management-api#112.

**database#47's migrate gate is NOT a green-skip** (I suspected it was). "Phase 3 — down
then up" runs on branches that touch migrations (`feat/location-is-internal`,
`chore/document-absent-user-fk`) and skips on docs-only branches. Correct conditional
behaviour. Caveat: 9/9 runs are green, so its **failure** path is unexercised.

**Not verified — yours or a later run's:** store-front#203 / #205 / #206 (I did not locate
where store-front deploys; not ECS, and `amplify list-apps` returned nothing),
storefront-api#27 (auth change), flashback-cns#214 / #215 / #216 / #217 (k8s; cluster is
reachable, I ran out of session before checking them), infrastructure#107 (ConfigMap
`grafana-alerting-kiosk-rules` exists; I did not diff its content against the merge).

---

## 4. The two gaps only you can close

### 4a. wallet-api#39 — the adoption path has **never run in production**

`POST /api/v1/users/` (`app/api/v1/endpoints/users.py`, adoption branch calling
`crud_user.adopt_identity`).

Timeline, all UTC, each independently checkable:

| when | what |
|---|---|
| 21:41:08 | PR #39 merged |
| **21:41:41** | the one `origin='adopted'` row written (user 148) |
| **21:42:11** | image `0.7.26` **pushed to ECR** |
| 21:42:13 | ECS deployment created |
| 21:52:53 | `rolloutState=COMPLETED` |

**The adopted row predates the existence of the image containing the code that writes it by
30 seconds.** The deployed path cannot have produced it. Only `app/crud/user.py` and
`app/models/user.py` write `origin='adopted'` — there is no migration or script in the repo
that does — so it was written out-of-band (most likely a local run or hand-rolled SQL).

Confirmed by logs: the new task's log stream (`…aaaca8a889c3`, 2026-09-05T21:43:24 → now,
covering the whole life of the deploy) contains **zero** lines matching `adopt`, and the
code logs on *every* branch — success, refusal, and conflict.

**So the single piece of production evidence that adoption works was not produced by the
deployed code.** The endpoint is unverified in prod.

**What to write:** provision a user via `POST /api/v1/users/` with a verified token whose
email matches an existing wallet but whose `sub` differs. Assert **200** (not 201), that
`pinball.user_identity` gains a row with `origin='adopted'` for the *existing* `user_id`,
and that `identity_adopted:` appears in the logs. Then the refusal path: a `sub` already
bound to a *different* wallet must yield **409 `identity_belongs_to_another_user`** and
write nothing. Table constraints to lean on: `app_write_id` is UNIQUE (265/265 distinct
today), `origin` is `CHECK (origin IN ('backfill','primary','adopted'))`, `user_id` is
`FK → pinball."user"(id) ON DELETE CASCADE`.

⚠️ **This is the one place to be careful.** Adoption repoints a Cognito identity at a wallet
— adjacent to "merging customer accounts", which is on the fleet's non-reversible list. Use
a throwaway user you created in the same test; do **not** exercise it against user 148 or
any real customer.

### 4b. storefront-api#28 — the `meta` jsonb round-trip cannot self-verify

The author said so explicitly: *"Not verified: the jsonb round-trip against real Postgres."*
It still is not, and **waiting will not help** — nothing is writing to the table.

Measured 2026-09-07 ~19:00Z: 60 rows, **0 non-null `meta` all time**, 0 rows since the merge,
latest row `2026-09-06 03:27:52Z` (~27h *before* the merge, so PR 28 did not break it).
Cause: **zero `POST /api/v1/events` in 4,449 log events** — six genuine requests in twelve
hours. `cta_click` is **0 of 60** impressions, all time; the table has only ever held
`impression` rows.

I closed the read-only half: the 6-column INSERT is sufficient (`id`, `occurred_at` are the
only other NOT NULL columns and both default), `meta` is jsonb/nullable/no-default, and
`CAST(<json.dumps output> AS jsonb)` accepts the PR's stated payloads. **The remaining gap
is one real INSERT.**

**What to write:** `POST /api/v1/events` with `surface="community_discord"`,
`event="impression"`, and a `meta` body, then read the row back and assert `meta` survives
as jsonb. This endpoint **always returns 204, even on failure** — the handler swallows every
exception — so *asserting on the response is worthless*. **Assert on the row.**

Watch for: `event` is constrained both app-side (`ALLOWED_EVENTS`) and by a DB
`CHECK (event = ANY (ARRAY['impression','cta_click','dismiss']))` — I verified these match
exactly. `surface` has **no** DB allowlist (only non-empty); the app is the stricter
contract there. `meta` is capped at 2000 serialized chars at the edge — worth a 422 case.
`user_id` is `FK → pinball."user"(id)`, so a bogus user_id fails the insert and vanishes
into a 204.

This is low-risk: additive telemetry, no customer-visible effect, and the table is
demonstrably idle.

---

## 5. Two things to carry into any future verification

1. **`git grep <mergeSha>`, never a working-tree grep.** The tree is routinely ahead of the
   merge you are checking, and the difference flips conclusions.
2. **Silence is only evidence once you have proved the thing would have spoken.** Check that
   the code logs, that your pattern matches the literal string, and that your log stream
   covers the window. Two of my findings depended entirely on getting this right.
