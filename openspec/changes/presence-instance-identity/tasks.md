## 1. Presence wire format v2 (consume-side first, flag-gated)

- [x] 1.1 Add `SLACK_PRESENCE_FQDN` (default off) config read at bridge startup (`index.js`), gated on presence being on
- [x] 1.2 `parsePresence` (orchestrator.js): accept `v:2` where `agents` is `[{name, workspace, pane}]`; keep `v:1` (bare strings); both normalize to instance records via `toInstance`
- [x] 1.3 `applyPresence`: store `host → { agents: instance[], ts, seen }` de-duped by full instance key (workspace+name+pane); out-of-order ts guard preserved
- [x] 1.4 Unit tests: v1/v2 parse, format→parse round-trip, dedup of exact dups, two same-name-different-workspace stay distinct, ts ordering

## 2. Owner election, reachability, collisions on full identity

- [x] 2.1 `ownersOf`/`ownerOf`: name-level ownership on records (bare-name back-compat), lexically-smallest host
- [x] 2.2 `presenceCollisions`: collision = same `workspace/name` identity (>1 instance, across hosts OR twice on one host); different-workspace same-name is NOT flagged; v1 (no workspace) still flags cross-host same-name
- [x] 2.3 `reachability`: one row per **instance** (`{name, workspace, pane, host, owner, collided}`)
- [x] 2.4 Unit tests: intra-host duplicate visible; identity collision (intra + cross-host); v1 back-compat collision; deterministic owner

## 3. Publish-side v2

- [x] 3.1 `localLiveAgents()` (index.js): return records `{name, workspace, pane}` (registry ∩ live panes), deduped by pane
- [x] 3.2 `formatPresence`: emit `v:2` records when `SLACK_PRESENCE_FQDN` on, else `v:1` bare names (graceful degrade)
- [x] 3.3 `publishPresence`: cadence unchanged (startup + heartbeat + registry watch); payload now carries workspace/pane; `gatherFleetStatus` name consumer updated for the record shape

## 4. Registration correctness (populate workspace at the source)

- [x] 4.1 `substrate.sh register` already writes `WORKSPACE`, falling back to `$NEXUS_WORKSPACE` → `workspace-of <pane>` (herdr bucket) — verified in `tmux/mac/tmux-scripts/substrate.sh:313`. No change required.
- [x] 4.2 `hook-sessionstart.sh` + launchers register through that path, so the fallback populates workspace for human-launched and spawned agents alike.
- [x] 4.3 Backfill is by re-register (an agent re-running `substrate.sh register` refreshes its entry) — demonstrated live: `wA:p5` re-registered `general → general-2` with `WORKSPACE=agents-nexus/routing`. Old-format entries (pre-`WORKSPACE` writer, e.g. a stale `w3:pK`) correct themselves on next register.
- [x] 4.4 Verified: a current registration writes all 7 fields incl. `WORKSPACE`; the two-`general` case presents as `interactive/general` + `agents-nexus/routing/general-2`.

## 5. Resolution ladder + cross-host instance addressing

- [x] 5.1 `handleBusMessage`: bare `wN:pN` (local pane) fast path retained; added `host/pane` → named host + `resolveByPane` (instance-exact cross-host)
- [x] 5.2 `host/workspace/name` → workspace-scoped `resolveByName(name, ws)` (already workspace-aware) + existing idle-gate; grammar already parsed by `parseAddress`
- [x] 5.3 Ambiguous bare-name path now logs the QUALIFIED candidates (`host/workspace/name`, else `host/pane`) instead of only a pane-handle hint; still no double-delivery
- [x] 5.4 `agent-send.sh` needs no change — the FQDN grammar already parses and `--via-slack` posts the qualified token verbatim (the receiver resolves it)
- [x] 5.5 `GET /agents` returns instances (`reachability` now yields `{name, workspace, pane, host, owner, collided}`); consume-side collision log upgraded to identity form

## 6. Docs & rollout

- [x] 6.1 `docs/slack-bridge.md`: `SLACK_PRESENCE_FQDN` flag row, v2 `/agents` shape, and a "FQDN presence — instance identity" subsection
- [x] 6.2 `docs/agent-bus-instance-addressing.md`: addendum — cross-host instance addressing now in scope (pane-handle + no-forced-unique-names decisions unchanged)
- [x] 6.3 `docs/agent-bus-roadmap.md`: note that Phase C's per-instance presence structure is now delivered here
- [ ] 6.4 **DEFERRED to operator rollout:** enable `SLACK_PRESENCE_FQDN=1` on a host, restart the bridge, verify a two-instance host is fully addressable cross-host. Not done here to avoid restarting the live bridge mid-fleet-work (this box is crash-prone). Flag defaults off, so the running bridge is unaffected until deliberately enabled.

## 7. Verification

- [x] 7.1 Logic verified by unit tests (`node --test`, 40 pass): v1/v2 parse, dedup, identity collisions (intra + cross-host), per-instance reachability, back-compat; `node --check` on both bridge files; `openspec validate --strict` passes.
- [ ] 7.2 **DEFERRED (live e2e, needs flag on):** `melvin/… → alex-nexus/interactive/general` delivers instance-exact; ambiguous bare name logs candidates instead of dropping.
- [ ] 7.3 **DEFERRED (live e2e):** mixed-version fleet — a v1 peer reachable by unique name; a v2 host's duplicates addressable from another v2 host.
- [ ] 7.4 **DEFERRED (live e2e):** idle-gate + no-loop preserved on the new qualified paths.
