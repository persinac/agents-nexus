# nexus-fleet plugin

Fuzzy repo picker + workspace bucket creator for herdr, powered by the agents-nexus spawn chain.

**Keybindings:**
- `prefix+shift+n` — fleet repo picker (fzf worktree selection → spawn agent)
- `prefix+shift+b` — new workspace bucket (fzf category/slug → create empty bucket)

## Installation

### Prerequisites
- herdr 0.7.3+ installed
- agents-nexus checkout (any path) with `~/.tmux` symlinked or `NEXUS_TMUX_DIR` configured
- fzf available in PATH

### Install (opt-in)

The base install (`tmux/{mac,linux}/install.sh`) deploys a **plugin-free** herdr
config. This plugin is opt-in — install it from the agents-nexus checkout root:

```bash
scripts/herdr-plugin-install.sh nexus-fleet
```

That links the plugin **and** appends its keybindings to `~/.config/herdr/config.toml`
(idempotent — safe to re-run), then reloads. herdr plugin manifests cannot declare
keybindings, so the chords must live in your config; the installer handles that. A
plain `herdr plugin link ./plugins/nexus-fleet` links the plugin but leaves the chords
unbound.

Verify:
```bash
herdr plugin list        # nexus.fleet enabled, version 0.1.0
```

### Configure (optional)

If your agents-nexus checkout is NOT at the default `~/.tmux` location, set:

```bash
export NEXUS_TMUX_DIR=/path/to/your/agents-nexus/tmux-scripts
```

in your shell profile. The plugin falls back to `$HOME/.tmux` if unset.

## Usage

Press `prefix+shift+n` in any herdr workspace to:
1. See a fzf list of repos in the agents-nexus fleet
2. Pick one → agent spawns in a new window with checkpoint/memory/context injected

Press `prefix+shift+b` to:
1. Select a workspace category (e.g., "interactive", "batch")
2. Optionally name a sub-bucket (e.g., "batch/model-train")
3. New empty workspace created and focused

## Rollback

Disable the plugin and restore the old keybindings:

```bash
herdr plugin disable nexus.fleet
# remove the "# >>> nexus-plugin:nexus.fleet keys >>>" ... "<<<" block from ~/.config/herdr/config.toml
herdr server reload-config
```

Or unlink it entirely:

```bash
herdr plugin unlink nexus.fleet
```

## Troubleshooting

**Pane opens but fzf doesn't render or is unresponsive:**
- The picker panes use `placement="zoomed"` (a full tab). `popup` is NOT a valid herdr
  0.7.3 placement — the manifest parser rejects it (`overlay|split|tab|zoomed` only).
- Smoke-test the pane directly: `herdr plugin pane open --plugin nexus.fleet --entrypoint picker`

**Agent doesn't spawn after picking a repo:**
- Check that `launch-claude.sh` exists in `$NEXUS_TMUX_DIR` (default `~/.tmux`)
- Run `herdr plugin log list --plugin nexus.fleet` to see action logs

**Bucket creation fails with "workspace-create not found":**
- Ensure `substrate.sh` exists in `$NEXUS_TMUX_DIR`
- Verify `NEXUS_SUBSTRATE=herdr` is set in the plugin's env (it should be auto-set by `bin/workspace-new.sh`)

## See also

- `docs/herdr-plugin-nexus-fleet-design.md` — full design, API facts, test plan
- `herdr plugin help` — herdr's plugin command reference
