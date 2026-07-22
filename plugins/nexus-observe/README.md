# nexus-observe plugin

Observability panels for the agents-nexus fleet on herdr, opened as split-alongside
dashboards.

**Keybindings** (wired by the installer):
- `prefix+shift+m` — live memory-system health (Postgres/CNS stats; refreshes)
- `prefix+shift+a` — APM / fleet stats (your APM, agent APM, active agents, today's
  totals; refreshes every ~2s)
- `prefix+shift+f` — keyword search ("find") over the agent-memory notes store
  (interactive prompt; ports the dashboard "search notes" feature)
- `prefix+shift+o` — command center ("ops"): refreshing fleet health + agents +
  services + timers (ports the dashboard command-center grid)

## Install (opt-in)

From the agents-nexus checkout root, after the base install:

```bash
scripts/herdr-plugin-install.sh nexus-observe
```

Links the plugin **and** appends the two chords to `~/.config/herdr/config.toml`
(idempotent).

## Panels

- **Memory health** (`bin/memory-panel.sh` → `memory-status.sh` → `memory-status.py`):
  a live TUI reading `DATABASE_URL` from the agent-memory `.env`. Degrades to a
  "DATABASE_URL not set" view if the DB is unreachable.
- **Fleet APM** (`bin/apm-panel.sh` loops `stats.sh`): APM counters, active-agent count
  (via the substrate seam — herdr or tmux), and today's totals, from
  `$NEXUS_TMUX_DIR/apm.log`.
- **Memory search** (`bin/memory-search-panel.sh` → `memory-search.py`, rendered by
  `bin/memory-search-render.py`): an interactive keyword search over `agents.memory_nodes`
  (embedding-free; the semantic path stays in the dashboard / `agent_memory.cli search`).
  Type a term; prefix with `p:<project>` or `all:` to scope; `:q` to quit. Self-loads
  `DATABASE_URL` from the repo `.env` the same way the MCP server does.
- **Command center** (`bin/command-center-panel.sh`): a refreshing TUI (default 5s,
  `NEXUS_CC_REFRESH` to override) porting the dashboard command-center grid — health dots
  (docker / substrate / DB), the live agent roster (`substrate.sh query`), running nexus
  containers (`docker ps`), and installed timers (launchd / systemd). Read-only; composes
  the existing fleet primitives, no arbiter dependency. Ctrl-C / pane-close to exit.

All open as a right split (`--direction right`); close with your herdr pane-close key.

## Rollback

```bash
herdr plugin disable nexus.observe
# remove the "# >>> nexus-plugin:nexus.observe keys >>>" ... "<<<" block from ~/.config/herdr/config.toml
herdr server reload-config
```
