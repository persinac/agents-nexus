# Agent SDK harness spike

Exploring migrating the tmux harness from driving the interactive `claude` TUI
(send-keys + settings.json shell hooks + `@waiting` screen-scrape) to the
**Claude Agent SDK** (Python, `claude-agent-sdk`), keeping tmux as the
display/attach surface (hybrid model).

Branch: `spike/agent-sdk-harness`. Runner scaffold: `agent-runner/` (venv gitignored).

## Decisions locked in

- **Posture: hybrid.** SDK owns the agent loop; it runs inside a tmux pane and
  renders its event stream there so `tmux attach` still works. "Take-over" = type
  a line that's queued as the next prompt at the turn boundary (no true mid-turn
  keyboard seize — the SDK can't accept mid-turn input, but the bus already only
  delivers when idle, so this matches existing semantics).
- **Runtime: Python** (`claude-agent-sdk`), to reuse the litellm classifier logic
  in the permission gate. Confirmed `ClaudeAgentOptions` exposes `can_use_tool`,
  `hooks`, `env`, `setting_sources`, `session_store`/`resume`/`fork_session`,
  `agents` (subagents), `effort`, `mcp_servers`, `tools` — the Python SDK is fully
  capable (the permission callback is NOT TS-only, contrary to earlier doubt).

## V1 — proxy passthrough (the kill-switch)  →  ✅ GO (with one required flag)

The whole observability story is `ANTHROPIC_BASE_URL` → litellm/nexus-proxy
(`:4000`) → Langfuse, with per-agent trace naming via a `sess/<name>/` path
prefix (`proxy/main.py`). Requirement: the SDK must route through it.

**Result:** The SDK routes through the proxy and traces land in Langfuse with
cost — **only if the SDK is prevented from loading `~/.claude/settings.json`.**

- SDK version: `0.2.114` (latest on PyPI; needs py≥3.10; venv on 3.14.2).
- Auth: `~/.claude/.credentials.json` (OAuth) — SDK-spawned `claude` authenticates
  automatically; no `ANTHROPIC_API_KEY` needed.
- **Default (loads settings.json): BYPASS.** With `ANTHROPIC_BASE_URL` exported to
  the proxy, the SDK-spawned `claude` still went direct to `api.anthropic.com` —
  no `spike-sdk-proxy` trace in ClickHouse. Cause: settings.json's `env` block
  pins `ANTHROPIC_BASE_URL=https://api.anthropic.com`, applied on top of process env.
- **`setting_sources=[]`: PASS.** Re-ran → ClickHouse shows two `spike-sdk-fixed`
  traces, observations `claude-haiku-4-5-20251001` with `total_cost` populated
  (0.039 + 0.00059). `HookEventMessage`s also vanished (settings.json hooks no
  longer loaded — confirms the flag took effect).

**Implication for the harness:** run agents with `setting_sources=[]` (or without
`"user"`) and set the per-agent proxy URL (`…/sess/<agent-name>`) in-process. We
were going to define hooks/permissions/MCP in-process anyway, so not inheriting
the CLI's filesystem settings is desirable, not a loss.

**Verify a passthrough (oracle = ClickHouse, not proxy logs — success path logs nothing):**
```
docker exec langfuse-clickhouse clickhouse-client --user clickhouse --password clickhouse -q \
  "SELECT name, toString(timestamp) FROM traces WHERE name='<sess>' ORDER BY timestamp DESC LIMIT 5"
```

## 🚨 Side-finding (unrelated to the SDK, but live): fleet is bypassing the proxy

`~/.claude/settings.json` → `env.ANTHROPIC_BASE_URL = "https://api.anthropic.com"`.
This is the exact regression the 2026-06-24 checkpoint fixed to `env: {}`. It's back.
Because Claude Code applies settings.json `env` on top of the shell env, every
agent launched via `open-claude.sh` (which exports the proxy URL) has that URL
clobbered → goes direct to Anthropic → no proxy, no Langfuse cost/session trace.

- **Last model trace through the proxy: 2026-07-07 15:07 UTC.** Everything since is
  only mnemon MCP tool traces (`create_note`/`search_similar`/`log_event`).
- **Fix:** set settings.json `env` back to `{}` (removing `ANTHROPIC_BASE_URL`), then
  relaunch agents (settings.json is read at Claude Code startup; running agents keep
  bypassing until relaunched). Worth finding *what re-added it* so it stops recurring.

## V2 — idle-gated delivery without send-keys  →  ✅ PASS

Harness loop: `query -> drain receive_response (turn runs) -> idle -> read inbox
-> query next`. `ClaudeSDKClient.query(prompt)` sends into the live session;
`receive_response()` yields until (incl.) that turn's `ResultMessage`, delimiting a turn.

Test (`spike_v2_delivery.py` + `spike_inbox_writer.py`): an EXTERNAL process
(stand-in for the slack-bridge) appended a message to `/tmp/spike-inbox-*.txt` ~2s
into a deliberately long TURN A (`Bash(sleep 6)`).

```
writer wrote to inbox            +3.0s    ← mid-turn, external process (no tmux)
TURN A: Bash(sleep 6)            +3.3s → +12.0s
        replied DONE_A           +13.3s   ← turn boundary
inbox msg delivered as TURN B    +13.3s   ← gated to the boundary, NOT mid-turn
TURN B reply: "I ran sleep 6."   +15.5s   ← processed, context intact across the queue
```

