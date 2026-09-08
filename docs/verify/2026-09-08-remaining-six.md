# Verifier run — 2026-09-08 — the six deferred merges

Closes the list carried over from `2026-09-07-method-and-handoff.md` §4d. Method and the
nine rules live in that file; this records only results.

Read-only throughout. **None of the six carried a `VERIFY:` line** — so all six are
`UNFALSIFIABLE` by the script's criterion. All six nonetheless carried a checkable claim in
prose, and all six were checked. That gap between "no VERIFY line" and "unverifiable" is now
the single most consistent finding across 30 merges.

---

## storefront-api#27 — `1e2ad0dae4` — **CONFIRMED**, claim is exact

Titled as a docs correction but touches `app/core/auth.py`, which is why it was worth a look.

**It is genuinely docs-only.** `git show 1e2ad0dae4 -- app/core/auth.py` changes only
docstring prose (8 lines, all inside the docstring); zero executable lines.

Its substantive claim — *"Guards four endpoint modules: `user`, `notifications`,
`promo_nudges` and `storefront_events`"* — is **exact**.
`git grep -l -a get_current_user 1e2ad0dae4 -- app/api/v1/endpoints/` returns exactly
`notifications.py promo_nudges.py storefront_events.py user.py`. Listing *all* matching
modules rather than checking the four named is the control: a fifth guarded module, or a
named module that wasn't guarded, would both have shown.

**Needed nothing** — no integration test, no real identity, no browser. Reported as such to
`ui-integration-tests`, since a "needed nothing" stops them building coverage for a path
that was never in doubt.

## infrastructure#107 — `574de9584b` — **CONFIRMED, with a live caveat**

Claim: both kiosk PIN rules ran `execErrState: Alerting` with `for: 0s`, so a transient
datasource error fired a security-worded page instantly and with no values.

**The debounce shipped.** Deployed ConfigMap `grafana-alerting-kiosk-rules` now has
`for: 5m` on both rules ("Kiosk bad-PIN burst at location", "Kiosk device hit PIN rate
limit").

**But `execErrState: Alerting` is still set on both.** So the failure the PR title names —
*"an exec error was paging as a security event"* — is **narrowed, not removed**: a datasource
error that persists past 5 minutes still pages as *"Possible PIN guessing… revoke the tablet
if hostile."* Whether that is acceptable is a judgment call, but the exec-error path is still
armed and the PR reads as though it was closed.

## flashback-cns#214 — `dea36af662` — **deploy CONFIRMED, premise only half-checkable by me**

`payment-reconciler` deployment is live in namespace `flashback-fleet`, **1/1**, age 2d21h,
image `flashback-cns:0.1.246`.

**What I could not check, and why it is a real limit rather than an omission:** the reconciler
compares *Stripe paid sessions* against *issued grants*. I have the grants side only —
`pinball.token_grant` shows 170 `source_type='purchase'` grants across 168 distinct
checkouts, 1 since the merge. **The Stripe side is not in Postgres**, so "paid but no tokens"
is not derivable from my access. I did not verify the premise; I verified the workload runs.

## flashback-cns#215 — `06804fccc8` — **CONFIRMED**

Claim: the gauge shipped as bare `unit="cent"` and landed as
`payments_unfulfilled_amount_cents_cent`; the fix is `{cent}` annotation form, which the
collector drops.

`unit="{cent}"` is present at `reconciler.py:315`. **The negative control fired a false
positive worth recording:** counting all `unit="..."` occurrences showed a *still-bare*
`unit="cent"` — which on inspection is docstring prose at line 296 explaining the bug, not a
declaration. Exactly one cent-unit metric declaration exists and it uses the annotation form.
`unit="s"` ×2 is deliberate per the PR.

## flashback-cns#216 — `9706ba29e4` — **CONFIRMED** (deploy and effect)

Claim: per-machine relay silence is now schedule-aware, because the location-level
`location_kind_all_expected_dark` reads 0 where only one machine of six powers down.

**The per-entity metric is live.** Deployed ConfigMap `grafana-alerting-cns-health-check`
references `location_entity_expected_dark` alongside the location-level metric, with an
`unless on (location…)` join.

**RESOLVED — effect CONFIRMED against live Prometheus.**
`max_over_time(location_entity_expected_dark[7d])` → 68 series (20 `bridges`, 48 `machines`),
**18 nonzero**, including `machines / brigid-s-bottlehouse-4-4 / 15` — **the PacMan case the
PR was written for** — plus both Fenix relays and four alex-garage machines. By contrast
`max_over_time(location_kind_all_expected_dark[7d])` is 1 only at fenix and 0 everywhere
else, which is precisely the gap #216 exists to close. It is closed.

**Two corrections, both against myself, and the second is the one worth keeping:**

1. The killed subagent's lead — *"the per-entity machine series never oscillates"* — is
   **REFUTED**. Filing it as a hypothesis rather than a finding was the right call; as a
   finding it would have been wrong in the permanent record.
2. **My own first read was also wrong, and would have published.** I capped output at 8 rows;
   all 8 happened to be `kind="bridges"`, all zero except Fenix — which reads cleanly as
   "the per-entity metric only fires where the old location-level one already did, so
   Brigid's is uncovered." The `machines` kind holds **48 of the 68 series** and sat entirely
   below the cut. **A truncated result set is not a sample.** Check cardinality and the label
   dimensions before reading a pattern into the first N rows — a `head`-shaped limit is a
   sampling decision disguised as a display decision.

## flashback-cns#217 — `a1544159d7` — **CONFIRMED**

Claim: v1 roster shape deleted; a non-v2 location refuses rather than falling back.

`git grep -c -a -iE "wire_v.*1|build_v1|roster_v1|_v1\(" a1544159d7 -- services/` returns
**nothing**. Control: the equivalent v2 pattern returns a hit (`services/ota/s3.py`), so the
grep is capable of matching — the zero is a real absence, not a broken pattern. The control
is narrow, and I note that rather than dressing it up.

---

## Deploy status for all four cns merges — one measurement covers them

`payment-reconciler` and `location-hours-exporter` both run image
**`flashback-cns:0.1.246`**. Merge VERSIONs: #214→`0.1.243`, #215→`0.1.244`,
#216→`0.1.245`, #217→`0.1.246`, and `origin/main` is `0.1.246`. **The deployed image is at
main**, so it carries all four. A deployed tag *below* `0.1.246` would have shown which
merges had not landed.

## Summary

| merge | verdict |
|---|---|
| storefront-api#27 | CONFIRMED — docs-only, claim exact, needed nothing |
| infrastructure#107 | CONFIRMED — debounce shipped; `execErrState: Alerting` still armed |
| flashback-cns#214 | deploy CONFIRMED; premise not checkable from Postgres alone |
| flashback-cns#215 | CONFIRMED |
| flashback-cns#216 | **CONFIRMED** — effect measured; per-entity series fires incl. Brigid's machine 15 |
| flashback-cns#217 | CONFIRMED |

**One thing left open:** cns#214's premise needs the Stripe side, which is outside this
station's access. cns#216 was closed by direct measurement (see above). The
infrastructure#107 gap is filed as Trello card 612.
