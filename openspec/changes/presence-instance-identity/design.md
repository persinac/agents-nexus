## Context

Presence today (shipped in `slack-agent-bus`, Phase 2):
- **Publish:** `publishPresence()` → `localLiveAgents()` returns an array of **names** → `formatPresence({host, agents, ts})` posts `::nexus-presence:: {v:1, host, agents:["general", …], ts}` to `#nexus-agents`.
- **Consume:** `parsePresence` returns `{host, agents: strings, ts}`; `applyPresence(map, snap)` stores `map.set(host, { agents: new Set(snap.agents), ts, seen })`.
- **Derive:** `ownerOf`/`ownersOf` (lexically-smallest claiming host per name), `reachability` (one row per name per host), `presenceCollisions` (same name on ≥2 **hosts**).
- **Deliver:** `handleBusMessage` parses `[host/][workspace/]name: body`, and for the name branch calls `resolveByName(name, ws)` against the local `loadRegistry()`.

Two structural facts cause the bug:
1. **`agents` is a `Set` of bare names.** Two `general`s on one host store as one `"general"`. The duplicate is invisible to presence, `ownerOf`, `reachability`, and collision detection (which only compares across hosts).
2. **`resolveByName(name, '')` returns `null` on ≥2 local matches** — correct (it can't pick one), but the caller then silently `return`s unless `dups > 1` triggers the ambiguity log; and even that log only helps a *same-host* sender who can use a pane handle.

Meanwhile the **grammar is already richer than the data**: `parseAddress('alex-nexus/interactive/general')` → `{host:'alex-nexus', workspace:'interactive', name:'general'}`. The workspace dimension exists in the address and (sometimes) in the registry (`WORKSPACE=agents-nexus/routing`), but it is **dropped on the presence wire** and **inconsistently populated at registration** (observed: `w3:pK` had no `WORKSPACE`; `wA:p5` had one).

Prior art / boundary being moved: `docs/agent-bus-instance-addressing.md` solved the *same-host* duplicate with **pane-handle** addressing (host-local), **declined** forced-unique names ("surprises the human who opened the window"), and **explicitly scoped out cross-host instance addressing**. This change reopens that last item by giving presence enough identity to make a remote instance addressable.

## Goals / Non-Goals

**Goals:**
- Make every live agent uniquely identifiable and addressable fleet-wide, including same-name instances on one host.
- Turn the silent drop-on-collision into a deterministic delivery (qualified address) or a logged, actionable non-delivery.
- Preserve bare `host/name` for the common unique case; add cost only for the ambiguous case.
- Interoperate across mixed-version bridges (v1 and v2 presence on the channel at once).
- Reuse the existing grammar (`parseAddress`), delivery ladder, and idle-gate — additive, not a rewrite.

**Non-Goals:**
- Forced-unique naming / auto-suffix — explicitly rejected upstream; this change makes it unnecessary.
- Reply-correlation envelopes / RPC (roadmap Phase B) — orthogonal.
- Changing same-host `send-keys` or the local pane-handle fast path.
- A shared presence store — stays announce-on-channel (the Phase-1/2 decision), just with a richer record.

## Decisions

### Instance key = `workspace/name`, pane as tiebreaker
The stable, human-readable identity is `host/workspace/name`. Pane (`wN:pN`) is unique but host-local, opaque, and renumbers across a herdr restart, so it is the *tiebreaker/instance-exact* selector, not the primary key. **Alternative considered:** key on pane only — rejected; pane isn't meaningful cross-host and changes on restart, so owner election would thrash. **Alternative considered:** synthesize a global uuid per agent at spawn — heavier, needs a new field everywhere, and loses the human-readable address the bus is built around.

### Presence wire = v2 records, v1 fallback
`agents` becomes `[{name, workspace, pane}]`; bump `v` to 2. `parsePresence` accepts both: a v1 bare string folds in as `{name, workspace:'', pane:''}`. `formatPresence` emits v2 only when the FQDN flag is on, else v1 — so a v2 bridge among v1 peers degrades gracefully and a v1 bridge ignores fields it doesn't read. **Alternative considered:** a second sentinel (`::nexus-presence2::`) — rejected; one sentinel with a version field is simpler and the parser already keys on `::nexus-presence::`.

### Map stores instances, not a name Set
`applyPresence` stores `host → Map<instanceKey, {name, workspace, pane, ts}>` where `instanceKey = workspace + NUL + name` (pane appended only if a workspace+name still collides). A derived `name → instances[]` index preserves the fast legacy lookup. **Alternative considered:** keep the Set and bolt workspace on as a parallel map — rejected; two sources of truth drift (this whole bug is drift).

### Resolution ladder, most-specific-first
`handleBusMessage` resolves in order: (1) bare pane handle `wN:pN` → local pane (unchanged); (2) `host/pane` → that host, that pane (**new**, instance-exact cross-host); (3) `host/workspace/name` → that host, scoped registry lookup (**new**); (4) `host/name` or `name` → name index; unique → deliver; ambiguous → **log + drop with the qualified addresses to retry** (upgraded from silent). **Alternative considered:** always require a qualified address — rejected; needless friction for the unique-name majority and a breaking change.

### Registration always populates `workspace`
Fix at the source: `substrate.sh register` takes/derives a workspace (herdr `workspace_id` / bucket), and the launchers pass it; `localLiveAgents()` reads it back into the presence record. Without this, v2 presence carries `workspace:''` and we're back to bare-name collisions. **Alternative considered:** derive workspace only in the bridge from the registry — insufficient, since the registry itself is the inconsistent source.

### Opt-in, default off
Behind `SLACK_PRESENCE_FQDN` (or a v2 flag under `SLACK_PRESENCE_ENABLED`). Off = today's behavior byte-for-byte. On = v2 publish + instance map + qualified resolution. Mirrors every prior bus phase.

## Risks / Trade-offs

- **Mixed-version fleet.** A v1 bridge among v2 peers publishes bare names (`workspace:''`) and can't be instance-addressed. Mitigation: v2 consumers fold v1 agents as unqualified; bare-name delivery still works for unique names; instance addressing is best-effort until every bridge is v2. Acceptable, matches the incremental rollout of every bus phase.
- **Workspace still duplicated.** Two agents with the *same* `host/workspace/name` (e.g. both `interactive/general`) — the doc's original scenario. Mitigation: pane tiebreaker → `host/pane` is always unique; and this is now a *detected, logged* collision, not a silent drop.
- **Wire size.** Records are larger than bare strings. Mitigation: still one line per host per heartbeat (5 min); a fleet of dozens of agents is a few KB — negligible on Slack.
- **Registration lag.** An agent live before it (re)registers with a workspace shows `workspace:''` transiently. Mitigation: same self-healing full-state snapshot as today; the next heartbeat corrects it.
- **Reopening a scoped-out boundary.** The instance-addressing doc chose *not* to do cross-host instance addressing. Mitigation: this is additive and flagged; the pane-handle host-local path is untouched; reviewers can reject the cross-host half and keep just the visibility/collision fixes.

## Migration Plan

1. Land `parsePresence`/`applyPresence` v2 + v1 fallback and the instance-keyed map behind `SLACK_PRESENCE_FQDN` (default off) — pure consume-side, safe with the flag off.
2. `formatPresence`/`localLiveAgents` emit v2 records when the flag is on.
3. Registration: populate `WORKSPACE` in `substrate.sh register` + launchers + `hook-sessionstart.sh`; backfill live agents by a one-time re-register.
4. `handleBusMessage` resolution ladder: add `host/pane` and `host/workspace/name`; upgrade the ambiguous-name drop to a logged error naming the qualified retries.
5. `GET /agents` returns instances (`{name, workspace, pane, host, owner}`); update the dashboard/CLI readers.
6. Unit tests (`orchestrator.test.js`): v1/v2 parse, instance keying, owner election with intra-host dups, resolution table.
7. Enable on one host, verify a two-instance host is fully addressable cross-host; then fleet-wide. Rollback: unset the flag + restart → v1 behavior.

## Open Questions

- **Flag shape:** a dedicated `SLACK_PRESENCE_FQDN`, or fold into a `SLACK_PRESENCE_ENABLED=2`? Leaning dedicated flag for a clean on/off and easy rollback.
- **Workspace source of truth:** herdr `workspace_id` (e.g. `w3`, `wA`) vs the logical bucket (`interactive`, `agents-nexus/routing`)? The address should use the *human* bucket; confirm the registry's `WORKSPACE` is that, not the physical `wN`. (Observed `WORKSPACE=agents-nexus/routing` suggests logical — good.)
- **`host/pane` cross-host:** deliver instance-exact by pane even though pane is opaque cross-host — worth it for tie-broken duplicates, or is `host/workspace/name` + a logged collision enough? Leaning: support it, cheap once the record carries `pane`.
- **Legacy owner election:** when a v2 instance and a v1 bare name for the same name coexist, which owns bare `name`? Leaning: v2 instances win; a bare v1 name is a last-resort fallback.
- **Backfill:** re-register all live agents on rollout, or let the 5-min heartbeat converge? Leaning: trigger a re-register so the fleet is correct in seconds, not minutes.