Verdict: `written +3.0s < turnA-end +13.3s <= delivered +13.3s` → gated. The message
sat in the inbox 10s without interrupting the turn, then was consumed at idle; turn B
remembered turn A (session context survives the queued delivery). Routed through the
proxy (`sess=spike-v2-delivery`) — observability intact.

**Implications for the harness:**
- Replaces send-keys + `@waiting` screen-scrape + settle-delay-before-Enter entirely.
- **Simpler than today:** the runner self-gates on `ResultMessage`, so the bridge no
  longer probes `@waiting` to decide when to deliver — it just writes the inbox
  unconditionally; the runner delivers when idle.
- The SDK's "no mid-turn injection" limitation is a non-issue: it *is* the bus's
  idle-gated contract. (`interrupt()` exists if we ever want a true barge-in.)
- "arrives while idle" case is trivial (immediate delivery); V2 proved the harder
  "arrives mid-turn" case. Real loop blocks on an inbox read (FIFO/watch) when idle.

## V3 — `can_use_tool` replaces notify-classify  →  ✅ PASS

Signature: `async can_use_tool(name, input, ctx) -> PermissionResultAllow() |
PermissionResultDeny(message=...)`. Gate (`spike_v3_permission.py`): read-only →
allow inline; mutating → write an out-of-band request and AWAIT an external approver
(`spike_v3_approver.py`, a Slack-approval stand-in), fail-safe deny on timeout.

Prompt ran three Bash commands; filesystem canaries verify enforcement:
```
git status --short        → ran, NO gate call   (CLI built-in read-only auto-approve, before the callback)
touch …ALLOWED-canary     → HOLD → approver:allow → ran     → file created  ✅
touch …DENIED-canary      → HOLD → approver:deny  → blocked → is_error=True, file absent ✅
```

`can_use_tool` fired **exactly twice** — only for the mutating touches. Key results:
- Async human-hold works: the callback `await`s an external decision (no send-keys,
  no exit-code/digit-injection dance). Deny actually blocks execution and the reason
  is surfaced to the model as an error tool_result; it continued.
- **Bonus:** in `permission_mode="default"` the CLI auto-approves known-safe commands
  (`git status`) *before* the callback — so our classifier only weighs in on the
  ambiguous/mutating calls (less work than today's notify-classify, which classifies
  everything). Note: if we want the gate authoritative for ALL tools we'll need to
  bypass that built-in allow (config TBD).
- Do NOT list a tool in `allowed_tools` if you want `can_use_tool` to gate it —
  allow-listing shadows the callback (`CanUseToolShadowedWarning`).
- The classifier here is a cheap prefix heuristic; the real gate `await`s the litellm
  classifier — which is exactly why Python was chosen.

## Spike verdict — all three validations green ✅

| # | Validation | Result |
|---|---|---|
| V1 | Proxy/Langfuse passthrough | ✅ GO (requires `setting_sources=[]`) |
| V2 | Idle-gated delivery w/o send-keys | ✅ PASS (inbox → next turn boundary) |
| V3 | `can_use_tool` replaces notify-classify | ✅ PASS (async human-hold, allow/deny enforced) |

The hybrid+Python SDK harness is **viable**. The SDK deletes the whole
puppet-a-TUI fragility class (send-keys races, `@waiting` scrape, exit-code/digit
permission signalling) and is often *simpler* than today. Proxy/observability,
delivery semantics, and the permission gate all carry over.

**Recommended next step:** build a minimal end-to-end `agent-runner` (one long-lived
`ClaudeSDKClient` in a tmux pane rendering its stream) that unifies V1+V2+V3 into the
real loop: blocking inbox read when idle → deliver at boundary → `can_use_tool` gate
wired to the real litellm classifier + Slack approver. Then decide fleet rollout vs.
keeping it a single opt-in agent. Out of scope until then: install.sh/Taskfile/launchd,
Windows, worktree picker.

## Runner (cut 1) + inbound bus delivery — done

`agent-runner/runner.py`: long-lived `ClaudeSDKClient` in a tmux pane, renders its
stream, self-registers in `~/.tmux/registry/<pane>` (`RUNTIME=sdk` + `INBOX=<path>`),
merges keyboard stdin + inbox file into one turn-boundary-gated queue, gates tools via
`can_use_tool`, MCP (memory) passed explicitly, `system_prompt` preset+append
preserves CLAUDE.md. Smoke-tested green.

**Inbound bus delivery (the bridge→SDK re-wire):** `agent-send.sh` `deliver_local` now
checks the target's registry entry — if it carries `INBOX=`, it appends a framed JSON
record (`{"from","text","ts"}`, python `json.dumps`) to the inbox instead of tmux
send-keys; the runner consumes it at the next turn boundary. The bridge needs NO change
(it already calls `agent-send.sh`), and CLI/TUI agents are unaffected (they keep the
send-keys path, now with the `SLACK_A2A_ENTER_DELAY` settle delay restored). A lone
control digit is dropped for SDK agents (they self-gate). Verified end-to-end: a live
runner received a name-addressed bus message, buffered it, and answered (`6×7 → 42`).
Runner accepts a bare `echo 'hi' >> inbox` too (non-JSON lines = plain text).

Note: `agent-send.sh` flattens newlines before delivery (pre-existing bus behavior), so
inbox messages are single-line for now; preserving multi-line for SDK inboxes (skip the
flatten when `INBOX=` present) is a possible refinement.
