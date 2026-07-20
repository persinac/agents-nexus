# nexus-mission plugin

Launch autonomous **Conductor** missions from herdr.

**Keybinding** (wired by the installer): `ctrl+a shift+p` → a launcher pane:
1. fzf pick a mode — **distribute** or **sdlc**
2. enter a goal (distribute) or ticket/goal (sdlc)
3. the Conductor is kicked off

## Modes

- **distribute** → `conductor.py --distribute "<goal>"`: fan-out mission (classify → plan →
  workers into a tiled `mission/<slug>` bucket → verify → synthesize → report to your
  tracker/MR/Slack). Runs **detached**; the launcher returns after dispatch and reports the
  bucket. Works today.
- **sdlc** → `conductor.py --sdlc "<ticket|goal>"`: drives an external `sdlc` plugin's staged
  pipeline (requirements → domain-model → tech-design → validation → **plan.md**, stopping at
  the plan; code phase is left to a human). Runs **foreground** in the launcher pane.
  **Requires** an SDLC plugin installed + the context repos (`project-context-*`) cloned under
  a dir in `CUSTOM_WORKSPACE_ROOTS` (colon-separated; or `sdlc.workspace_root` in
  `conductor.yaml`) — the launcher preflights this and tells you how to set it up if missing.

## Install (opt-in)

```bash
scripts/herdr-plugin-install.sh nexus-mission
```

Links the plugin + appends the chord to `~/.config/herdr/config.toml` (idempotent).

## Rollback

```bash
herdr plugin disable nexus.mission
# remove the "# >>> nexus-plugin:nexus.mission keys >>>" ... "<<<" block from ~/.config/herdr/config.toml
herdr server reload-config
```
