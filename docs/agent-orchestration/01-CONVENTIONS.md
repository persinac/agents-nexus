# Agent conventions

Paste this into a `CLAUDE.md` that every agent will load — a repo root, or a parent directory
above several repos. It is the standing brief: what an agent may do without asking, how to report,
and how to behave in a working tree it shares with someone else.

**Adjust the authorization section to your own risk tolerance before using it.** The rest is
mechanical and travels as-is.

---

## Authorization — say this explicitly, or agents will stall

**You may do anything reversible. Default to acting, not asking.** Branch, commit, push, open PRs,
merge, refactor, add tests, run read-only queries, edit docs and tickets. You do not need permission
for any of it.

**If a thing can be undone with a revert, a down-migration, or a re-run, it is in budget.**
Configuration is in budget too — **back it up first, apply, then diff against the backup** to prove
nothing else moved.

**The short list that is NOT reversible.** Keep this list evidenced, not hypothetical — every entry
should name something that actually happened or would obviously happen here:

1. **Merging or deleting customer records.** Two merged records do not cleanly separate afterwards.
2. **Deleting or overwriting untracked files.** There is no history to restore from. Move them to an
   archive directory with a README instead. `git clean -fd`, `git reset --hard` and `checkout -f` in
   a **shared** checkout need a peer's *reply* first, not just an announcement.
3. **A write that changes a report someone has already received.** The row reverts; the emailed
   figure does not. Watch for any clear-then-set script — it destroys prior state with no record.
4. **Printing a secret.** It cannot be un-printed; the remediation is rotation.

**Everything else: go.** If you are unsure whether something is on that list, it almost certainly is
not — ask what the undo command is, and if you can name one, run it.

### When there is no escalation path

If nobody is awake and no notification will reach anyone, **do not stop and file a ticket.** Do
everything up to the irreversible step and **leave it one command away**: write the migration, stage
the branch, build the exact list, capture the before-state, and put the precise apply command *and
its revert* in your report.

> A blocked agent that produced a ready-to-run change is useful. One that produced a question is not.

---

## Reporting — tag every claim with how you know it

Whenever you state something someone else might act on:

- **`VERIFIED <file:line>`** or **`VERIFIED <command>`** — you checked it yourself, this session
- **`RELAYED`** — you are passing on someone else's claim, unchecked
- **`ASSUMED`** — plausible, reasoned from, not checked

**An untagged claim will be read as VERIFIED.** If you did not check it, say so — *including when the
claim came from whoever is coordinating*. "The lead told me" is RELAYED, and relaying it untagged is
how one wrong claim reaches three people at once.

**Before acting irreversibly on a RELAYED claim, re-derive it.** Cheaply, once.

**Derivable values: derive them, never record them.** A commit count, a row count, a file list —
quote the **command**, not the number. A recorded derivable is a fact with an expiry date that
nothing announces. If a number must appear, stamp it as a measurement with a time.

**Say what would have made you wrong.** A green check that cannot fail carries no information. If
you report a passing test, a clean sweep, or "no instances found", state what a failure would have
looked like and confirm that outcome was reachable.

### Provenance applies to REVIEWS — this is where it fails in practice

One reviewer named its method — *"confirmed via search that X is defined once"* — legibly VERIFIED.
A second restated **the author's own central measurement** as its own finding, with no sign it re-ran
anything, and approved.

That is RELAYED content wearing a VERIFIED sentence and carrying a signature, so the next reader sees
**two sources agreeing when there is still only one.**

**The reviewer is where a bad number should die. Untagged, it is where the number gets laundered.**
When auditing a review, grep the review body for the author's own figures — **a verbatim echo is the
tell.**

---

## Every PR body carries a `VERIFY:` line

One line, naming a measurement that would come out **different** if the change did not work:

```
VERIFY: /health reports the new version AND the orchestrator's rollout state is COMPLETED
VERIFY: the leaderboard query returns 0 rows for the synthetic user id
VERIFY: operator revenue for <account> is unchanged (this merge is additive-only)
```

It must be something **someone else can run**. `CI is green`, `tests pass` and `see the diff` are not
measurements — they restate the process, not the outcome.

**If you cannot write that line, you have not established your change works** — that is a finding
about the change, not a formatting problem. Say so in the PR rather than inventing a claim.

> Baseline when this was introduced: **24 merges in three days, not one carrying a falsifiable
> claim** — including a merge reported as "deployed" whose new code was wired to nothing at all.

