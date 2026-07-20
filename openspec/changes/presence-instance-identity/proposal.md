## Why

The bus presence gossip identifies agents by a per-host **set of bare names**: `::nexus-presence:: {v:1, host, agents:[name, …], ts}`, folded by `applyPresence` into `host → { agents: new Set(names) }`. Two agents that share a name on one host **collapse into a single presence entry** (Set dedup) and become **unaddressable**: a bus message to `host/name` reaches `resolveByName`, matches two local registry rows, returns `null` by design, and is **silently dropped**.

This is not hypothetical. A live cross-host reply — `melvin/melvin2001` → `alex-nexus/general` — was dropped exactly this way: `alex-nexus` had two agents registered as `general` (one was a mislabeled `general-2`), so `alex-nexus/general` was ambiguous. The addressing *grammar* already supports `host/workspace/name` (`orch.parseAddress` splits it, `resolveByName(name, ws)` scopes it), but presence **throws the workspace away** on the wire, and registration populates `WORKSPACE` inconsistently (some registry entries carry it, others none), so there is no data behind a qualified address to resolve against. Net effect: same-name instances are invisible to the fleet and reachable only by **host-local pane handles**, which a cross-host sender cannot use.

## What Changes

- **Per-instance presence payload (schema v2).** Presence carries `agents:[{name, workspace, pane}]` records, not bare name strings. `parsePresence` accepts v2 and falls back to v1 (a bare string becomes `{name, workspace:'', pane:''}`), so mixed-version fleets interoperate.
- **Instance-keyed presence map.** `applyPresence` stores instances keyed by `workspace/name` (pane as tiebreaker), retaining a name index for legacy `host/name` resolution. Intra-host duplicates become **distinct, visible** entries instead of collapsing.
- **Owner election + collisions on full identity.** `ownerOf` / `reachability` / `presenceCollisions` operate on `host + workspace + name`. A *true* collision is the same `host/workspace/name`; two same-name-different-workspace agents are **not** a collision.
- **Cross-host instance addressing.** `host/workspace/name` (and `host/pane` for instance-exact) resolves to exactly one remote instance — wiring the already-parsed grammar through owner election and delivery. This deliberately **reopens** the "cross-host instance addressing" item that `docs/agent-bus-instance-addressing.md` scoped out.
- **Registration correctness.** `substrate.sh register`, `hook-sessionstart.sh`, and the launchers (`open-claude.sh` / `conductor`) always populate `WORKSPACE`; the bridge's `localLiveAgents()` emits `{name, workspace, pane}`. Fixes the inconsistency where some entries have no workspace.
- **Opt-in, default off.** Behind a flag; bare `host/name` still resolves when unique, and an ambiguous name that used to drop silently becomes a logged, self-service error. Zero behavior change until enabled.
- **Out of scope (deliberately):** forced-unique naming / auto-suffix (`general → general-2`) — the instance-addressing doc rejected it as surprising to the human who opened the window, and this change makes it unnecessary; the reply-correlation envelope (roadmap Phase B).

## Capabilities

### Modified Capabilities
- `agent-presence-registry`: presence identity moves from per-host bare-name **sets** to per-instance `{name, workspace, pane}` **records**. Same-name instances become distinct and visible; owner election and collision detection key on the full `host/workspace/name` identity; reachability reports every instance, not one-per-name-per-host.

### New Capabilities
- `agent-instance-addressing`: a bus message MAY be addressed to a specific remote instance via `host/workspace/name` (or `host/pane`), delivered by exactly one host to exactly one pane, replacing the current silent-drop-on-collision with deterministic resolution.

## Impact

- **Code:** `slack-bridge/orchestrator.js` (`parsePresence`/`formatPresence` schema v2 + v1 fallback; `applyPresence` per-instance store; `ownerOf`/`ownersOf`/`reachability`/`presenceCollisions` on full identity; `parseAddress` already handles `host/ws/name`); `slack-bridge/index.js` (`localLiveAgents` emits records; `publishPresence` payload; `handleBusMessage` qualified-address resolution + owner election; `GET /agents` output shape); `tmux/*/tmux-scripts/substrate.sh` + `hook-sessionstart.sh` + `open-claude.sh`/`conductor-run.sh` (populate `WORKSPACE` consistently); `slack-bridge/orchestrator.test.js` (parse + resolution tables).
- **Config:** one opt-in flag (default off) — either `SLACK_PRESENCE_FQDN=1` or a v2 bump gated by the existing `SLACK_PRESENCE_ENABLED`.
- **Wire format:** presence **v2** (`agents` is an array of objects); v1 still parsed. A v1 peer's agents fold in as `workspace:''` — unqualified but still reachable by unique name.
- **Docs:** `docs/agent-bus-instance-addressing.md` (addendum: cross-host instance addressing now in scope); `docs/slack-bridge.md` (presence schema v2, address grammar); `docs/agent-bus-roadmap.md` (this realizes the identity half of Phase C — capability presence).
- **Operational:** bare-name addressing is unchanged when the name is unique fleet-wide; only an *ambiguous* name now requires a qualified address — and either way the non-delivery is logged, never silent. Baseline note: the `agent-presence-registry` capability currently lives in the (still-open) `slack-agent-bus` change, so these deltas layer on that capability rather than a published `openspec/specs/` baseline.
