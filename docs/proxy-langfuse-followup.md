# Proxy + Langfuse — open follow-ups (2026-04-30)

## What was just fixed (committed-pending in main worktree)

1. **Trace input/output were blank in the Langfuse UI.**
   `proxy/main.py` was creating `lf.trace(name="claude-code")` with no input/output and only setting them on the child generation. The trace-list view reads from the trace row, so 100% of traces appeared empty. Fixed by setting `input`/`output` on the trace itself in `_emit_trace`.

2. **Streaming generations had NULL output ~85% of the time** (112/131 in the day before the fix).
   The non-streaming path (`messages`) had a `output or response` fallback so all 98/98 of those rows were populated. The streaming path (`messages-stream`) only collected `delta.type == "text_delta"` — so any turn that was pure tool calls or thinking ended up empty, and the SDK serialized the empty string as NULL. Fixed by reconstructing the full block list (`text`, `thinking`, `tool_use` with parsed `input_json_delta` buffer) and emitting a structured dict when there's any non-text content. `_summarize_blocks` + `_pick_output` handle both code paths uniformly.

3. **No sessions in Langfuse.**
   `session_id` was never passed. Now the proxy strips an optional `/sess/<name>/` prefix from the request path and uses it as `trace.session_id`. The launcher (`~/.tmux/open-claude.sh`) appends `/sess/$MY_NAME` to `ANTHROPIC_BASE_URL` after the tmux window name is resolved, so each window becomes its own Langfuse session.

4. **`LANGFUSE_HOST=http://localhost:3000` in shell env was leaking into the proxy container.**
   Docker Compose's `${VAR:-default}` substitution prefers shell env over `.env`, so the container got `localhost:3000` and couldn't reach Langfuse. The work compose (`docker-compose.work.yml:189`) already hardcoded it; the personal `docker-compose.yml:137` did not. Hardcoded the proxy block to `http://langfuse-web:3000` to match.

## Verified locally

After rebuild, a manual test trace landed in ClickHouse with input/output/session_id all populated. Real Claude Code turns are now writing populated input + structured output (`{text, thinking, tool_uses}`); session_id will populate as soon as a Claude Code session is launched after the launcher edit.

## Still uncommitted

In `/Users/alex.persinger@getgarner.com/garner/repos/agents-nexus` (main worktree):

- `proxy/main.py` — modified
- `docker-compose.yml` — modified (LANGFUSE_HOST hardcode for proxy block)
- `proxy/` itself was untracked before this session

Outside the repo:

- `~/.tmux/open-claude.sh` (Mac) — followed the symlink and edited the file in `claude-agents-tmux/mac/tmux-scripts/open-claude.sh`. **Untracked there.**

## The launcher fragmentation problem

There are at minimum three copies of `open-claude.sh`, and they were already drifted before this session:

| Copy | Path | Status after this session |
|---|---|---|
| Mac runtime | `~/.tmux/open-claude.sh` → `claude-agents-tmux/mac/tmux-scripts/open-claude.sh` | **Edited** (4-line `ANTHROPIC_BASE_URL` append after `MY_NAME` is set) |
| Mac install | `agents-nexus/tmux/mac/tmux-scripts/open-claude.sh` | Out of date with the runtime copy. Not edited. |
| Windows install | `agents-nexus/tmux/windows/tmux-scripts/open-claude.sh` | Not edited. |
| Linux install | `agents-nexus/tmux/linux/tmux-scripts/open-claude.sh` | **Doesn't exist.** Only `hook-notification.sh` is there. |

So the mini-PC (Linux) is launching Claude Code from somewhere that is not under the agents-nexus repo as currently structured. Need to find out where before applying the same edit.

## Mini-PC checklist (for whenever you pick this up)

1. **Pull + rebuild** on the mini-PC:
   ```bash
   cd ~/garner/repos/agents-nexus    # or wherever it lives there
   git pull
   docker compose -f docker-compose.work.yml up -d --build proxy
   # (or whichever compose file the mini-PC actually runs)
   ```
   The `proxy/main.py` and `docker-compose.yml` changes will land here.

2. **Find the mini-PC launcher**:
   ```bash
   readlink -f ~/.tmux/open-claude.sh
   ```
   Apply the 4-line edit (after `MY_NAME` is computed):
   ```bash
   if [ -n "$ANTHROPIC_BASE_URL" ] && [ -n "$MY_NAME" ]; then
     export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/}/sess/${MY_NAME}"
   fi
   ```

3. **Restart any long-running Claude Code sessions** to pick up the new `ANTHROPIC_BASE_URL`. Existing sessions are still hitting `/v1/messages` directly without the `/sess/<name>` prefix, so they'll keep producing NULL session_id traces until restarted.

4. **(Optional cleanup, separate task)** Decide whether `claude-agents-tmux` or `agents-nexus/tmux/<os>/` is the source of truth for `open-claude.sh`, sync the three copies, drop the other location.

## Background notes that may matter later

- The proxy upstream (`http://host.docker.internal:54777/anthropic`) has been unreachable for the entire session — every request fails over to direct `https://api.anthropic.com`. That's expected at home; the corporate Bifrost only resolves on the work network. The failover path is logging cleanly to Langfuse, so observability still works.
- Langfuse v2 SDK is currently pinned at `langfuse==2.59.7` in `proxy/requirements.txt`. v3+ is OTel-based with a different API; the current `lf.trace(...).generation(...)` pattern would need to be rewritten if you upgrade.
- All non-proxy containers (mnemon-mcp, spark, postgres, dashboard, ollama, langfuse-*) are on `agents-nexus-work_default`. The proxy was briefly on a separate personal-compose network during this session — symptom was that resolving `langfuse-web` returned `gaierror`. Always rebuild the proxy with the same compose file as the rest of the stack.
- mnemon-mcp's `LANGFUSE_HOST` (in `docker-compose.yml:102`) still uses the same `${LANGFUSE_HOST:-...}` substitution and would have the same problem if your shell exports `LANGFUSE_HOST=http://localhost:3000`. It's working today only because mnemon-mcp's image was built before the shell-env value was set. Worth hardcoding it for symmetry next time you touch that file.