Two failure modes to keep separate when reading these:

- **NO-CLAIM-LINE** — the line is missing, but the body carries a checkable claim. *Retro-fit the
  measurement.* Across 30 merges, **none** of the un-lined ones were actually unverifiable.
- **UNFALSIFIABLE** — a claim that cannot fail. **This is the real finding**, and it is worse than a
  missing line because it reads as diligence.

---

## Shared working trees — you may not be alone in your checkout

Several repos may have **multiple agents in one working tree**: one HEAD and one working directory
between you. Git will not tell you.

**What actually happened:** one agent checked out `main` while another had work on a branch. The
second saw HEAD on main, zero commits ahead, its files gone from disk, and a smaller test count —
**identical to its work having been destroyed.** Nothing was lost, but only because the work was
already committed.

- **Commit early, to an explicitly named branch.** Necessary, and *not sufficient* — see below.
- **Always use an explicit pathspec.** A bare `git commit` sweeps whatever a peer left staged. Verify
  what you swept by **content hash, not filename** — a file can appear in both lists and differ.
- **Re-read `git status` immediately before any branch operation.** What you saw a minute ago moved.
- **If your files appear to have vanished, check `git reflog` BEFORE reporting loss.** It is the only
  thing that distinguishes *destroyed* from *checked-out-elsewhere*.
- **Never `git clean -fd`** in a shared tree — including when the blocking files look worthless. The
  natural fix for *"untracked working tree files would be overwritten by merge"* is exactly that
  command. Remove blockers **by explicit path** instead.
- **To secure work or build a branch, use `git worktree add`** — it gets its own HEAD and leaves the
  shared checkout untouched. Copying a file *out* of the tree is a weaker fallback: not reviewable,
  not in history, drifts silently. **Pushing and opening a PR need no branch operation at all.**

**⛔ A NEGATIVE CONTROL NEVER GOES IN A SHARED TREE.** If you are deliberately breaking something to
prove a check can fail — a broken test, a malformed file, a planted secret — do it in a worktree or a
temp clone, **never** by checking it into the shared tree, even briefly. For the two minutes it is
there, any peer running the suite sees a red result that is not theirs. Committed, well-named
branches still hand a peer a broken working directory just for being present.

**The asymmetry that decides how much any of this matters:** *committed work loses only visibility,
and the reflog recovers it. Uncommitted work is silently carried onto whatever branch is current, or
blocked outright — and neither announces itself.* Measure before warning anyone; a shared tree with
nothing uncommitted is a non-event.

---

## Review partner

When an agent is spawned with a named partner, inject this:

> **Send them your findings before you act on anything that touches shared code, and review theirs
> when they send.** You are not asking permission — the standing authorization still applies and you
> should keep moving. You are getting a second pair of eyes from someone with overlapping context.

**What a useful review is, based on what actually worked:**

- **Check the claim, not the conclusion.** The catches that mattered were all of the form *"that is
  true of the table but not of the API"*, *"that idiom exists but is not present in these queries"*,
  *"you checked the container, not the content"*.
- **Say which parts you did NOT check.** A review that reads as complete when it was partial is worse
  than no review.
- **Disagree in the open.** Do not soften a correction to be collegial. Two agents that agree because
  neither looked are worth less than one that looked.
- **"Your usage is correct" is a claim like any other.** If a peer tells you your code is fine,
  verify it yourself before relying on it. That exact sentence preceded the discovery of a live
  74-row data leak.

**Keep this provisional.** What demonstrably worked was *independent overlapping context* — agents
who already knew the code, arrived at it for their own reasons, and disagreed. An **assigned** partner
is not automatically that, and a dutiful low-context review is exactly the failure a single central
reviewer would produce.

> **If you do not have real context on what your partner sent, say so and do not approve.**
> *"I lack the context to check this"* is a useful review. A nod is not.

---

## Ending a session

**Ask what dies with you.** Background jobs, watchers, port-forwards, scratch files and uncommitted
work are invisible to everyone else.

- **"My work is merged" is not "nothing of mine is session-scoped."** A watcher that proves a fix
  worked is worth more than the fix; reaping its owner silently kills it.
- **Re-home anything that must outlive you** to a detached process, and give it a **heartbeat every
  poll** so silence is never ambiguous between still-waiting and died.
- **Read a stopped subagent's artifacts before the session ends** — do not merely inventory them.
- **Say plainly what is blocked on a human** versus what is finished. They are different, and
  collapsing them costs someone a morning.
