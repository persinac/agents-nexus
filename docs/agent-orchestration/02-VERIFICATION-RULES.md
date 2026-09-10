# Verification rules for agent work

Drop this into a repo (or a parent directory) as `CLAUDE.md`, or paste it into an existing one.
Nothing here is specific to a company, a stack, or a cloud — every rule below was earned by a
real check that returned a **real answer to a question nobody meant to ask**.

Use it as a checklist for the moment *before* you report a finding, not as reading material.

---

## ⛔ The core rule: a check is only evidence about the question it actually asked

**And tools do not tell you when they silently narrowed it.** Every row below looked like a clean
result. Most were produced by careful people who had already decided to be careful.

| The check | What it looked like | What it actually said |
|---|---|---|
| `git diff <sha> -- <untracked-path>` | "no local additions" | untracked files are **invisible** to diff — it also produced a *backup* that omitted everything it was made to preserve |
| `grep` on a file containing one NUL byte | "no matches" | GNU grep switched to binary mode and printed nothing. Use `grep -a` |
| `git apply --check` inside a dirty tree | "the patch does not apply" | the layer was **already applied there**. Test against a clean extraction |
| four `200`s in a row from an API | "the workaround works" | it was answering a different call than the one that mattered |
| `pytest` with a required env var exported to a dummy | "2 tests failing" | the export **defeated the premise** of a test asserting behaviour when that var is *unset* |
| `some-cmd 2>&1 \| tail` | exit code **0** | a pipeline returns the **last** command's status; the real command had *failed* |
| `<secrets-read> \| wc -c` → `0` | "the secret is empty" | a validation error from a bad identifier. **Zero bytes is byte-identical to a failed call** |
| `grep -c` on a minified JSON document | "1 occurrence" | `-c` counts *lines*, and the document is **one line**. The real count was 9 |
| `npm run typecheck` → exit 0 | "the tree type-checks" | it compiled **zero files**. Exit 0 before *and* after the fix; what moved was **files compiled, 0 → 20**, with two real errors hiding behind that green |
| a CLI edit printing a deprecation notice | "a warning, it applied" | it was an **abort**. The title and body never updated |

**The one-line test:** *for any green check, ask what result would have looked like failure.*

---

## Habits that catch these

- **Check that the detector can return "fine."** A technique that only ever finds fault is
  indistinguishable from a broken one. A timing comparison earns trust precisely because it
  *cleared* one artifact and *condemned* another on the same clock.
- **Separate "empty" from "failed."** Read the exit code and the error class before reading anything
  into a null result.
- **An absence is evidence only once you have established you looked everywhere it could be.**
  A "never deployed" finding was retracted when the resource turned out to be in a different region.
  **Know where your infrastructure actually is** — split regions are normal and invisible.
- **Prefer the command that refuses.** `git merge --ff-only` on a clean tree either fast-forwards or
  stops; it cannot improvise a merge you did not ask for. Choose instruments that fail loudly over
  instruments that cope.
- **Prefer a measurement that would differ.** When before and after are honestly identical, **say
  so** and supply a *counterfactual* that does move — rather than hunting for a number that flatters
  the change.
- **Form the explanation AFTER the measurement**, not before it and then confirm. A withdrawn finding
  was traced to exactly this: the first explanation was never re-examined once the data arrived, and
  the refuting rows were on screen the whole time. The guard is an ordering, not a checklist item.
- **Re-derive before acting, and re-derive again if time passed.** Ask the authoritative source, not
  a local cache. *"Behind by 0"* is meaningless if the remote ref you compared against is itself
  stale.

---

## Git

- **`git diff` between two refs defaults to the ENDPOINT comparison. `A...B` is the ONLY form that
  asks "what does this branch add".** Do **not** learn this as "three dots not two" — **the bare
  two-argument form is identical to two-dot**, and most people type no dots at all, so that phrasing
  lets a reader who typed neither conclude they are safe.

  | form | result on one branch four commits behind main |
  |---|---|
  | `git diff main branch` (bare) | 25 files, +1292, −237 |
  | `git diff main..branch` | **byte-identical** |
  | `git diff main...branch` | **10 files, +1052, −41** |
  | `git diff $(git merge-base main branch) branch` | identical to three-dot |

  A **2.5× inflation** from one character, on exactly the kind of number that gets quoted into
  tickets. **Name the operative token correctly or the rule gets followed and still fails.**

- **A branch SHA is not a citation. Cite the LANDED commit.** Under **squash** merging a branch's
  commits are not reachable from the default branch afterwards, so every branch SHA in a ticket,
  doc, or code comment becomes a dead reference the moment the PR merges. If your repo permits
  *several* merge methods, you cannot tell from the SHA which was used — which makes the rule
  stronger, not weaker.

