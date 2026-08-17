## Status: ✅ IDENTITY MODEL LIVE — Slack-gossip publish path SUPERSEDED (2026-08-17)

**FQDN instance identity is in production**, but by a different road than this change built.
The live path is the **NATS presence KV**: `transports/nats-transport.js` keys every entry with
`orchestrator.js#fqdnToKvKey` as `<host>.<workspace>.<name>`, unconditionally — no flag involved.
The `nexus_presence` bucket currently holds FQDN entries across `alex-nexus` and `melvin`, and
`GET /agents` serves them (now with a paste-ready `fqdn` per entry).

What this change ALSO built — the **Slack-channel gossip v2 wire format** gated on
`SLACK_PRESENCE_FQDN` — is now dormant: `SLACK_A2A_ACTIVE = !SINGLE_HOST` and the fleet's A2A,
presence included, is on NATS. The parse/format/election logic (§1–§3) is still live and unit-
tested; only its Slack *transport* leg is retired.

⚠️ **`SLACK_PRESENCE_FQDN` being off does NOT mean FQDN presence is off.** That inference was
made in this repo and it was wrong — the flag governs a deprecated Slack gossip payload, not the
KV. Check the KV (or `/agents`), never the flag. The deferred items in §6/§7 below are therefore
closed as superseded rather than pending: they can never be executed, because the leg they
verify is out of service.

---

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
- [~] 6.4 **SUPERSEDED — will not be done.** This asked to enable `SLACK_PRESENCE_FQDN=1` and verify cross-host instance addressing over Slack gossip. The fleet cut over to NATS instead, where FQDN keying is inherent and flagless, and cross-host addressability is demonstrated by the `nexus_presence` bucket carrying both `alex-nexus.*` and `melvin.*` entries. Enabling the flag now would light up a deprecated Slack leg for no gain.

## 7. Verification

- [x] 7.1 Logic verified by unit tests (`node --test`, 40 pass): v1/v2 parse, dedup, identity collisions (intra + cross-host), per-instance reachability, back-compat; `node --check` on both bridge files; `openspec validate --strict` passes.
- [~] 7.2 **SUPERSEDED (Slack-gossip e2e).** The equivalent NATS path is live: `alex-nexus/interactive/general` is an addressable FQDN in the presence KV today, and instance-exact routing rides `hostSubjectFilter` + the per-host durable consumer rather than gossip.
- [~] 7.3 **SUPERSEDED (Slack-gossip e2e).** A mixed v1/v2 gossip fleet cannot arise — there is no gossip. Under NATS every entry is FQDN-keyed at write time, so there is no bare-name wire format left to be compatible with.
- [~] 7.4 **SUPERSEDED as written (Slack-gossip e2e).** The idle-gate itself is unchanged and still in force on the NATS delivery path; its remaining hardening is tracked in `nats-a2a-bus-transport` §5 (ack-on-idle), which is the right home for it.
