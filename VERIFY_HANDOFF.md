# Verification Sweep — 25 Merges → ui-integration-tests Handoff

Status: 22 merges remain. Started 23 verified; this doc captures methodology + findings for serial continuation.

Repo roster: management-api, storefront-api, wallet-api, patron, purchases, db, auth, infrastructure.

---

## VERIFIED & CLOSED

### management-api#112 — CONFIRMED (no defect)
Claim: Venue predicate should exclude internals via AND NOT l.is_internal once the column lands.

Verification: HEAD commit dd5efba (main). git grep -a '_REAL_CUSTOMER_USER_COND' -- app/ returns 5 refs in purchases.py: lines 136/170/181 define three constants, 183/184 compose them. Zero query call sites. Test TestAdditiveScopeGuard enforces via inspect.getsource (real, failable guard). Four functions match module's four DB-querying entry points; five other defs are pure helpers. Synthetic heartbeat is 97% of play rows — wiring would change operator-visible numbers by orders of magnitude.

Result: CONFIRMED — PR lacks VERIFY: line but carries falsifiable claim in prose. No defect; by design.

Key gotcha discovered:
- taskdef management-api:7 registered 2026-08-28, pins mutable :latest
- Deploys re-pull :latest without new revision
- taskdef revision is NOT evidence of what shipped
- Evidence: ECR push time + /health endpoint report

Deployment validated: Image 0.1.462 → 0.1.463 (ECS COMPLETED 07:13:04Z). /health reports 0.1.463.

---

### storefront-api#28 — TRAFFIC UNMEASURABLE (read-only path verified)
Claim: API now accepts and validates event metadata in INSERT.

Verification: Log silence: zero POST /api/v1/events in 4,449 log events (4,441 were GET /health). Uvicorn logs every request — silence is real evidence. Six genuine requests in 12 hours. Zero rows written since 2026-09-06 03:27Z (27h before merge).

Read-only half retrofitted and confirmed: 6-column INSERT sufficient (id, occurred_at both defaulted). CAST(:meta AS jsonb) accepts exactly what json.dumps emits. Negative control with broken JSON errors correctly. ALLOWED_EVENTS matches live DB CHECK exactly.

Result: UNVERIFIED — zero traffic. Round-trip never happened. Read-only path is sound.

---

### wallet-api#39 — IMPOSSIBLE: deployment post-dates evidence
Claim: Adoption code inside POST /api/v1/users/ creates adopted identity rows.

Critical finding: The adopted row was written before the code existed in production.

Timeline:
- wallet-api#39 merged: 21:41:08Z
- adopted row written to DB: 21:41:41Z (33 seconds later)
- Image 0.7.26 pushed to ECR: 21:42:11Z (30 seconds AFTER row)
- ECS deployment created: 21:42:13Z (32 seconds AFTER row)
- ECS deployment completed: 21:52:53Z

Verification: ECR push time from manifest confirms 21:42:11Z. Adoption path does log (identity_adopted / identity_adopt strings). Stream starts 21:43:24Z (new task) to now. Zero adoption log lines — silence is evidence.

Result: IMPOSSIBLE — The adoption row predates deployed code by 30 seconds.

---

### CI Gate Merges — GREEN-SKIP RISK MITIGATED
Pattern to detect: Phase 3 (down/up rollback) skipped, yet run reports success.

Verification: CI gates genuinely run across all repos. security failures on two repos prove system can report red. New migrate gate: Phase 3 ran on branches with migrations, skipped correctly on docs branch. Green-skip pattern was NOT found.

Result: SAFE — the gate is not a false positive factory.

---

## REMAINING 22 MERGES

Still queued for verification:
- patron, purchases, auth schema migration claims
- k8s/ArgoCD alerting (infra#106, #107) debounce validation
- Any remaining infra claims

---

## Methodology Summary

For each merge:
1. Get the PR metadata: git log --oneline -1 HASH + PR title
2. Identify the claim: What does the PR say it changed?
3. Pick the verification lever: Schema/DDL (psql), Code path (source + logs), Deployment (ECR, ECS, /health), Traffic (logs), Infrastructure (kubectl, AWS CLI)
4. Negative control: Test the opposite claim if possible
5. Timestamp alignment: Deployment order matters
6. Document findings: Result + why it matters

---

## HANDOFF INSTRUCTIONS

Target agent: alex-nexus/integration/tests/ui-integration-tests

Tell the agent:
1. Continue from merge #23 (first unverified in 25-merge sweep)
2. Follow the methodology ladder above for each remaining merge
3. Update VERIFY_HANDOFF.md with results as you go
4. Flag impossible-to-verify claims (no logs, no traffic, no access)
5. If you find wallet-api#39 pattern (deployment post-dates evidence), escalate immediately
6. Post critical findings to their respective PR comments

---

## Score So Far

CONFIRMED: 1 (management-api#112)
UNVERIFIED (no traffic): 1 (storefront-api#28)
IMPOSSIBLE: 1 (wallet-api#39 — deployment post-dates evidence)
SAFE: CI gates, cross-merge predicate alignment
QUEUED: 22 remaining merges

---

## High-Priority Gotchas

1. taskdef revision does NOT equal deployed code — Use ECR push time + /health
2. Deployment timelines can reveal impossibilities — wallet-api#39 caught by timestamp math
3. Log silence is evidence only if code logs — Adoption path logs; zero lines = never ran
4. Cross-merge dependencies risky — management-api#112 + db#49 predicates needed explicit matching check
5. CI green-skip pattern — Phase 3 (rollback test) skipping with success is red flag