- **`grep FILE 2>/dev/null || echo absent` cannot tell no-match from no-file.** The redirect swallows
  *"No such file or directory"*, the `||` fires, and you read a **manufactured absence** as evidence.
  This produced a real finding against a path that had **never existed in the repo's history** — and
  the conclusion happened to be *correct on the stale tree being examined*, so a right answer arrived
  by an invalid method and survived review. `ls` the file first, or drop the error suppression.

- **`git fetch` updates the remote ref; local tooling reads the local tree.** A freshly-fetched repo
  will confidently report "nothing pending" from a migration tool, a lockfile check, or a status
  command, while describing a working tree that has not moved. **Pull before trusting any local
  "pending" count.**

- **Validating a fix against an unvalidated baseline proves nothing — and this is how a "recovery"
  destroys working code.** A tree was reported as *wiped* and a recovery procedure written up.
  Nothing was lost: the true remote was four commits newer than the ref being read, and every
  "destroyed" file was on it. **Behind is not wiped.** The author had even re-verified the patch
  applied cleanly "to the new base" — against a base they never checked was current. **Verify the
  BASELINE from a source outside the local repo before trusting any recovery.**

---

## Shared working trees and shared identity

- **Never `git clean -fd`, `git reset --hard`, or `git checkout -f` in a tree someone else is
  working in.** Branch *switching* is safe — git refuses a switch that would clobber local
  modifications — but those three destroy untracked work with no warning. Remove blocking files **by
  explicit path**, and re-derive the list immediately before acting; collision counts move.
- **Untracked files can be deliberate and load-bearing.** Before "fixing" a broken build in a shared
  tree, find out whether the breakage is the point.
- **To secure work in a shared tree, use `git worktree add` — never a branch checkout.** A checkout
  moves HEAD under your peer. A worktree gets its own HEAD and leaves theirs alone. **Pushing and
  opening a PR need no branch operation at all**, so a shared tree never has to move to ship a
  branch.
- **Negative controls go in a worktree or a temp clone, never the shared tree**, even briefly.
- **The asymmetry that decides how much this matters:** *committed work loses only visibility, and
  the reflog recovers it. Uncommitted work is silently carried onto whatever branch is current, or
  blocked outright — and neither announces itself.* Measure before warning anyone: a tree with
  nothing uncommitted is a non-event.
- **Shared credentials mean identity never discriminates.** If several actors use one API token or
  one git identity, an action attributed to a person is **not evidence a person did it** — and,
  equally, **not evidence one didn't**. Two opposite wrong inferences were drawn from identical
  metadata 40 seconds apart. If it matters, establish it from something else — a timestamp against
  known working hours, a response landing in a known transcript — or **say who you are in the body**,
  because the metadata cannot.

---

## Deploys and rollouts

- **A version endpoint during a rollover is NON-DETERMINISTIC, not merely early.** Old and new
  targets are both registered in the load balancer, so it round-robins. Sampled mid-deploy: **10
  probes returned 9× old and 1× new**, then 12/12 new minutes later. *"I checked and it was still
  the old version"* is not evidence of a stalled deploy, and one hit on the new one is not evidence
  of a finished one. **Sample ~10 and read the distribution; a mixed result is "mid-rollover,
  healthy".**
- **A no-op redeploy and a mid-rollover present identically from a version endpoint alone.** If CI
  force-redeploys regardless of whether the build pushed, a deploy that changed nothing looks the
  same as one in progress. **Registry push timestamps separate them.** The authoritative signal is
  the orchestrator's own rollout state plus the running count.
- **A version file, an image tag, and a task/revision number are not deploy evidence** if any of
  them can stay still while the artifact changes — a mutable `:latest` tag, a version file CI
  stopped bumping. Check something that *must* move.

---

## Tests, types, and gates

- **A type-level construct is not a runtime guarantee — and this is the only rule here that looks
  RIGHT in a pull request.** TypeScript's non-null assertion `!` is **erased at emit**:
  `const a = src.VALUE!` compiles to `const a = src.VALUE;`. Three tickets were filed to add a
  missing `!` *as a fix*; every one would have shipped a no-op that reads in the diff as a
  correction. The same holds for anything the compiler removes — `as`, `satisfies`, interfaces,
  type-only imports. **Every other entry in this document describes an error that looks wrong once
  measured; this one keeps review, CI and the diff all green.** If you need a runtime guarantee,
  write a runtime check.
