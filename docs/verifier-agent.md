# The verifier agent — spec

**Status:** proposed, 2026-09-07. Not built.

## The gap it fills

On the overnight run of 2026-09-07, seven agents produced eight cards, one merge and two
PRs. Peer *review* happened constantly and caught most of the real defects. **Verification
that a merged change did what it claimed happened exactly once** — one agent checked ECS
`rolloutState` after its own deploy. Nobody checked anyone else's.

That is the missing station. Review reads the diff; **the verifier measures the world.**

It runs **after merge, never before.** This is deliberate and matches the fleet's stated
posture: merge freely, nothing is permanent, check the outcome. A pre-merge gate would slow
the thing that is already working.

## What it owns

**One question: did the change do what its author said it would?**

Three checks, in order of increasing value:

1. **Did it deploy?** Version flip via `/health`, ECS `rolloutState=COMPLETED`, Amplify job
   status. Cheap, mechanical, and the fleet convention already says to check `rolloutState`
   rather than trusting a health poll.
2. **Did the predicted effect occur?** The novel part. When a PR says *"excludes synthetic
   plays"*, run the query and confirm the count moved as predicted. When it says *"numbers
   will move"*, capture before/after and report the delta. When it says *"no production
   behaviour change"*, prove behaviour did not change.
3. **Is the claim still true a day later?** Re-run the same check on a schedule. A predicate
   that worked at merge and silently stopped matching is the shape this fleet keeps hitting.

## What makes check 2 possible: a falsifiable claim

The verifier needs something to check against, so PRs and merge reports must carry one line:

```
VERIFY: <a measurement that would be different if this change did not work>
```

Real examples, all from the 2026-09-07 run:

- `VERIFY: /health reports 0.1.463 and ECS rolloutState=COMPLETED`
- `VERIFY: SELECT count(*) … WHERE user_id = 0 returns 0 rows on the leaderboard query`
- `VERIFY: operator revenue for location 4 is unchanged (this merge is additive-only)`
- `VERIFY: cta_click appears in pinball.storefront_event within 24h of a real click`

**A PR whose author cannot write that line has not established that their change works** —
which is itself the finding. The verifier reports "unfalsifiable claim" as a result, not as
an error.

## What it does NOT own

- **Not a merge gate.** It has no veto and blocks nothing.
- **Not code review.** That is the paired-partner mechanism; the verifier never reads a diff
  to judge it.
- **Not incident response.** It reports drift; it does not fix.
- **Not a test runner.** CI owns that. The verifier's whole point is checking things CI
  structurally cannot see — production state, real data, effects across service boundaries.

## Why it is not just "more review"

The defects it would have caught on 2026-09-07 are ones no reviewer could have:

- A merge described as *"deployed"* that was **wired to nothing** — every predicate landed
  and no query called any of them. The diff was correct. The effect was zero.
- **`cta_click` at 0 of 47 impressions across 11 days.** No diff shows that; only counting
  does.
- Revenue reports correct **by accident of an INNER join**, with no rule stating it. Correct
  today, silently wrong the day someone inserts a row.
- A test that could only ever green-skip. It passed review. It never ran.

Each was found by luck. The verifier makes them routine.

## Shape

- **Spawn:** one agent, workspace `verify`, cwd `agents-nexus` (it is fleet-wide, not
  per-repo). Read-only Postgres via the `shared-postgres/prd` Grafana role, `aws --profile
  flashback` for ECS, `gh` for merge events.
- **Trigger:** on merge to `main` in any fleet repo, plus a daily re-run of the last 7 days
  of `VERIFY:` lines.
- **Reports to:** `alex-nexus/orchestrator/orchestrator`, and posts the result as a comment
  on the originating PR.
- **Never writes** to any production system. Its authorization is strictly read-only, which
  is narrower than the fleet default and deliberately so — a verifier that can fix things
  will start fixing things instead of reporting them.

## Output contract

Three outcomes, and the middle one is the valuable one:

| Result | Meaning |
|---|---|
| `CONFIRMED` | The `VERIFY:` measurement was run and matched. |
| `UNFALSIFIABLE` | No `VERIFY:` line, or one that cannot fail. **This is a finding.** |
| `DRIFTED` | It matched at merge and does not match now. |

## Open questions

- Does it verify every merge, or only ones touching data/production paths? Start with
  everything; the volume is low.
- Daily re-run window — 7 days is a guess.
- Should `UNFALSIFIABLE` auto-file a card, or accumulate into a weekly digest? Probably the
  latter, or it becomes noise.

---

## Corrections from the first run (2026-09-07)

**`UNFALSIFIABLE` was conflating two different things.** A PR with no `VERIFY:` line may
still be perfectly verifiable from its prose — that label marked a *missing line*, not an
unverifiable change. Split into `NO-CLAIM-LINE` (retro-fit the measurement from the body)
and `UNFALSIFIABLE` (a claim that cannot fail — the real finding, and worse than a missing
one because it reads as diligence).

**Verify at the merge commit, not at `HEAD`.** Checking `management-api#112` at `HEAD`
showed `patron.py` referencing the constant; at `dd5efba` it does not. `HEAD` was a feature
branch. Use `git grep <pattern> <sha>`.

**⚠️ An ECS task-definition revision is NOT evidence of what shipped.** `management-api:7`
was registered 2026-08-28 and pins the mutable `:latest` tag, so a months-old revision
serves today's image and the revision number never moves. **Use ECR push time plus the
version reported by `/health`.** Any deploy check keyed on taskdef revision is measuring
nothing — the same shape as everything else this fleet keeps finding: a signal that cannot
change is not a signal.

**"Wired to nothing" is not automatically a defect.** `#112` was deliberate and said so four
times, with a guard enforcing it over exactly the four DB-querying entry points. The
verifier's job is to state *what changed in production* — here, the image version and
nothing else — not to infer intent from an absence.
