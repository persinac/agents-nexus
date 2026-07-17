## 1. Seed-prompt support in the launch primitive

- [x] 1.1 Add `SEED_PROMPT` handling to `tmux/linux/tmux-scripts/open-claude.sh`: when set, use it as the opening prompt passed to `exec claude` (merged with or in place of checkpoint context), with no `send-keys`
- [x] 1.2 Preserve current behavior when `SEED_PROMPT` is unset (backward compatible)
- [x] 1.3 Manually verify: `SEED_PROMPT='…' tmux new-window -d -c <repo> open-claude.sh` launches an agent that starts from the seeded prompt — verified via hermetic stub-claude harness (seed lands as launch prompt; no-seed path unchanged)

## 2. Bridge scaffolding & feature flag

- [x] 2.1 Add `SLACK_SPAWN_ENABLED` flag (default off) read at bridge startup in `slack-bridge/index.js`
- [x] 2.2 Add config reads: spawnable-repo allowlist, rate-limit cap + window, Spark resolution threshold — all env-configurable
- [x] 2.3 Document the new env vars (and Doppler `nexus/prd` entries) alongside the existing bridge config — `docs/slack-bridge.md` Orchestrator section + `slack-bridge/spawnable-repos.example.json`

## 3. Repo resolution (Spark)

- [x] 3.1 Add an async `resolveRepo(text)` helper that queries the Spark MCP (`mcp__spark__spark`) and returns the top repo + score — `scripts/spark-resolve.py` (MCP-over-SSE to the live service via spark venv); verified returns `{repo,score}` matching the MCP tool
- [x] 3.2 Apply the resolution threshold; return null when no repo clears it — resolver returns null on Spark "no results"; numeric `min-score` cutoff is configurable and enforced bridge-side (Wave 5), defaulting permissive because the human confirm card is the real gate (Spark scores are tiny/reranked)
- [x] 3.3 Handle Spark errors/timeouts gracefully (treat as unresolved → usage hint) — never raises: timeout/connect/parse all emit `{repo:null,error}` and exit 0; verified service-down + gibberish + empty paths

## 4. Guardrails (in-memory state)

- [x] 4.1 Implement the spawnable-repo allowlist check — `matchAllowlist` (case-insensitive, canonical-key); unit-tested
- [x] 4.2 Implement the per-repo in-flight lock (`Set<repo>`); seed it from the live registry on startup so already-running repos count as locked — `inFlight` Set + `repoLocked` (registry + ledger); seeded via `seedLocksOnStartup`
- [x] 4.3 Implement the global rate-limit (rolling window of spawn timestamps vs configured cap) — `rateState` ring; unit-tested (under/full/prune)
- [x] 4.4 Compose guardrails in order allowlist → lock → rate-limit, all evaluated before any `tmux new-window`; ensure a rejection releases any acquired lock and leaves no side effects — lock acquired at card-post, released on No/timeout/failure; rate re-checked at approval

## 5. Spawn branch in handleMessage

- [x] 5.1 At the routing fall-through (`index.js:586`), when `SLACK_SPAWN_ENABLED`, call `resolveRepo()` instead of dead-ending on the usage hint — `offerSpawn` wired at the fall-through; returns false (→ usage hint) when unresolved
- [x] 5.2 If repo unresolved or not on the allowlist or repo locked → post the appropriate message (usage hint / already-running) and do not offer a spawn
- [x] 5.3 If a running agent already serves the repo, offer to route there rather than spawn a duplicate — `repoLocked` → "address it with `name: …`"

## 6. Confirmation card & action handler

- [x] 6.1 Post a Block Kit "Spin up an agent in `<repo>`? [Yes / No]" card (reuse the action-handler pattern at `index.js:620`), acquiring the per-repo lock at post time — `confirmCard` + `pendingSpawns`; `spawn:yes`/`spawn:no` action_ids (no collision with perm digits)
- [x] 6.2 Add the Yes/No action handler: on Yes → run rate-limit check then spawn; on No → release lock + acknowledge cancellation — `handleSpawnAction` → `performSpawn`
- [x] 6.3 Release the per-repo lock on Yes-complete, No, and confirmation timeout (so a stale card never blocks a repo forever) — `expirePendingSpawns` sweep (60s) at `SLACK_SPAWN_CONFIRM_TTL_MS`

## 7. Spawn execution & reporting

- [x] 7.1 On approval, launch the agent via `tmux new-window -d -n <name> -c <repo>` with `SEED_PROMPT` set to the originating message, using the bridge's systemd-safe env (PATH incl. fnm bin, `TMUX_SESSION=agents`) — `spawnWindow` (`-dP -F` captures pane/slot); verified in isolated session incl. apostrophe-quoting; open-claude.sh self-heals fnm PATH
- [x] 7.2 On success, reply in the originating thread with the spawned agent's name/slot; release lock after registration confirmed — `awaitRegistration` polls registry then releases lock to the live-agent check
- [x] 7.3 On launch failure or guardrail rejection, reply in-thread with the specific reason, release lock, and ensure no orphaned tmux window or registry entry — failure paths in `performSpawn`/`offerSpawn` delete the lock; no window created on rejection