- **Name the layer a test covers before letting it close a question.** A server-side test bounds the
  *server*; the client that emits the request is a separate system with its own failure modes. A
  zero in a click metric was finally settled by **reading the client source**, which no server test
  could do.
- **A test that can only ever green-skip is not a test.** DB-backed tests that skip when a connection
  string is unset will sit green forever in a CI that never sets it.
- **A required status check names a STRING, not a workflow.** Platforms will happily require a
  context that nothing produces — and the failure mode is silence: the PR reports *mergeable* and
  then refuses. Add the workflow, watch one PR emit the check, **then** add the requirement.
- **Two rulesets can each be satisfiable and jointly impossible.** One allowing only *squash* and
  another only *rebase* produce an empty intersection: a PR reporting **clean, every check green,
  unmergeable by any method**. Check the *intersection*, not each rule.
- **A guard's current no-op status is not a reason to skip it.** A predicate that filters nothing
  today because the data happens to be clean is still the thing that will filter something tomorrow.

---

## Observability and metrics

- **A truncated result set is not a sample.** A `head`-shaped limit is a **sampling decision
  disguised as a display decision**, and rows come back ordered by the store, not by relevance. A
  query capped at 8 rows returned one category entirely and produced a wrong conclusion — the other
  category held 48 of 68 series and contained the exact case being looked for. When the question is
  *"does this ever happen"*, **aggregate** (`count(... > 0)`), never eyeball a truncated head.
- **An instant query and a range query answer different questions**, and so do a count-at-a-moment
  and a count-over-a-window. Same family as truncation: a narrower view presented as the whole one,
  and it is **what the tool does by default** rather than a mistake anyone chose.
- **A gap in a count is only evidence once you have proved the baseline is dense enough to notice
  one.** A ~40h quiet window looked like a broken write path until the baseline was measured: 60 rows
  over 11 days but only **9 active days**, mean 6.7 on those, and two *interior* zero-days predating
  the change. At that density "quiet" and "broken" are not separable from the row count at all.
- **Read the health metric, not just the count.** `count = 0` and *a broken exporter* look identical.
  Whatever signal says "the check ran" is load-bearing; the count alone cannot distinguish "nothing
  wrong" from "nothing measured".
- **Before concluding a premise is unverifiable, check whether something already EXPORTS the
  answer.** A claim was parked as "needs access I don't have" — but the reconciling service
  **publishes its own comparison** as a metric. The author went looking for the **input** and missed
  that the **output** was already computed and scraped.
- **Separate seeded-at-insert from advanced-by-running-code before reading drift as motion.** Rows
  where `last_seen > created` by up to 218 days read as a column that moves. It did not:
  `max(last_seen) == max(created)` and nothing postdated the newest insert — they were backfill rows
  seeded with historical values against a backdated timestamp.
- **Test a join by running it, not by inspecting one side.** A label mismatch leaves *both* sides
  looking healthy while suppressing nothing. Split the expression: count the ungated left side,
  count it gated, count the right side. If gated equals ungated, the join matches nothing.

---

## Secrets — assert the property, never print the value

The transcript is durable and is sent to a model API. **A value printed there cannot be
un-printed**, and some credentials have no clean revocation path.

- **Never dump a credential-bearing file.** No `cat`, `head`, `tail`, `less`, `strings`, `base64 -d`,
  and **no `grep -A/-B/-C`** — trailing context you never previewed is how the worst leaks happen.
- **Assert the property instead:** `grep -c` for existence, `grep -oE` for one field with no context,
  `sha256sum | cut -c1-16` to compare, or parse and print **key names only**.
- **Not an unrestricted interpreter.** `python3 -c` / `node -e` on a credential file dumps it exactly
  as completely as `cat` does.
- **Silence mutating secret commands** — many print the full remaining table by default. Redirect,
  then verify by hashing.
- **PRESENCE is not ARMEDNESS, and ARMEDNESS is not CORRECTNESS.** Three distinct claims; collapsing
  any pair yields a confident wrong answer. A key in a config block proves only that *something* is
  injected; armedness needs the value's **length**; correctness needs a **comparison**. All three are
  checkable without disclosure — length via a query printing only `len()`, equality via hash
  prefixes. People collapse them because doing it properly *looks* like it needs the secret.
- **When redacting a flagged value without reading it, replace the LITERAL, not the line.** The
  obvious `sed -i '53s|.*|<redacted>|'` blanks the whole line and breaks the parse — and the
  resulting test failure looks *exactly* like "the test genuinely depended on that value" when it is
  only a syntax error you just introduced. **The instruction manufactures the evidence it tells you
  to interpret.** Replace only the quoted span; confirm the line count is unchanged.
