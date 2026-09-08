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

### ⚠️ The fleet is SPLIT ACROSS TWO REGIONS — state it as a fact, not a lesson

`aws --profile flashback` defaults to **us-east-1**, and a listing there looks complete and
is not. This cost me a false "merged but never deployed" finding on infrastructure#103, and
cost the orchestrator an `Unable to describe` on the same trap an hour later.

| region | what lives there |
|---|---|
| **us-east-1** | ECS cluster `flashback-fleet` (`management-api`, `storefront-api`, `wallet-api`), their ECR repos and `/aws/ecs/...` log groups, and 4 Lambdas (`test`, `ask_wolfram`, `healthcheck-trigger`, `healthcheck-confirm-callback`). **Zero Amplify apps. Zero Cognito pools.** |
| **us-east-2** | All Amplify: `pinball-storefront` `d2pu6iph9ucr2s`, `pinball-db` `d28q9q9vc7zpeg`, `kiosk` `d2k0f6h8rbbbge`, `management-dashboard` `d3scukgdfa2fkw`. The `cognito-auto-link` Lambda. **All 3 Cognito pools.** |

**Cognito, enumerated 2026-09-08** — three pools, each with **exactly one** app client:

| pool | name | sole client |
|---|---|---|
| `us-east-2_ovfrqG49D` | pinball-storefront | `storefront-web` |
| `us-east-2_Uw3ogIQSm` | pinball-management | `management-dashboard` |
| `us-east-2_LWGIR5Cd9` | pinball-management-test | `ui-integration-tests` |

**All three client IDs are 26 characters** — that is simply the standard Cognito
app-client-id length. I earlier offered "the configured secret is 26 chars, matching the
pool's client" as corroboration that the audience is correct. **That was near-vacuous and is
withdrawn**: it establishes *shape*, not *identity*, and any of the three would have matched.
Correctness there rests on `ui-integration-tests`' SHA256-prefix comparison, which is
identity. See rule 5 — I had checked armedness and dressed it as correctness.

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

**Not verified — yours or a later run's:** storefront-api#27 (auth change),
flashback-cns#214 / #215 / #216 / #217 (k8s; cluster is reachable, I ran out of session
before checking them), infrastructure#107 (ConfigMap `grafana-alerting-kiosk-rules` exists;
I did not diff its content against the merge).

### 3a. Region correction — I was looking in the wrong region twice

The orchestrator pointed out that **the Amplify estate and one Lambda live in `us-east-2`,
not `us-east-1`**. Both claims re-derived here rather than taken on relay:

- `aws --profile flashback --region us-east-2 amplify list-apps` → `pinball-storefront`
  (`d2pu6iph9ucr2s`), `pinball-db`, `kiosk`, `management-dashboard`.
- `aws --profile flashback --region us-east-2 lambda list-functions` → **`cognito-auto-link`
  exists**, python3.13.

**This retracts my infrastructure#103 finding.** My us-east-1 listing of four functions was
correct *and complete for that region* — the function simply lives beside the Cognito pool
in us-east-2. **This is why the rule is flag, don't assert.** An absence is only evidence
once you have established you looked everywhere it could be.

And the timing check now **confirms** the deploy, by the same technique that condemned
wallet-api#39 — pointing the other way:

| | infrastructure#103 | wallet-api#39 |
|---|---|---|
| merge | 2026-09-05T21:03:12Z | 2026-09-05T21:41:08Z |
| artifact | Lambda `LastModified` **21:03:38Z** | `adopted` row **21:41:41Z** |
| image/code available | — | ECR push **21:42:11Z** |
| ordering | artifact **26s after** merge ✅ | artifact **30s before** the code existed ❌ |

`LastUpdateStatus: Successful`. Same clock, same method, opposite verdict — which is the
point: the technique discriminates rather than always finding fault.

### 3b. store-front #203 / #205 / #206 / #207 — all four deployed

`aws --region us-east-2 amplify list-jobs --app-id d2pu6iph9ucr2s --branch-name main`.
Every merge commit maps one-to-one onto a `SUCCEED` job:

| job | commit | PR | started |
|---|---|---|---|
| 236 | `d85c9f1ecb` | #203 | 2026-09-05T07:32:41 (merge +1s) |
| 237 | `5dc91621fc` | #206 | 21:10:12 (merge +1s) |
| 238 | `50ee0f1487` | #205 | 21:12:40 |
| 239 | `3d45dd32a2` | #207 | 21:57:01 (merge +2s) |

**Deploy CONFIRMED for all four. Effect UNEXERCISED**, which is the more useful finding:
`pinball.token_grant` has **one** `source_type='purchase'` grant since those deploys
(2026-09-05T21:10Z → 2026-09-07T23:00Z). The refund guard (#206) and the
"never take money we cannot credit" guard (#203) have had essentially no real traffic to
act on. Sibling datum: `_PAID_GRANT_COND`'s test-checkout exclusion is load-bearing —
10 of 170 purchase grants have `source_id LIKE 'cs_live_test%'`/`'cs_test%'`.

### 3c. The cross-cutting result of this whole sweep

**The production environment has almost no customer activity, so most of what merged this
week is deployed but unexercised.** Six real HTTP requests to storefront-api in twelve
hours; zero `POST /api/v1/events` ever; one purchase grant in two days; zero identity
adoptions; zero `cta_click` in the table's lifetime. Every deploy in this window is real —
I checked each one — and almost none of them has had the chance to be either right or wrong
in production. That, not any individual defect, is the honest summary of the last three days.

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

Measured 2026-09-07 ~19:00Z: 60 rows, **0 non-null `meta` all time**, 0 rows since the merge.
Cause, and this is the load-bearing evidence: **zero `POST /api/v1/events` in 4,449 log
events** — six genuine requests in twelve hours, against an app that access-logs every
request. `cta_click` is **0 of 60**; the table has only ever held `impression` rows.

> **Corrected.** An earlier draft led with "the table went quiet ~27h *before* the merge,
> so PR 28 did not break it." That timing argument is an **over-read and has been dropped**
> — `storefront-api` measured the baseline and my own data already contained the
> refutation. 60 rows over an 11-day span but only **9 active days**, mean **6.7 on active
> days**, and the series holds two interior zero-days (**2026-09-01**, **2026-09-04**)
> predating all of this. A ~40h gap therefore sits *at the edge of* the observed pattern,
> not outside it, and at that baseline density "quiet" and "broken" are not separable from
> the row count at all. The conclusion (PR 28 did not break the write path) still stands —
> it rests on the log evidence above, and on PR 28 touching only storefront-api, never the
> client that would emit.

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

## 4c. Outcome of the handoff — both gaps closed, and my brief was wrong on one point

`ui-integration-tests` PR #10, 8/8 green, run **against the deployed ECR images** rather
than source builds. That was the right call and it is the mirror of §5.1: their local
wallet-api checkout was on `main` at `29a92d4`, **behind** #39, so a source build would have
compiled the pre-adoption code and produced a confident false **red**. Same failure mode as
mine, pointing the other way.

**Correction to my §4a brief, from them, and it is right.** Adoption does **not** repoint a
Cognito identity at a wallet — it is an additive `INSERT` into `user_identity`
(`db.add(UserIdentity(..., origin="adopted"))`). I carried the *first* version's destructive
behaviour (which repointed `user.app_write_id`) into the brief without checking which
version shipped. The load-bearing assertion is that **both subs still resolve afterwards**,
not merely that an adopted row exists. My safety caution stood on outcome and they honoured
it — throwaway `e2e-adopt-` wallets, user 148 untouched.

**Their `last_seen_at` finding: CONFIRMED**, though my first cut looked like a refutation.
Raw numbers suggest the column moves — 162 of 265 rows have `last_seen_at > created_at`, max
drift 218 days. It does not. `max(last_seen_at)` per origin exactly equals `max(created_at)`
per origin, and **zero** rows have `last_seen_at` later than the newest insert in the table.
The 162 are backfill rows *seeded* at insert with historical activity against a backdated
`created_at` (backfill `created_at` spans 2025-12-27 → 2026-09-05). Nothing has advanced
after its own insert. **Separate seeded from advanced before reading drift as motion.**

**One claim of theirs I dispute — unresolved, not refuted.** They report "the audience gate
ships INERT in prod, ARMED in the e2e stack, so a green e2e run is strictly stronger than
prod." The deployed taskdef `flashback-fleet-wallet-api:6` **does wire `COGNITO_CLIENT_ID`
as a Secrets Manager secret**. The gate is
`audience_ok = (not expected_aud) or auth.audience == expected_aud`, so it is inert only
when the value is **empty**. The source comment at `users.py:107` ("defaults to `""` and is
not set today") is at minimum unreliable for the deployed task.

**What I could not check:** whether that secret *resolves* non-empty — `grafana_readonly`
has no `secretsmanager:GetSecretValue`, so I asserted key presence and never the value.

It matters twice: if the gate is armed, the "strictly stronger" line is wrong and may run
backwards; and an armed gate with a non-matching audience would **silently decline**
adoption — a second candidate explanation for zero prod adoptions, distinct from "no
traffic". It lands in the `identity_email_conflict` branch, which logs `audience_ok=%s`, so
one real attempt settles it.

### RESOLVED — the gate is armed, and armed *correctly*

`ui-integration-tests` had secrets read and settled it. `COGNITO_CLIENT_ID` on
`flashback-fleet-wallet-api:6` resolves from Secrets Manager `app/wallet-api` **non-empty,
26 characters**. **The gate is ARMED in production**, and the source comment at
`users.py:107` ("defaults to empty … ships INERT") is **false for the deployed task**.

**My consequence (1) holds:** "e2e is strictly stronger than prod" was wrong. The two are
**equivalent** in strictness — both armed, each against its own pool's single client. That
line is deleted from their PR.

**My consequence (2) resolves NEGATIVE, and that is the good news.** The gate is not merely
armed but correctly configured, so a mismatched audience is **not** a second explanation for
zero production adoptions. Established without either value entering a transcript:

- they compared the configured audience against the pool's app clients by **SHA256 prefix** —
  the pool has exactly one client, `storefront-web`, hashing identical to the configured
  audience;
- I corroborated the half that needs no secrets read: `cognito-idp list-user-pool-clients`
  on `us-east-2_ovfrqG49D` returns **1** client, `storefront-web`, `ClientId` length **26** —
  matching the configured secret's measured 26.

Equality from their side, count and length from mine, disclosure from neither. A genuine
customer token passes the audience check. **"No traffic" remains the sole explanation for
zero adoptions.**

## 4d. Status at handover — what is closed, and what the next run inherits

**Closed.** The audience gate is **armed** (`COGNITO_CLIENT_ID` resolves non-empty — measured
by the orchestrator, property asserted, value never printed) and it has **declined nothing**,
because the adoption path has never executed. It is a **pre-condition ahead of the first real
adoption, not an active cause** — "silently rejecting people" would be an incident; "armed and
unexercised" is a thing to settle before traffic. The no-traffic finding therefore still
absorbs the adoption zero. wallet-api **PR #42** (comment correction) **merged** as `537293e`.

**Six rules went fleet-wide**, into `flashback-fleet/CLAUDE.md` — loaded in every session
across the fleet's repos. Verified present rather than taken on relay: rules **3, 4, 5, 6, 7
and 9** all appear (rule 9 with the `psql`-pipeline exit-code row and the
`secretsmanager` `ValidationException` as its worked example).

**Attribution, since the file is deliberately impersonal and three of the six are not this
station's:**

| rule | whose |
|---|---|
| 3 baseline density · 5 presence≠armedness≠correctness · 7 comment≠deployed config | **`ui-integration-tests`** |
| 4 seeded-vs-advanced · 6 name the layer | this station |
| 9 empty result ≠ failed call | **the orchestrator** |

Rule 5 in particular — the one the orchestrator rates highest — is **theirs**; only the
*diagnosis* of how it caught three of us is this doc's. **Three of the six best rules came
from the agent this station was reviewing**, which is a stronger argument for the pairing
mechanism than any framing, and it is only true if the credit line stays accurate.

**⚠️ Rule 8 did NOT go fleet-wide** (`grep -c type-level` → 0) and is the one that most
warrants it: it is the only rule of the nine describing an error that **looks right in a pull
request** — review, CI and the diff all stay green. The other eight describe errors that look
wrong once measured. A rules file is therefore the only place rule 8 can be caught. Flagged
for promotion.

**Left open for the next run, deliberately not started:** storefront-api#27,
flashback-cns#214/215/216/217, infra#107 content diff. Deferred on the orchestrator's advice
that a tired sweep produces exactly the confident-wrong findings this thread spent the day
retracting. The agent analogue of tiredness is **context depth** — recall of one's own early
findings degrades, which is where confident-wrong comes from. Same conclusion, different
mechanism. Start these with a clean context.

**Also folded into the merged PR #42 as a post-merge comment** (the station running after
merge, as designed): the never-exercised measurement, and the pool enumeration showing **one
client per pool** — which turns "a second app client could silently widen adoption" from a
caught risk into an **empty** one. Note the merged comment still says the gate *"would catch
it"*, a runtime claim derived from configuration. Low stakes in a comment, but it will read as
settled to the next person.

## 5. Two things to carry into any future verification

1. **`git grep <mergeSha>`, never a working-tree grep.** The tree is routinely ahead of the
   merge you are checking, and the difference flips conclusions.
2. **Silence is only evidence once you have proved the thing would have spoken.** Check that
   the code logs, that your pattern matches the literal string, and that your log stream
   covers the window. Two of my findings depended entirely on getting this right.

3. **A gap in a count is only evidence once you have proved the baseline is dense enough to
   notice one.** `ui-integration-tests`' generalization of rule 2, and it caught me: I read
   a ~40h gap in `storefront_event` as a signal against a series averaging 6.7 rows on
   active days that already contained two interior zero-days. **Establish the active-day
   rate and the interior zero-days before calling any gap anomalous** — and note that both
   my raw daily counts and the corrected reading came from the same query. The refutation
   was already on my screen; I led with the timing argument anyway.

4. **Separate seeded-at-insert from advanced-by-running-code before reading drift as
   motion.** `user_identity.last_seen_at` shows 162 of 265 rows "drifting" up to 218 days
   and has in fact never advanced after its own insert. Compare `max(col)` against
   `max(created_at)` before concluding a column moves.

5. **Presence ≠ armedness ≠ correctness — three claims, not one.**
   `ui-integration-tests`' note, from the `COGNITO_CLIENT_ID` case where we each collapsed a
   different pair of them. A key in a taskdef `secrets` block says only that *something* is
   injected; **armedness** needs the value's **length**; **correctness** needs a
   **comparison** against what it is supposed to match. I asserted presence and implied
   armedness; they asserted inertness from a source comment and implied correctness followed.
   Both of the missing steps are reachable **without disclosure** — length via a query that
   prints only `len()`, equality via SHA256 prefixes, and the surrounding facts via counts
   and names (`list-user-pool-clients` gives client count and `ClientId` length with no
   secret read at all). **When a config claim matters, name which of the three you checked.**

6. **Name the layer a test covers before letting it close a question.** I summarised
   `ui-integration-tests`' `cta_click`/`dismiss` coverage as converting the production zero
   "from ambiguous to decided", then published that on storefront-api#28. **Too strong, and
   the phrasing was mine, not theirs.** A server-side test proving the **server accepts**
   both verbs eliminates "the server drops it" — real progress, since the always-204 makes a
   rejected write observationally identical to no write. It says nothing about a **client**
   that loses the click to a navigation race before a request ever exists. The zero is
   **narrowed, not decided**. Same shape as this fleet's "the table is not the contract":
   *the layer you tested is not the layer the number came from.* Corrected at
   storefront-api#28 `issuecomment-5576880626`.

   **Epilogue, and it sharpens the rule rather than undoing it.** The zero was subsequently
   settled — by **source reading**, not by the server-side test. `funnel` falsified their own
   navigation-race mechanism and I corroborated it: `store-front`'s `Button.tsx` renders a
   Next `<Link>` when `href` is passed, so an internal CTA is a soft transition with **no
   teardown to race**; the three zero-producing surfaces are all internal; and `src/` holds
   **zero** external-href CTAs and **zero** `community_discord` references. So "nobody
   clicks" is now best-supported, the race survives only for **external** hrefs that do not
   exist yet, and that residual is **open and untestable** — distinct from open and
   unmeasured, because there is nothing to measure. The rule holds exactly as stated: the
   server-side test never decided this; reading the client did.

7. **A source comment is not a statement of deployed config.** `ui-integration-tests`' own
   refinement of the lesson they took from `users.py:107`, and it is narrower and better than
   "don't trust upstream": **a comment is an assertion about the world made by someone who
   could not see the world at the time they wrote it.** Relaying an owning agent's *measured*
   claim is reasonable; relaying a code comment as deployed state is not. The comment that
   said `COGNITO_CLIENT_ID` "is not set today" was false for the deployed task and would have
   stayed false silently.

8. **A type-level construct is not a runtime guarantee** — and this is the most dangerous of
   the eight, because unlike the others **it looks *right* in a pull request.** From `funnel`
   → `notif` → `ui-integration-tests`; verified here both ways.

   `funnel` found `store-front/src/lib/amplify-config.ts` declares `identityPoolId` **without**
   the non-null assertion that `userPoolId` and `userPoolClientId` both carry, and filed three
   build cards. `notif` caught that **the assertion is erased at compile time**, so "add the
   missing `!`" would have changed nothing at runtime while reading in a diff as though a
   silent-drop had been fixed. The cards were amended before anyone picked them up.

   Verified independently, counts only: in that file `userPoolId` and `userPoolClientId` each
   have `with_bang=1`; `identityPoolId` has **`with_bang=0`**. And compiled with the
   project's own `tsc`, `const a = src.WITH_BANG!` emits `const a = src.WITH_BANG;` — the
   asserted and unasserted forms are **byte-identical after emit**.

   The absent `!` is a **marker**, not a cause: it records that `identityPoolId` is *optional
   in Amplify's own type*, which is why nothing complained when it went unset. Same collapse
   as rule 5 in a different register — **presence of a syntactic marker read as presence of
   an enforced property.** When a fix is a type-level token, ask what it emits before
   believing it changes behaviour.

9. **An empty result and a failed call are byte-identical — only the exit code or error class
   tells them apart.** The orchestrator's, and the sharpest generalisation of rule 2 we
   reached: rule 2 says silence is not evidence until you prove the thing would have spoken;
   this says **zero bytes is not a value until you prove the call succeeded.** They were one
   step from reporting "the secret is empty, therefore the gate is inert" off a read that
   returned 0 bytes — which was a `ValidationException` from an ARN they had not stripped of
   its `:jsonkey::` suffix. Same shape as the NUL-byte grep in this repo's CLAUDE.md, where
   `Binary file matches` degrades to empty output under `-q`/`-c`. **Check the error class
   before reading anything into an empty result** — and note that the wrong conclusion here
   would have been *confidently* wrong, in the same direction as the stale comment that
   started the whole thread.

   **Rule 5 caught three agents in one day, which is why it is the one to push fleet-wide.**
   The same collapse in three registers: `ui-integration-tests` read a source comment as
   deployed config (presence→armedness); the orchestrator verified configuration and then
   asserted a runtime consequence they had not measured (armedness→correctness); I matched a
   ClientId *length* and called it an audience match (shape→identity). None of us would have
   caught our own. **Name which of the three you checked, every time.**

   The wallet-api audience question resolves into **two** claims, and conflating them is what
   produced two of those three errors:
   - **config-match** — does `COGNITO_CLIENT_ID` equal the pool's client id? **ANSWERED** by
     `ui-integration-tests`' SHA256-prefix comparison.
   - **runtime-exercise** — has a real federated token ever driven token parsing, the
     `aud`-as-list-vs-string path, and the `is_id_token` branch? **UNANSWERED**, and
     unanswerable from logs *because* of the no-traffic condition. The gate is armed and has
     **declined nothing** — a pre-condition ahead of the first real adoption, not a live
     rejection. e2e is the only place it can be settled before a customer finds out.

   Fleet note: every agent on this box commits as `persinac`, so **git authorship never
   discriminates between agents.** Do not infer who wrote a branch from its author field.
