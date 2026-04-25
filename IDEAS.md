# Ideas & Roadmap

## High impact — low effort

1. ~~**Windows toast notifications**~~ — removed; red status bar indicator (item 3) is sufficient
2. **Batch approve/reject** — done: `qa` function (e.g., `qa 1` to approve all waiting agents)
3. **Wait duration in status bar** — done: three-state color system (green=working, grey=idle, red+timer=needs input) via `PreToolUse`, `Stop`, and `Notification` hooks
4. **CLAUDE.md files per repo** — done: `CLAUDE.md.template` + `claude-init` command to scaffold

## Medium effort — multiplier effects

| # | Priority | Idea | Status |
|---|----------|------|--------|
| 5 | 10 | **Session templates** — predefined layouts for common workflows (e.g., `work-stack frontend backend tests`). One command to set up a whole workstream | backlog |
| 6 | 1 | **Git worktree integration** — auto-create worktrees when spawning agents in the same repo, so two agents can work on different branches without conflicts | done |
| 7 | 5 | **Agent summary on peek** — enhance `v()` to parse the last Claude output and show a one-line status instead of raw terminal output | done |
| 8 | 5 | **Stuck agent detection** — if an agent's been "running" (green) for >10min with no tool use logged, flag it yellow | done |
| 9 | — | **Agent-to-agent messaging** — `/msg <slot> <message>` slash command + `agent-send.sh` script | done |

## Bigger bets

| # | Priority | Idea | Status |
|---|----------|------|--------|
| 10 | 3 | **Agent handoff / chaining** — when agent 2 finishes, auto-send its summary to agent 3. Pipeline: write → review → test | |
| 11 | 2 | **Searchable history & agent memory** — central knowledge store across all devices. Design: [`searchable-history-design.md`](docs/searchable-history-design.md) | done |
| 11a | 2 | ↳ Central Postgres schema (`memory_events` + `memory_nodes` + `memory_links` + `memory_entities`) + MCP server with basic CRUD tools | done |
| 11b | 2 | ↳ Local wrapper + CF tunnel + tmux hook integration (auto-capture events from `Stop`/`PreToolUse`/`Notification` hooks) | done |
| 11c | 3 | ↳ Entity extraction + backlinks ("everything about auth-module") + tag taxonomy | done |
| 11d | 4 | ↳ pgvector embeddings on notes + semantic search (MAGMA-style anchor identification) | done |
| 11e | 5 | ↳ Temporal edges | done |
| 11g | 6 | ↳ Auto-inject knowledge into agent prompts (`/recall` + MAGMA retrieval + token-budgeted context) | done |
| 12 | 4 | **Cost/token dashboard** — track token usage per agent per session alongside APM | |
| 13 | 7 | **Auto-routing** — when an agent goes red, automatically `v` it in a small persistent pane at the bottom | |
| 14 | 2 | **Pixel agents dashboard** — fork [pixel-agents](https://github.com/pablodelucca/pixel-agents) React webview into a standalone Electron/web app. Replace VS Code postMessage with WebSocket fed by tmux hook data. Animated pixel art characters show agent state (typing/reading/waiting/idle) in a fun office visualization. Source: `webview-ui/` in the pixel-agents repo. | done |
| 15 | 2 | **Agent awareness** — agents should know about other running agents (window slot, repo, branch, status) and be able to use `/msg` to request info from them. Could be a CLAUDE.md snippet or a hook that injects context on session start. | done |
| 16 | 6 | **Causal inference** — slow-path LLM batch over note pairs to infer why changes happened. Anthropic Message Batches API. Run after session end. Defer until ~50+ notes in corpus. | |
| 17 | 6 | **Knowledge graph web UI** — interactive visualization of memory nodes + links. Orthogonal views (temporal/causal/semantic/entity). Extend pixel-dashboard. Build after causal inference produces links worth visualizing. | |
| 18 | 5 | **Versioned agentic scheduled jobs** — add a `jobs/` directory pattern: each job is a folder with a plist + script (e.g. `obs-digest`, `obs-tidy`). Installer links scripts to `~/.local/bin` and plists to `~/Library/LaunchAgents`. Makes it easy to add new Claude-powered cron-style jobs. | |
| 19 | 4 | **Multi-org GitHub token rotation for Spark** — support multiple GitHub PATs in config (keyed by org), so Spark enrichment (descriptions, PRs) works across `persinac`, `lockfale`, `flippin-balls` without needing a single god-token. Fine-grained PATs are scoped per-org. | |