- **A secret scanner keys its fingerprints to a COMMIT.** Redacting or rewriting a flagged file
  creates a **second** finding for the same content — the original commit stays in history and the
  rewrite adds another. Expect the count to *rise* after a cleanup.
- **Removing a file does not un-leak it.** Rotation is the only thing that ends exposure. A baseline
  file that suppresses a finding should say plainly that it rotates nothing.
- **Never ask someone else to run what a security control refused you.** A control one person can
  satisfy by asking a second person is not a control, and the norm that establishes is worse than any
  single payload. Restructure the command, or hand it to a human.

---

## Documentation and comments

- **A source comment is not a statement of deployed config** — it is an assertion about the world
  made by someone who could not see the world when they wrote it. A comment saying a flag "is not set
  today" survived long after the deployed config set it. This holds even when the comment's author
  owns the code.
- **But a comment IS evidence of INTENT, and the second half matters.** A change was reported as
  *incomplete* because a setting survived alongside the fix — the reasoning sat **three lines away**
  in the same file, and the "incomplete" follow-up would have undone a deliberate safety property.
  Verify config against the deployed artifact, then **read the stated intent before judging the
  design**.
- **Whenever two layers describe the same thing, the stricter one is the contract — and it is rarely
  the one you read first.** A database column that accepts anything may sit behind a validator that
  rejects most of it; a field present in the table may be plumbed nowhere in the write path and
  silently discarded. **This is worst wherever a handler swallows exceptions.** Read the validation
  layer and the write path, not just the schema.
- **In a web framework that generates API docs from source, a handler docstring may be PUBLISHED.**
  (FastAPI serves them as the OpenAPI `description`.) Internal reasoning, ticket ids and real
  identifiers go in a `#` comment inside the function body. **Scoped to request handlers** —
  elsewhere a docstring is the right home for provenance, and stripping ticket ids everywhere costs
  you the trail to the postmortem. And check the actual paths: schema and docs pages can sit under
  *different roots* on the same service.
- **A stale wrong claim in the most-read position is worse than one buried.** A ticket title
  asserting a mechanism that was later retracted is what people read before deciding whether to open
  it at all. Fix the title, not just the thread.

---

## Working with other people

- **A wrong framing from whoever is coordinating propagates further than anyone else's wrong
  finding.** An overstated warning was restated **verbatim by five of ten** collaborators — into a
  ticket description and a durable record — before a sixth measured it. **Nobody was careless.** A
  claim arriving from the coordinator *reads as established*, which is the correct default and
  exactly what makes it travel.

  **Two halves, both cheap:**
  1. **At the source — state uncertainty explicitly.** "Unmeasured", "my first read". The deference
     that spreads an error will spread the caveat with it.
  2. **At the restatement — attribute.** *"Per your framing, which I have not verified"* costs a
     clause and makes a third-hand claim **self-flagging**.

  *"Check harder" is not the fix* — nobody can re-derive every claim in a coordination message and
  still be useful.

- **Why a peer catches what the author cannot.** Of four "right answer by an invalid method" errors
  found in one session, **three were caught by whoever did not write the claim.** The mechanism, not
  the moral: **verification by the author tends to confirm**, because you re-run what you already
  believe and stop when it agrees. The author's check is aimed at the conclusion they already hold; a
  reader's is aimed at the claim. **Route findings through someone who did not produce them.**

- **A missing "how would we know this worked" line is a documentation gap, not an evidence gap.**
  Across 30 merges, none of the un-lined changes were actually unverifiable — every one carried a
  checkable claim in its prose. Retro-fit the measurement from the body. Only a claim that **cannot
  fail** is the real finding.

- **An expectation stated as fact creates pull toward motivated measurement.** When a task says
  *"this number will move"* and the measurement says it did not, **report the contradiction** — do
  not hunt for a different number that satisfies the brief.

- **Before ending a session, ask what dies with you.** Background jobs, watchers, port-forwards and
  scratch files are invisible to anyone else. **And read a stopped subagent's artifacts before the
  session ends — do not merely inventory them.** A watcher re-homed to a detached process should
  write a **heartbeat every poll**, so silence is never ambiguous between still-waiting and died.

---

## And one that is not a technique, because no checklist catches it

After withdrawing a finding, its author observed:

> **Having the refutation on screen is not the same as letting it bite.**

Their own query had already printed the rows that destroyed their argument, and they led with the
argument anyway. **Every failure in the table at the top of this document was visible at the moment
it was made.** The gap is attention, not method — and the only guard anyone found for it is the
ordering rule above: form the explanation *after* the measurement.
