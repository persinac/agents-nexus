# herdr substrate spike — Phase 0 findings

**Date:** 2026-07-13 · **herdr:** 0.7.3 (Homebrew core, bottled) · **protocol:** 16
**Socket:** `~/.config/herdr/herdr.sock` (per-session: `~/.config/herdr/sessions/<name>/herdr.sock`)
**Verdict:** ✅ **GO.** Every gate cleared, including the make-or-break headless-drive test. The migration is feasible; proceed to Phase 1 (see `plans/…herdr…` / the approved migration plan).

This is the frozen op-map — the contract the `substrate` CLI shim and `substrated` daemon implement. It mirrors `docs/agent-sdk-spike.md` in intent (de-risk before building).

## The three recorded open feasibility questions — answered
1. **spawn remap** → clean. `workspace.create`/`pane.split`/`agent.start` + `pane.run` cover every `tmux new-window` call site.
2. **`@waiting`/registry remap** → clean, and *better* than tmux. herdr reports semantic `agent_status` natively (`working|blocked|idle`) and pushes changes over `events.subscribe`; `agent.list` returns name→pane→state→cwd in one call (replaces the registry-file + `show-option` scrape).
3. **rewrite friction** → favorable via the adapter seam (shim + daemon). No call-site rewrite of logic, just re-pointing the substrate verbs. herdr's *own* Claude Code integration is a `~/.claude/hooks/herdr-agent-state.sh` hook calling `report-agent` — i.e. our hook-authoring approach is exactly what herdr does, so the pattern is validated and there's a reference impl.

## Wire protocol (verified)
- Newline-delimited JSON over a Unix socket. Request `{"id","method","params"}` → success `{"id","result":{"type",...}}` or error `{"id","error":{"code","message"}}`.
- **One request per connection**: the server closes the socket after each non-subscription response. Open a fresh connection per write (this is exactly what the `herdr` CLI does). **Subscriptions stay open** (long-lived push).
- `ping` → `{"type":"pong","version":"0.7.3","protocol":16,"capabilities":{"live_handoff":true,"detached_server_daemon":false}}`.
- **`detached_server_daemon:false`** — this build's `herdr server` does not self-daemonize; for a persistent fleet server use `brew services start herdr` (launchd) rather than a bare `herdr server`.
- Headless server: `herdr server` (foreground/backgroundable) or `brew services start herdr`; `herdr server stop`; `herdr status server`.

## Op-map (substrate verb → herdr method / CLI, verified)

| Substrate verb | herdr method | CLI (verified) | Notes |
|---|---|---|---|
| spawn workspace/first pane | `workspace.create` | `herdr workspace create --cwd P --label L --no-focus` | → `result.root_pane.pane_id` (`w1:p1`), `tab_id`, `workspace_id` |
| spawn agent | `agent.start` | `herdr agent start <name> --workspace w1 --split down --cwd P --no-focus -- <argv…>` | → `agent_started`; auto-registers the agent by name |
| split pane | `pane.split` | `herdr pane split <pane> --direction right\|down [--ratio] [--cwd] [--env K=V]` | needs an existing source pane |
| run command (cmd+Enter) | `pane.run` | `herdr pane run <pane> "<cmd>"` | executes in the PTY |
| **deliver: literal text** | `pane.send_text` | `herdr pane send-text <pane> "<text>"` | **headless-verified** |
| **deliver: submit / keys** | `pane.send_keys` | `herdr pane send-keys <pane> enter` | named keys (`enter`,`esc`,`ctrl+h`,…); this + send-text = the `agent-send.sh` deliver pattern |
| read pane content | `pane.read` | `herdr pane read <pane> --source visible\|recent\|recent-unwrapped [--lines N]` | → `result.text` |
| wait for output | `wait output` | `herdr wait output <pane> --match TXT [--regex] [--timeout MS]` | returns matched line + surrounding text |
| wait for state | `wait agent-status` | `herdr wait agent-status <pane> --status idle\|working\|blocked\|done [--timeout MS]` | |
| **report state** | `pane.report_agent` | `herdr pane report-agent <pane> --source S --agent L --state idle\|working\|blocked\|unknown [--message] [--custom-status] [--seq N]` | `done` is derived, not reportable |
| report metadata | `pane.report_metadata` | `herdr pane report-metadata <pane> --source S [--state-label STATUS=TEXT] [--custom-status] [--title] [--ttl-ms N] [--seq N]` | persists; rides in `agent.list` + events |
| read one pane's state | `pane.get` | `herdr pane get <pane>` | → `result.pane.agent_status` (+ `agent`,`custom_status`,`cwd`,…) |
| enumerate agents/state | `agent.list` | `herdr agent list` | → `agents[]` with `agent`,`agent_status`,`custom_status`,`state_labels`,`pane_id`,`foreground_cwd` — the registry-scrape replacement |
| bootstrap snapshot | `session.snapshot` | `herdr api snapshot` | one-time; then subscribe + cache |
| subscribe (push) | `events.subscribe` | (raw socket) | **requires `pane_id` per subscription**: `{"subscriptions":[{"type":"pane.agent_status_changed","pane_id":"w1:p1"}]}` → `subscription_started`, then pushed `{"event":"pane.agent_status_changed","data":{agent,agent_status,custom_status,state_labels,pane_id,workspace_id}}` |
| kill | `pane.close` / `workspace.close` | `herdr pane close <pane>` / `herdr workspace close <ws>` | |
| rename | `pane.rename` | `herdr pane rename <pane> <label>` | |