## 8. Resilience: pin guard

- [x] 8.1 Add a `@keep` window-option check to `scripts/overseer-reap.sh` so a kept window is skipped even under `REAP_ALL=1` (alongside the existing exclude/attached guards) — always-honored sibling of `@orchestrator`
- [x] 8.2 Provide an ergonomic way to set/clear `@keep` (helper script and/or `keep <name>` Slack command), beyond raw `tmux set-option` — `scripts/agent-keep.sh` (name/slot/%pane resolution + `list`); Slack `keep` command folded into Wave 5
- [x] 8.3 Verify: a `@keep` window survives a forced `REAP_ALL=1` sweep; an unkept idle window still gets reaped — verified in an isolated throwaway session (pinned survived, unpinned reaped)

## 9. Resilience: durable agent ledger

- [x] 9.1 Define the ledger location + format (JSONL append-log under `~/.tmux/`, with compaction) and a small read/write/reconcile module — `scripts/agent-ledger.py` (stdlib, bridge-independent, flock-guarded); full lifecycle unit-tested
- [x] 9.2 Write a `live` ledger entry on every spawn (repo, name, seed prompt, timestamp) — `performSpawn` → `ledgerCmd(['spawn', …])` with pane/slot
- [x] 9.3 Update the entry to `dormant` + checkpoint pointer when the reaper checkpoints-then-kills an agent (in `overseer-reap.sh`) — best-effort, fail-open, no-op for non-orchestrator agents; verified end-to-end
- [x] 9.4 On bridge startup, read the ledger and reconcile against the live registry (downgrade/clear stale `live` entries); seed the per-repo lock from surviving `live` entries — `seedLocksOnStartup` calls `agent-ledger.py reconcile` then seeds `inFlight`
- [ ] 9.5 Verify: spawn → reap → ledger shows `dormant` with a valid checkpoint path; ledger survives a bridge restart

## 10. Resilience: restore & reconnect nudge

- [x] 10.1 Add restore mode to `open-claude.sh` (seed the opening prompt from a named checkpoint passed in), with fallback to a plain spawn when the checkpoint is missing — `RESTORE_CHECKPOINT` env; verified readable→inject, missing→fallback
- [x] 10.2 Add a Slack restore command + Block Kit restore card that respawns a dormant agent in its repo seeded from its checkpoint, run through the same guardrails (allowlist/lock/rate-limit); return the entry to `live` — `restore <repo>` command + `nudgeCard` `restore:do` buttons → `doRestore`/`handleRestoreAction` → `performSpawn({restore:true})` (ledger `restore` event)
- [x] 10.3 No-op restore when the repo's agent is already `live` (inform, don't duplicate) — `doRestore` checks ledger state + `repoLocked`
- [x] 10.4 Implement the reconnect nudge: when dormant entries exist, surface a count + one-action restore (default trigger: next inbound `#nexus` message); never auto-restore — `maybeNudge` (fire-and-forget, ≥1h throttle)
- [ ] 10.5 Verify: reap an agent → restore via command and via card → agent returns seeded from checkpoint; nudge appears and restores only on explicit action

## 11. Verification & rollout

- [ ] 11.1 End-to-end test in `#nexus`: unaddressed message → Spark resolves an allowlisted repo → confirm card → Yes → seeded agent appears and is reapable
- [ ] 11.2 Negative tests: below-threshold/unresolved → usage hint; non-allowlisted repo → no offer; duplicate repo → blocked; rate-limit exceeded → rejected; No/timeout → no spawn + lock released
- [ ] 11.3 Resilience end-to-end: pin survives `REAP_ALL=1`; AFK reap → dormant ledger entries → reconnect nudge → restore-from-checkpoint brings the working set back
- [x] 11.4 Confirm rollback: `SLACK_SPAWN_ENABLED=0` + restart reverts to the usage-hint behavior with zero side effects (pin guard + ledger are inert without spawns) — verified by gating inspection + load smoke test (flag off/on both boot-guard clean; every new path gated on `SPAWN_ENABLED`; reaper ledger calls no-op without live entries)
- [x] 11.5 Configure conservative production defaults (allowlist of 1–2 repos, small rate-limit) and enable the flag — `~/.tmux/spawnable-repos.json` (store-front, wallet-api); `SLACK_SPAWN_ENABLED=1` in Doppler nexus/prd; bridge restarted, orchestrator ENABLED, socket connected, startup reconcile ran clean
