# Agent bus — instance addressing & the colon-truncation drop

> **STATUS: RESOLVED (2026-07-16).** Moves 1–3 implemented: `orch.parseAddressedLine()`
> (colon-**space** delimiter, handle-aware — admits `:` in the token, non-greedy) + a new
> `resolveByPane()` + actionable drop logging (ambiguous-name log names the handles), wired
> into **both** the bus path (`handleBusMessage`) and the human control-channel path. The pure
> parser is unit-tested in `slack-bridge/orchestrator.test.js` against the tables below. Docs
> corrected (`herdr-workflow.md`, `slack-bridge.md`). `agent-send.sh --via-slack wQ:pF "…"`
> now delivers to that exact pane. This doc is retained as the design record.
>
> **ADDENDUM — a deeper 4th layer, found by live-testing the fix.** Moves 1–3 were necessary
> but NOT sufficient: a live handle round-trip still dropped. Underneath sat a more
> fundamental bug — `loadRegistry` (`index.js`) matched `SLOT=(\d+)` and gated on
> `name && slot`. In **herdr mode `SLOT` is the pane HANDLE (`wN:pN`), not a number**, so the
> gate dropped **every** herdr agent → the bridge's registry view was **empty** →
> `resolveByName`/`resolveBySlot`/`resolveByPane` all returned null. (So Layer 3's "ambiguous
> across 2 matches" was really "0 matches" — the collision framing was a red herring; the
> whole fleet was invisible to the bridge, breaking bus delivery + smart routing + `status`
> in herdr mode. Local send-keys A2A was unaffected — it uses `agent-send.sh`'s own registry
> read, not the bridge.) **Fix:** capture `SLOT` as-is; keep entries with `name && (slot ||
> pane)`. Verified live: send to a handle → `[bus] delivered to <name> (wN:pN)`, message lands
> in that exact pane. LESSON: unit tests + code review passed all of Moves 1–3; only driving
> the real round-trip exposed the registry-filter layer.

A message sent to a specific agent instance over the bus can be **silently dropped**:
it posts to `#nexus-agents` (so you see it) but is never delivered to the agent. This
happens whenever the target contains a `:` — most commonly a herdr pane handle
(`wQ:pF`), which is the *only* way to disambiguate two agents that share a name.

This doc records the root cause (three stacked layers), then designs the fix. The
headline: the bus is **name-keyed and has no per-instance address**, and the parser
that reads addresses off the channel truncates at the first colon — so the natural
workaround (address by pane handle) is silently mis-parsed.

## Symptom

```
$ agent-send.sh --via-slack wQ:pF "hello world"
Sent to wQ:pF via bus (from general): hello world      # <- HTTP 200 on /send, NOT delivery

#nexus-agents shows:  wQ:pF: ↩ from general: hello world
recipient (wQ:pF):    …never receives anything
```

The `Sent … via bus` line only confirms the POST succeeded. Delivery is a **separate
step** on a channel round-trip, and that step failed with no log and no error.

```
 agent-send.sh --via-slack wQ:pF ──POST /send──► bridge
                                                   │  posts verbatim: "wQ:pF: ↩ from general: …"   (index.js:1584)
                                                   ▼
                                            #nexus-agents  ◄── VISIBLE HERE
                                                   │
                                                   ▼  every bridge reads it back → handleBusMessage
                                        parse "^([A-Za-z0-9][\w./-]*)\s*:\s*(…)$"  (index.js:1237)
                                                   │  ':' not in token class → token = "wQ"   ← TRUNCATED
                                                   ▼
                                        resolveByName("wQ")  → null   (index.js:1248, 233)
                                                   ▼
                                        if (!agent) return;  ← SILENT DROP  (index.js:1249)
```

## Root cause — three stacked layers

**Layer 1 — the parser truncates the address at the first colon.**
`handleBusMessage` reads an addressed line with (`slack-bridge/index.js:1237`):

```js
const m = cleanSlackText(event.text).match(/^([A-Za-z0-9][\w./-]*)\s*:\s*([\s\S]+)$/);
```

