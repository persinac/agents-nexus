# nexus-observe plugin

Observability panels for the agents-nexus fleet on herdr, opened as split-alongside
dashboards.

**Keybindings** (wired by the installer):
- `prefix+shift+m` — live memory-system health (Postgres/CNS stats; refreshes)
- `prefix+shift+a` — APM / fleet stats (your APM, agent APM, active agents, today's
  totals; refreshes every ~2s)

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

Both open as a right split (`--direction right`); close with your herdr pane-close key.

## Rollback

```bash
herdr plugin disable nexus.observe
# remove the "# >>> nexus-plugin:nexus.observe keys >>>" ... "<<<" block from ~/.config/herdr/config.toml
herdr server reload-config
```
