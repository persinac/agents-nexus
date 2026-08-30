# Overseer + cattle — chatting with a pinned pane that spawns disposable workers

Nothing here is new infrastructure. It's a composition of primitives that already exist —
`substrate.sh spawn`, `SEED_PROMPT` task injection, `agent-send.sh`, `agent-keep.sh`, and the idle
reaper (`docs/overseer.md`) — into one pattern: you chat with a pinned "overseer" pane, it spawns
"cattle" (`/spawn-cattle.md`) to do self-contained tasks headless with `--dangerously-skip-permissions`,
and each cattle sends exactly one line back when it's done instead of a chat loop.

| Concern | Where it lives |
|---|---|
| Pin the overseer so it survives being idle | `scripts/agent-keep.sh` |
| Spawn a cattle, inject its task | `commands/spawn-cattle.md` → `substrate.sh spawn` + `SEED_PROMPT` |
| Cattle's one-line completion signal | `agent-send.sh <overseer> "<result>"`, addressed by `$AGENT_FROM` |
| Catch a cattle that never calls back | `scripts/overseer-reap.sh` (idle only — see the gap below) |
| Cost class | `bg-` session prefix (`docs/model-routing.md`), never `ds-` (`docs/deepseek-routing.md`) |

---

## Why the overseer needs `agent-keep.sh`, not just a name

`open-claude.sh` auto-tags a pane `@orchestrator` when it's literally named `overseer` or
`orchestrator`, and `overseer-reap.sh` normally skips `@orchestrator`-tagged panes. But on a box
where `overseer-reap.service` sets `REAP_ALL=1` (this Linux box does — "unattended for days, prune
everything idle, command post included"), that exemption is explicitly dropped. `@keep` is the one
exemption `overseer-reap.sh` calls "always-honored... even under `REAP_ALL=1`." So:

```bash
scripts/agent-keep.sh "$AGENT_FROM"     # pin  (@keep 1) — do this once per overseer pane
scripts/agent-keep.sh "$AGENT_FROM" off # unpin, if you ever want it reapable again
scripts/agent-keep.sh                   # list what's currently pinned
```

Naming the pane `overseer` is still worth doing for readability, but treat it as cosmetic here —
the pin is what actually protects you.

## Spawning a cattle

From the overseer's own chat: `/spawn-cattle <task>`. See `commands/spawn-cattle.md` for the exact
mechanics. In short: one `substrate.sh spawn` into a fresh `cattle/<slug>` bucket, task delivered via
`SEED_PROMPT` (the launch prompt itself — not send-keys, so there's no terminal-readiness race),
running `--dangerously-skip-permissions` so it never blocks on a prompt nobody will answer.

The cattle pane gets **neither** `@keep` nor `@cohort`. Both are always-honored by the reaper even
under `REAP_ALL=1` — tagging a cattle with either makes it immortal, which is exactly backwards for
something meant to be disposable.

## The completion signal — one line, not silence, not a chat loop

The seed instructs the cattle to send exactly one message back — `agent-send.sh "$TARGET" "<one-line
result>"`, where `$TARGET` is the overseer's own `$AGENT_FROM` — when it finishes, fails, or gets
stuck, then stop.

This is deliberately *not* pure silence. The reasoning is an incident already on record in this
repo: **FC-1249**. A conductor worker did reflexive `git checkout -b` inside a worktree it was told
not to touch, silently forking the mission onto an invented branch; the parent committed to the
branch it was left on — empty — and reported a green MR. The failure was undetectable from the
overseer's side, because the worker never said anything wrong. Every mature fire-and-forget path
already in this stack (`minions`' `complete_engineer_work`/`release_engineer_work`, `conductor`'s
Slack ping, `/distribute -bg`'s `agent-send.sh --relay`) ships a mandatory terminal signal for
exactly this reason — a silent failure is a permanent one until someone happens to go looking.

What this pattern deliberately keeps from the original ask: no back-and-forth chat. The cattle
sends one line and stops — the overseer's own context never gets polluted with a cattle's turn-by-
turn tool calls.

## The wall-clock gap — stated plainly, not glossed over

There is no cap on elapsed wall-clock time for a cattle pane, anywhere in this repo today. Only two
things bound an unattended agent, and neither is a per-task budget:

- **The idle reaper** (`scripts/overseer-reap.sh`) — reaps after `REAP_IDLE_SECS` (4h) of *idle*
  time. It explicitly skips any agent with `@waiting=0` (actively working), at any duration. A
  cattle stuck in a genuinely active-looking loop — not idle, just wrong — is never caught by this.
- **The proxy's token/USD ceiling** (`proxy/main.py`, `docs/model-routing.md`) — a deliberately loose
  backstop, tuned "~8-18x above the busiest session ever observed... so it should never fire during
  real work." It exists to stop a pathological runaway, not to bound a single task's cost.

So: a cattle that finishes cleanly reports back and gets torn down by hand. A cattle that goes idle
without reporting (crashed to a prompt, gave up silently) is caught by the idle reaper after ~4h,
with a 15-minute self-checkpoint warning first. A cattle that keeps making tool calls indefinitely,
correctly or not, is caught by nothing until the (loose) cost ceiling trips.

**Accepted for now, not fixed here.** A candidate follow-up: a wall-clock companion to the idle
reaper, keyed off the registry's existing `AT=` spawn-timestamp field (currently read only as an
idle-time fallback), respecting `@keep`/`@cohort` the same way the idle reaper does. Not built as
part of this pattern — flagged so it doesn't quietly become "solved" by omission.

## Teardown — and a caveat inherited, not introduced, here

Once the callback lands:

```bash
"$HOME/.tmux/substrate.sh" workspace-close "cattle/<slug>"
```

Same idiom `/distribute -bg` already uses for its delegate teardown. One caveat worth knowing:
`substrate.sh kill` on herdr is a bare `herdr pane close` with no deregister step, and herdr fires no
`pane-died` event — so a pane closed without deregistering first looks like a crash to
`herdr-recover.sh`, which will **respawn** it. `herdr-close-agent.sh` is herdr's purpose-built close
primitive that deregisters, cleans the worktree, then closes, in that order. Whether
`workspace-close` already routes through the safe path wasn't verified while writing this — the
exposure (if it exists) already applies to `/distribute -bg`'s existing delegate teardown today, so
this pattern neither introduces nor fixes it. Worth checking before you lean on this daily.

## Why `bg-`, not `ds-`

Cattle spawn over herdr A2A and run under your Claude subscription — no API key, no per-token
billing. `bg-` opts a session into the existing session-class ceiling (`BG_CEILING_ENABLED`,
`docs/model-routing.md`) — cheaper-tier Anthropic, same subscription, no vendor change. `ds-` routes
to the DeepSeek cost leg (`docs/deepseek-routing.md`), which is real API-metered billing — a
deliberate choice made when this pattern was designed, not an oversight: staying on the subscription
for unattended background work was an explicit requirement, not the default this repo would pick on
its own.

## One thing that needed no extra work

PreToolUse hooks (`~/.claude/settings.json` — the credential-dump and destructive-command guards)
are global, not per-session. Every cattle pane inherits them automatically, bypass-permissions
included. `block-destructive.sh` was written for exactly this scenario: *"an overnight, unattended
agent is spawned with `--dangerously-skip-permissions`... that mode raises no prompts at all... this
hook closes exactly that gap."* Nothing to wire per-cattle.
