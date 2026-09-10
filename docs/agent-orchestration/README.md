# Agent orchestration package

Everything needed to run a multi-agent fleet the way the overnight run worked, minus anything
specific to one company, stack, or cloud.

Extracted 2026-09-09 from a fleet of eleven agents across eight repos. Every rule here was earned by
something going wrong — none of it is theory.

```
01-CONVENTIONS.md        what every agent is told (paste into a CLAUDE.md)
02-VERIFICATION-RULES.md how to tell a real check from one that cannot fail
03-ORCHESTRATION.md      what the coordinator does: spawn, brief, verify, stand down
hooks/                   the only actual enforcement in the package
settings.hooks.json      wiring for the above
```

## Install, in this order

**1. The hooks first.** They are the only *control* here; everything else is a document an agent can
talk itself out of. See `hooks/PORTING.md` — four environment-specific things to change, and a
verification step, because a hook that is not installed and a hook that approves look identical from
inside a session.

**2. `01-CONVENTIONS.md`** into a `CLAUDE.md` that every agent loads. **Read the authorization
section first and set it to your own risk tolerance** — as written it authorises merging without
asking, which is right for a two-person startup and probably wrong for a regulated codebase. The rest
travels as-is.

**3. `02-VERIFICATION-RULES.md`** alongside it, or appended. This is the part that keeps its value
longest and needs no adjustment at all.

**4. `03-ORCHESTRATION.md`** for whoever coordinates. Not agent-facing.

## The three things that mattered most

If you adopt nothing else:

**Every PR carries a `VERIFY:` line** — one measurement that would come out *different* if the change
did not work. Baseline before this was introduced: 24 merges in three days, **not one** carrying a
falsifiable claim, including a merge reported as "deployed" whose new code was wired to nothing.

**Route findings through someone who did not produce them.** Of four *"right answer by an invalid
method"* errors in one session, **three were caught by whoever did not write the claim** — because
verification by the author tends to *confirm*. You re-run what you already believe and stop when it
agrees.

**Never trust a spawn's exit code.** Six consecutive spawns returned exit 0 and produced six dead
panes. Poll the process table instead. This is the first self-confirming green signal in the chain,
and if you get it wrong, everything downstream is measuring nothing.

## What is deliberately not here

The substrate (terminal multiplexer, message bus, presence registry), the credential plumbing, and
anything naming real infrastructure. Those are the parts that make a fleet *yours* and make it unsafe
to travel. This package assumes only that you can start an agent in a directory with a seed prompt
and send it a message later.

## One caveat about the source

The fleet this came from stores its own version of these rules in a file that is loaded into every
session across 37 repos and **is not in any git repository** — no review, no history, no rollback for
the most load-bearing document in the system.

That is a real hazard and it is worth not reproducing. **Put your copy under version control.**