The address-token class `[\w./-]` (alnum, `_`, `.`, `/`, `-`) does **not** include `:`.
The `/send` outbound format is `` `${to}: ↩ from ${sender}: ${msg}` `` (`index.js:1584`),
so a handle target produces `wQ:pF: ↩ from general: hello world`. The regex stops the
token at the first `:` → `token = "wQ"`, body = `"pF: ↩ from general: hello world"`.
`wQ` matches no agent, and the unknown-name branch returns with no log (`index.js:1249`).

**Layer 2 — the bus is name-keyed; slot/handle targets are never resolved.**
`handleBusMessage` only ever calls `resolveByName` (`index.js:1248`). `resolveBySlot`
exists (`index.js:250`) but is unused on the bus path. Meanwhile `agent-send.sh
--via-slack` posts the target **verbatim** as `to` (`agent-send.sh:194`) — it does *not*
reverse-resolve a handle/slot to the agent's NAME. (That reverse-resolution,
`resolve_pane_name`, only runs on the *local* `channel`-mode path — `agent-send.sh:314`.)
So even a colon-tolerant parser would then call `resolveByName("wQ:pF")` and find
nothing. **Addressing the bus by instance is unsupported by design.**

**Layer 3 — same-name collisions make name-addressing itself unresolvable.**
The two agents in this fleet are both `NAME=general`, both `WS=interactive`:

| pane   | name    | workspace     |
|--------|---------|---------------|
| wQ:pF  | general | interactive   |
| wQ:pH  | general | interactive   |

`resolveByName` (`index.js:233`) returns `null` for **every** spelling once a name
collides inside one workspace (verified):

| address                | verdict                               |
|------------------------|---------------------------------------|
| `general`              | `null` — 2 workspaced matches, ambiguous |
| `interactive/general`  | `null` — still 2 matches              |
| `wQ:pF` (the handle)   | truncated to `wQ` → `null` (Layer 1)  |

So there is **no bus address that resolves to exactly one of the two `general`s.** The
name-keyed bus has no per-instance address, and the one discriminator that *is* unique
(the pane handle) is exactly what Layer 1 breaks.

