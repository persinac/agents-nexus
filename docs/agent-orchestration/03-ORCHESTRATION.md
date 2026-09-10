# Running a fleet: roles, spawn, brief, verify, stand down

The operational half. `01-CONVENTIONS.md` is what agents are told; this is what the coordinator does.

Substrate-agnostic — it assumes only that you can start an agent in a working directory with a seed
prompt, and send it a message later.

---

## The shape that worked

| Role | How many | What it owns |
|---|---|---|
| **Coordinator** | 1 | Dispatches work, re-derives claims, holds the cross-repo picture, makes no code changes of its own |
| **Producer** | 1 per repo/workstream | Does the work. Merges freely under the standing authorization |
| **Review partner** | paired among producers | Reads a peer's findings. **Not** a separate role — producers pair with each other |
| **Verifier** | 1, read-only | Runs *after* merge. Measures whether the change did what it claimed |

**The verifier is read-only on purpose.** Its authorization is deliberately narrower than everyone
else's — a verifier that can fix things will start fixing things instead of reporting them.

**One producer per working tree if you can.** Where you cannot, the shared-tree rules in
`01-CONVENTIONS.md` are not optional. Measured on one host: four agents in one checkout, two in
another, two in a third.

---

## Spawning: never trust the exit code

The single most expensive failure mode is a spawn that reports success and produces nothing.
Observed: **six consecutive spawns returned exit 0 and produced six dead panes.** The workspace was
created, the pane was listed, and capturing output returned empty — *indistinguishable from an agent
still booting.*

Root cause was mundane and worth knowing because it is threshold-dependent: past a size limit the
substrate wrapped the command in `exec <cmd>`, and a command starting with a bare assignment
(`SEED='…' prog`) is invalid after `exec` — bash treats the assignment as the program name. **A real
seed prompt is 800–1400 bytes, which lands exactly on that threshold**, so the same command that
worked at 110 bytes in testing broke once a paragraph of seed text was added.

**Three guards, in order:**

1. **Refuse a bare-assignment command prefix** and say what to use instead (`env VAR=value prog`).
2. **After spawning, poll the process table** for a live agent process whose environment carries the
   expected workspace name. **Never trust the spawn command's exit code.**
3. **Refuse an odd number of single quotes** in the seed. An apostrophe in prose — *"that column's
   first writer"* — closes the quote and kills the spawn silently.

Exit non-zero with a diagnosis if the process never appears. A spawner that cannot prove the agent
started is the first self-confirming green signal in the chain.

---

## The brief

Everything in `01-CONVENTIONS.md` gets injected automatically. On top of that, a good task brief has
four parts:

1. **The concrete goal**, in the repo's own vocabulary.
2. **What "done" looks like** — ideally the `VERIFY:` line you expect them to be able to write.
3. **What is off-limits**, named explicitly. Not a general caution — *this migration, that
   credential, this file.*
4. **Who their review partner is**, if paired.

**⚠️ Do not state an expectation as a fact.** *"This number will move"* creates real pull toward
motivated measurement. Say *"I expect this to move; if it does not, report that"* — the honest
identical result is usually the more useful answer.

**And mark your own uncertainty out loud.** A claim arriving from the coordinator *reads as
established*, so it gets carried rather than re-derived. Measured: **an overstated warning was
restated verbatim by five of ten agents**, into a ticket description and a durable record, before a
sixth measured it and it was walked back. Nobody was careless — that is the correct default and
exactly what makes an error travel.

Say **"unmeasured"** or **"my first read"** and the same deference spreads the caveat with it.

---

## Verification, after merge

Run it **after** merge, never before. A pre-merge gate slows the thing that is already working.

Three checks, in increasing value:

1. **Did it deploy?** Rollout state plus the running count — not a version endpoint alone. Cheap and
   mechanical.
2. **Did the predicted effect occur?** The novel part. When a PR says *"excludes synthetic rows"*,
   run the query and confirm the count moved as predicted. When it says *"no behaviour change"*,
   prove behaviour did not change.
3. **Is the claim still true a day later?** Re-run on a schedule. A predicate that worked at merge
   and silently stopped matching is a recurring shape.

**Outcomes:** `CONFIRMED` · `NO-CLAIM-LINE` (retro-fit it from the body) · `UNFALSIFIABLE` (**the real
finding**) · `DRIFTED`.

**It has no veto and blocks nothing.** It reports drift; it does not fix.

---

## The mechanism that actually produced the value

Of four *"right answer by an invalid method"* errors found in one session, **three were caught by
whoever did not write the claim.**

The mechanism, not the moral: **verification by the author tends to confirm**, because you re-run
what you already believe and stop when it agrees. The author's check is aimed at the conclusion they
already hold; a reader's is aimed at the claim.

**Route findings through someone who did not produce them.** That is a property of how the work is
organised, not of anyone being clever — and it is why, on the run this came from, the corrections
were worth more than the merges.

A second datum in the same direction: of nine durable rules that came out of that session, **only two
came from the reviewer** — three came from the agent *being reviewed*, and the rest from producers
correcting the coordinator.

---

## Standing down

Agents do not stop on their own. Say it explicitly, and say what is off-limits:

> Finish the pass you are on, report ids, then **go quiet**. Do not start a new workstream, do not
> pick up your blocked list, and do not act on peer traffic tonight unless it is data loss in
> progress. **Off-limits until further notice:** *\<name the specific migration, credentials, files
> and branches\>.*

**Then have each agent close its own gaps before going quiet:**

- Anything of theirs that is **not on the board and not in a checkpoint** — close that gap first.
- A **stale wrong ticket is worse than no ticket.** Correcting one is part of standing down.
- **Filing a ticket is itself an action** in a system with automation attached. One agent filed new
  cards *without* the label its queue poller watches, specifically so nothing would auto-start
  overnight. Nobody asked it to.

---

## Checkpoints

One file per agent, written at session end, carrying: what shipped, what is **blocked on a human**
(stated separately from what is finished), and **what is session-scoped and would die with them.**

**Two traps found the hard way:**

- **Filename collisions.** If checkpoints are named by date + project, two agents on the same repo
  collide. One had its entry **overwritten** by a peer's write and restored it by appending; another
  ended up with two agents' notes concatenated in one file. **Put an agent component in the name.**
- **Correcting a peer's checkpoint.** Append a clearly-marked correction *beneath* their entry in
  your own voice. Do not silently rewrite another agent's record — the file is what tomorrow reads.

---

## The coordinator's own discipline

Everything in `02-VERIFICATION-RULES.md` applies to you *more*, because your errors propagate
further. Specifically:

- **Re-derive agent claims before acting on them**, and say when you did not.
- **Verify a claim at its source before filing a ticket on it.** Two findings dissolved on
  re-derivation in one session; both would have become wrong tickets.
- **Hand decisions back rather than leaning.** When an agent has a criterion and you have a
  measurement, give them the measurement and let them decide. On the last card of that session, an
  agent declined to close on the coordinator's evidence, reproduced the numbers independently, and
  *then* closed — which is the behaviour you want and the opposite of what a confident sentence
  produces.
- **When you are wrong, correct it where the work lives** — the ticket, the checkpoint, the rules
  file — not only in chat.