Full JSON schema saved (Phase 0): `herdr api schema --output <path>` (223 KB; top keys `$schema/protocol/schema_version/schemas/title`).

## State-model mapping (decided)
herdr natively owns `agent_status` + `custom_status` (one human string) + `state_labels` (status→text map). Verified: both `custom_status` and `state_labels` **persist and ride in the pushed event + `agent.list`**.

| tmux today | herdr sink | verified |
|---|---|---|
| `@waiting 0` (working) | `report_agent state=working` | ✅ |
| `@waiting 1` (needs input) | `report_agent state=blocked` + `report_metadata --state-label blocked=<wait_type>` + `--custom-status "<human>"` | ✅ (`state_labels.blocked="permission_prompt"` round-trips) |
| `@waiting 2` (idle/done) | `report_agent state=idle` | ✅ |
| `@wait_type` (permission/elicitation) | `state_labels[blocked]` | ✅ preserves the distinction |
| `@wait_since` / `@last_tool` (epoch) | **`substrated` sidecar** (no native KV bag) | decided |
| `@keep` / `@cohort` / `@orchestrator` | **`substrated` sidecar** | decided |

`substrated` translates cached `agent_status` back to the `'0'/'1'/'2'` strings the existing readers compare against, so consumer comparison logic is untouched in the first cut.

## Gotchas / notes for Phase 1
- **Auto-detection coexists with hook authoring.** A bare `claude` launched in a pane is auto-detected as agent `claude` at `idle` even with the `claude` integration *not* installed. An explicit `report_agent --source nexus-hook …` **overrides** it (verified: idle → blocked with our `custom_status`/`state_labels`). Decision stands: keep our hooks authoring; do **not** install herdr's `claude` integration (it would be a second reporter on a different source).
- **Events can coalesce** rapid transitions (working→idle within one window delivered one settled event). Fine for our use (settled state is what gates delivery); each real hook fires a discrete report.
- `pane.read` CLI exposes `visible|recent|recent-unwrapped` (the raw `detection` source from the docs isn't a CLI flag).
- `events.subscribe` without `pane_id` → `invalid_request: missing field pane_id`.
- Real Claude Code TUI renders in a headless pane (verified the `❯` prompt box + "manual mode" footer via `pane read`).

## Verified evidence (2026-07-13)
- Headless server + raw-socket `ping` with **no TUI client attached**.
- `pane.run` executed a shell command; `send_text` + `send_keys enter` typed **and executed** a command — both read back via `pane.read`.
- `report_agent` set state; `pane.get`/`agent.list` read it; `events.subscribe` pushed the change; `session.snapshot` bootstrapped.
- Real `claude` launched via `agent.start`, auto-detected, then governed by a hook-style `report_agent` override.