> **This contradicts a stated invariant.** `docs/herdr-workflow.md:137` claims "herdr
> enforces GLOBALLY-UNIQUE agent names … real collisions can't happen among herdr
> agents." That holds for agents started via `herdr agent start`, but the shared
> `interactive` bucket collects **human-launched** Claude Code sessions that register
> through `hook-sessionstart.sh`, bypassing the uniqueness gate. Duplicate `interactive`
> names are therefore normal, not exceptional. `herdr-workflow.md` should be corrected
> (see [Docs to correct](#docs-to-correct)).

## Design principles (carried from the existing bus design)

1. **Opt-in, default off** for anything that changes routing behavior; the working bus
   never regresses.
2. **Reuse what we have.** `resolve_pane_name` (sender), `resolveBySlot`/the registry
   (bridge), `parseAddress` in `orchestrator.js`. Pure/testable pieces go in
   `orchestrator.js` and get unit tests; stateful glue + Slack I/O stay in `index.js`.
3. **Slack stays the human-observable transport.** The address grammar on the channel
   must stay human-readable; no opaque IDs where a handle will do.
4. **No silent drops.** An undeliverable addressed message must leave a log line that
   names the target and says why — the current failure was invisible, which is what
   made it expensive to diagnose.

## The fix

Three moves. Move 1 is the load-bearing one and is almost entirely receiver-side; it
makes the pane handle a **first-class bus address** without inventing new grammar.

### Move 1 — make the parser handle-aware (delimiter = colon-**space**)

Key insight: the `/send` format always emits the address/body delimiter as `": "`
(colon **followed by a space**), while a pane handle's internal colon is always
colon-**non**-space (`wQ:pF`). So we can let the token *contain* colons and anchor the
delimiter on colon-**whitespace**. Change the regex at `index.js:1237` to:

```js
//                     ┌ admit ':' in token   ┌ non-greedy      ┌ require ws AFTER colon
const m = cleanSlackText(event.text).match(/^([A-Za-z0-9][\w:./-]*?)\s*:\s+([\s\S]+)$/);
```

Three minimal edits: add `:` to the token class, make the token **non-greedy** (`*?`),
and require `\s+` (not `\s*`) after the delimiter colon. Validated against every case:

| input line                                   | old token                     | new token   |
|----------------------------------------------|-------------------------------|-------------|
| `wQ:pF: ↩ from general: hello world`         | `wQ` ❌                       | `wQ:pF` ✅  |
| `scripts: ↩ from general: ping`              | `scripts`                     | `scripts` ✅ |
| `scripts: ↩ from x: TODO: fix it`            | `scripts`                     | `scripts` ✅ (non-greedy stops at first `: `) |
| `chatbot/feedback/example-service: ↩ …`          | `chatbot/feedback/example-service`| unchanged ✅ |
| `::nexus-presence:: {…}` / `::nexus-relay:: …`| (no match)                   | (no match) ✅ — leading `:` still fails `^[A-Za-z0-9]` |

**Sole regression:** a name-addressed line with *no* space after the colon
(`general:hi`) stops being recognized as addressed. This never occurs from `/send`
(which always emits `: `); it can only come from a human hand-typing in `#nexus-agents`,
where a space is the natural form. Acceptable, and called out in the rollout note.

The sentinel guard is doubly safe: `handleMessage` already routes presence/relay out
*before* `handleBusMessage` (`index.js:1303-1308`), and the `^[A-Za-z0-9]` anchor still
rejects their leading `:`.

### Move 2 — resolve a handle/slot token to a local instance

After Move 1, `token` can be `wQ:pF`. In `handleBusMessage`, before the
`parseAddress`/`resolveByName` path, add an instance-first branch:

```js
// A herdr pane handle (wN:pN) or a bare slot number addresses ONE local instance
// exactly — bypass name resolution and the presence-owner election (a handle is
// inherently host-local and instance-exact; cross-host uses host/name).
let agent = null;
if (/^w[A-Za-z0-9]+:p[A-Za-z0-9]+$/.test(token)) {
  agent = resolveByPane(token);            // new: registry lookup by PANE_ID
} else if (/^\d+$/.test(token)) {
  agent = resolveBySlot(token);            // exists (index.js:250); now reachable
} else {
  const { host, workspace, name } = orch.parseAddress(token, {...});
  if (host && host.toLowerCase() !== SELF_HOST.toLowerCase()) return;  // not ours
  agent = resolveByName(name, workspace);
  // …existing presence single-owner election…
}
if (!agent) {
  console.warn(`[bus] no local agent for '${token}' — dropped (from bus)`);   // Move 3
  return;
}
```

`resolveByPane` is a two-line registry filter on `PANE_ID` (mirrors `resolveBySlot`).
A handle/slot that isn't in *this* host's registry simply yields `null` → logged drop;
it never mis-delivers, because a handle is host-local by construction.

With Moves 1+2, the **original failing command works unchanged** —
`agent-send.sh --via-slack wQ:pF "hello world"` posts `wQ:pF: …`, the parser keeps the
handle, and `resolveByPane("wQ:pF")` delivers to exactly that instance. No new flag
required for the receiver: it's a pure bug-fix (handles previously never resolved).

### Move 3 — kill the silent drop (observability)

The unknown-target `return` at `index.js:1249` logs nothing. Every non-delivery of an
*addressed* line must log the target and the reason:

- unknown handle/slot/name → `[bus] no local agent for '<token>' — dropped`
- **ambiguous name** → make the null path actionable:
  `[bus] '<name>' ambiguous across N local instances (wQ:pF, wQ:pH) — address by pane handle`

The ambiguous-name log turns the exact failure in this report into a self-service fix:
the sender is told to re-send with a handle.

### Sender-side hygiene (small, optional)

- `agent-send.sh --via-slack <slot-number>`: translate the slot to its **pane handle**
  before POSTing (handles are stable; slots renumber — see the `resolveBySlot`
  live-slot note at `index.js:246`). Reuse `resolve_pane_name`'s lookup to get the pane.
- Leave `--via-slack <name>` and `--via-slack <handle>` posting verbatim — Move 1+2
  make both correct on the receiver.

## What this deliberately does **not** do

- **No new selector grammar** (`name#2`, `name%pane`, `name!inst`). Every such char is
  outside the token class and would force a wider, riskier parser change; the pane
  handle already is a unique, human-readable, colon-free-*enough* instance address once
  the delimiter is colon-space.
- **No forced-unique `interactive` names.** Auto-suffixing human sessions
  (`general` → `general-2`) is heavier-handed than handle addressing and surprises the
  human who opened the window. Handle addressing solves the collision without touching
  registration. (Revisit only if bare-name addressing to interactive agents is a
  common enough ask to justify it.)
- **No cross-host instance addressing.** Pane handles are host-local. Reaching a
  specific *remote* instance still uses `host/name`, and remains a collision only if a
  remote host also runs duplicate names — out of scope here.

## Rollout & flags

Moves 1–3 are **bug-fixes**, not behavior additions — a handle target has never worked,
so nothing depends on the broken behavior. Ship them together, no flag, with the one
documented regression (human `name:hi` with no space) noted in `slack-bridge.md`. The
sender-side slot→handle translation is cosmetic and can ride along or land later.

Guardrails at ship time:
- Unit-test the new regex in `orchestrator.test.js` against the table above (extract
  the parse into a pure `orch.parseAddressedLine(text)` so it's testable without Slack).
- Unit-test `resolveByPane` / `resolveBySlot` against a synthetic registry with a
  duplicate name.
- Manual: from `wQ:pH`, `agent-send.sh --via-slack wQ:pF "hi"` → lands in `wQ:pF`, not
  `wQ:pH`, not dropped.

## Test plan

1. **Handle round-trip.** Two same-named agents; send to each by handle; assert each
   receives exactly one message and the other receives none.
2. **Bare unique name unchanged.** Send to `scripts`; still delivered.
3. **Ambiguous name logs, does not deliver.** Send to `general`; assert no delivery +
   the actionable ambiguity log naming both handles.
4. **Body-colon safety.** Send a message whose body contains `: ` (e.g. `TODO: x`);
   assert the token is the address, body is intact.
5. **Sentinel safety.** Post a presence and a relay line; assert neither is parsed as
   an addressed delivery (routed out at `index.js:1303-1308`, and non-matching regex).
6. **Idle-gating preserved.** Handle-addressed message to a busy pane queues and flushes
   on idle (the `BUS_DEFER`/`busQueue` path at `index.js:1269` is downstream of
   resolution and unchanged).

## Docs to correct

- `docs/herdr-workflow.md:137-142` — soften the "real collisions can't happen" claim:
  the `interactive` shared bucket admits duplicate human-session names (they register
  via `hook-sessionstart.sh`, not `herdr agent start`), and **pane-handle addressing** is
  the supported way to disambiguate them over the bus.
- `docs/slack-bridge.md` — document the address grammar precisely: delimiter is
  colon-**space**; a token may be a `name`, `[host/][workspace/]name`, a pane handle
  `wN:pN`, or a slot number; note the `name:hi` (no-space) regression.

## File map (verified references)

| location | role |
|---|---|
| `slack-bridge/index.js:1237` | addressed-line parser (Move 1) |
| `slack-bridge/index.js:1248-1249` | `resolveByName` call + silent drop (Moves 2, 3) |
| `slack-bridge/index.js:1269-1284` | idle-gate + deliver (unchanged, downstream) |
| `slack-bridge/index.js:1584` | `/send` outbound format `${to}: ↩ from …` |
| `slack-bridge/index.js:233` | `resolveByName` (name-only) |
| `slack-bridge/index.js:250` | `resolveBySlot` (exists, unused on bus path) |
| `slack-bridge/orchestrator.js:402` | `parseAddress` (`[host/][ws/]name`, splits on `/`) |
| `slack-bridge/orchestrator.js:270,278` | presence/relay sentinels (leading `:` guard) |
| `tmux/mac/tmux-scripts/agent-send.sh:194` | `--via-slack` posts target verbatim |
| `tmux/mac/tmux-scripts/agent-send.sh:159-169` | `resolve_pane_name` (local path only) |
| `tmux/mac/tmux-scripts/agent-send.sh:312-319` | channel-mode reverse-resolve (name-keyed) |
| `docs/herdr-workflow.md:137` | the globally-unique-names invariant (to correct) |
