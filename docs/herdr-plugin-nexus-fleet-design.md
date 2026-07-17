# Design: `nexus-fleet` — first herdr plugin (the fleet repo picker)

Status: DESIGN (2026-07-15; probe-verified 2026-07-16). Target: herdr 0.7.3. Owner: Alex.
Supersedes the ad-hoc `[[keys.command]]` picker/bucket bindings in
`~/.config/herdr/config.toml`.

## Probe results (2026-07-16, verified against installed herdr 0.7.3)

Throwaway scratch-plugin `link --disabled` / inspect / `unlink` against the live
server (non-disruptive). Three manifest facts locked; two design assumptions
corrected:

1. **Manifest filename = `herdr-plugin.toml`** (confirmed). A pure-TOML plugin
   links with no `[[build]]` block — no build step needed. (Open Q #5: resolved.)
2. **Pane `placement` ∈ `overlay | split | tab | zoomed` — `popup` is INVALID.**
   The parser rejects it verbatim: *"unknown variant `popup`, expected one of
   `overlay`, `split`, `tab`, `zoomed`"*. Use **`zoomed`** for the fzf UIs (full
   tab that closes on exit — the design's own fallback). (Open Q #1: resolved.)
3. **No direct key→pane.** The keybinding-command `type` enum is
   `shell | pane | plugin_action` (there is no `plugin_pane`), so a key CANNOT
   open a pane directly — the `plugin_action` → `plugin pane open` indirection is
   REQUIRED. Keep the two `[[actions]]`. (Open Q #2: resolved — no.)
4. **CORRECTION — plugin manifests do NOT declare keybindings.** A
   `[[keys.command]]` block inside `herdr-plugin.toml` is *silently ignored* (the
   manifest struct is `panes` / `actions` / `build` / event-hooks / link-handlers —
   no keys field). Keybindings stay in the USER's `config.toml`. So we do NOT move
   the chords into the manifest and do NOT delete them from config — we REPOINT the
   existing config chords from `type="pane"` (running the wrapper) to
   `type="plugin_action"` pointing at the plugin's actions.

Still to verify during the attended build: does a `zoomed` plugin pane auto-close
when its command exits (test-plan step 3/4), and does a plugin `plugin_action`
keybinding displace herdr's built-in default on the same chord (Open Q #3).

The manifest / config.toml sections below are updated to match.

## PREREQUISITE (do before the plugin): make the spawn chain relocatable

Decision (2026-07-15): the pre-work bar is **Relocatable (team-install)** — the
plugin must run on any machine that has the agents-nexus checkout, at any path,
for teammates. NOT full standalone (bare-claude, no nexus) — that's a later,
separate plugin.

**Why this blocks the plugin:** the picker is `exec launch-claude.sh`, which pulls
the entire spawn spine, and that spine hard-codes `$HOME/.tmux` (**85 literal
refs across 23 scripts**; 37 in the 4 spawn-chain scripts alone). A teammate who
`herdr plugin install`s the plugin gets scripts that `source $HOME/.tmux/env.sh`
— a file that doesn't exist for them, and when it does it carries *this machine's*
absolute paths. So the plugin can't work off-fleet, and the publish/share probe
(its headline reason to go first) would be testing a fiction. Fix the seam first.

### Spawn-chain dependency map (what the picker drags in)

```
launch-claude.sh   (15× $HOME/.tmux)  ── picker entry (exec)
  ├─ source ~/.tmux/env.sh            ← REAL file, machine-specific values
  ├─ source ~/.tmux/agent-resolve.sh  (2×)  → reads ~/.tmux/registry/
  ├─ call   ~/.tmux/substrate.sh      (7×)  workspace-list/spawn/pane-alive
  │            └─ ~/.tmux/registry/, ~/.tmux/workspace-categories.txt
  ├─ read   ~/.tmux/registry/         running-agent dedup
  └─ spawn→ ~/.tmux/open-claude.sh    (13×) the injection pipeline:
                ├─ source ~/.tmux/env.sh (again)
                ├─ load  $AGENTS_NEXUS_DIR/.env   (DATABASE_URL, bus)
                ├─ ~/.tmux/substrate.sh rename/register/tag
                ├─ ~/.tmux/memory-hook.py, memory-recall.py
                ├─ ~/.tmux/cache/<slug>.md
                ├─ ~/.tmux/agent-registry.sh, agent-send.sh
                └─ writes ~/.claude.json (trust seed)
```

### Prereq step 1 — the `NEXUS_TMUX_DIR` seam (the true blocker; small)

Introduce `NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"` and replace
`$HOME/.tmux` → `$NEXUS_TMUX_DIR` across the spawn chain (and, for consistency,
the rest of `tmux-scripts`). Mechanical; `env.sh` already uses this exact
`${VAR:-default}` idiom, so it's the house pattern. Result: the tree is
*relocatable* — install anywhere, point one env var at it. This is also exactly
what the Tier 4 Linux/systemd port needs, so it is not single-use work.

- Set/export `NEXUS_TMUX_DIR` in `env.sh` (single source of truth), and have each
  script fall back to the default so a script run before `env.sh` is sourced still
  resolves. Scripts that source `env.sh` inherit it; the few that run standalone
  (hooks) get the `${VAR:-$HOME/.tmux}` default.
- Scope: `launch-claude.sh`, `open-claude.sh`, `substrate.sh`, `agent-resolve.sh`
  first (the picker's chain), then sweep the remaining ~19 for parity so a future
  relocation is total. Keep it one commit for the chain + one for the sweep.

### Prereq step 2 — graceful degrade in `open-claude.sh` (½ day)

Make the nervous-system injections optional so a teammate without the full stack
still gets a *working agent*, just fewer bells:

- **`env.sh` split:** commit a `env.defaults.sh` (portable `${VAR:-...}` defaults)
  + keep machine-specific values in a git-ignored `env.local.sh` (or the existing
  `.env`). `env.sh` sources defaults then local. Removes this machine's absolute
  paths from the shared artifact.
- **Guard the injectors:** confirm each of memory-hook/recall, cache read,
  `.env`/`DATABASE_URL`, trust-seed, agent-registry/send is `[ -x ]`/`[ -f ]`
  guarded and *warn-not-fail* when absent. Most already are; the gap is `env.sh`
  being load-bearing. `substrate rename/register` should no-op cleanly if the
  registry dir isn't writable.
- Acceptance: on a scratch `NEXUS_TMUX_DIR=/tmp/nexus-test` with no `.env` and no
  mnemon venv, `open-claude.sh` still spawns a usable claude (logs skips, exits 0).

### Prereq acceptance gate (before starting the plugin)

`NEXUS_TMUX_DIR=/tmp/fleet-relo` + a fresh checkout path →
`launch-claude.sh` renders the picker and spawns an agent through substrate, with
memory/bus present-or-cleanly-skipped. Green here = the plugin is honestly
installable by a teammate. Only then build `plugins/nexus-fleet/`.

Out of scope for this prereq (explicitly): bare-claude standalone with no nexus
checkout at all → that's the future "public marketplace" plugin, not this.

---

## Why this one first

Lowest-risk, highest-frequency surface, and a *port* not a build:

- The picker (`nexus-pick.sh`) and bucket creator (`nexus-workspace-new.sh`)
  already exist, are deployed, and are dogfooded daily. We convert trusted code
  against a new API instead of debugging new logic + new API at once.
- Spawning an agent into a repo is the first thing done every session — best
  value-per-line, and the most legible unit for the "nexus herdr kit" teammates
  install.
- It doubles as the **publish-flow probe** (the herdr plugin dev/publish path was
  previously unverified) on a self-contained artifact.
- It **closes Tier 3** (keybinding collisions): manifest keybindings displace
  herdr's built-in defaults on `prefix+shift+n` / `prefix+shift+b` with no
  dual-attach (0.7.x behavior), so the chord-shadow goes away as a side effect.

Non-goal for v1: full portability to a machine without the nexus fleet installed.
This plugin depends on the nexus substrate layer (`~/.tmux/launch-claude.sh`,
`substrate.sh`, `open-claude.sh`, registry, env). Audience = teammates installing
the whole kit. Decoupling is a later plugin's problem.

## API facts (verified against installed herdr 0.7.3)

- `herdr plugin install <owner>/<repo>[/subdir] | link <path> | list | enable |
  disable | unlink | action <list|invoke> | pane <open|focus|close> | log list`
- A plugin is a directory with a `herdr-plugin.toml` manifest.
- **Actions run headless** (command logs; no interactive TTY). **Panes are real
  terminals** — `placement` ∈ `overlay|split|tab|zoomed` (NOT `popup`; verified
  2026-07-16, see Probe results). Use `zoomed` for the fzf UIs.
- Keybindings live in the USER's `config.toml`, **not** the manifest (a manifest
  `[[keys.command]]` is silently ignored). Bind a key to a plugin action with
  `[[keys.command]] type = "plugin_action", command = "<plugin_id>.<action_id>"`.
  The key-command `type` enum is `shell | pane | plugin_action` — there is no
  `plugin_pane`, so a key cannot open a pane directly.
- Injected env: `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`, `HERDR_ENV=1`,
  `HERDR_PLUGIN_ID`, `HERDR_PLUGIN_ROOT`, `HERDR_PLUGIN_CONFIG_DIR`,
  `HERDR_PLUGIN_STATE_DIR`, `HERDR_PLUGIN_CONTEXT_JSON`, `HERDR_WORKSPACE_ID`,
  `HERDR_TAB_ID`, `HERDR_PANE_ID`, `HERDR_PLUGIN_ACTION_ID`,
  `HERDR_PLUGIN_ENTRYPOINT_ID`.
- Marketplace publish = public GitHub repo + topic `herdr-plugin`;
  `herdr plugin install owner/repo[/subdir]`.

## Control flow

```
prefix+shift+n
  └─ [[keys.command]] type=plugin_action → "nexus.fleet.pick"
       └─ action `pick` (headless): herdr plugin pane open
            --plugin nexus.fleet --entrypoint picker
              └─ [[panes]] picker (popup, real TTY): bin/pick.sh
                   └─ exec $NEXUS_TMUX_DIR/launch-claude.sh   (fzf repo/worktree UI)
                        └─ substrate.sh (NEXUS_SUBSTRATE=herdr) → herdr agent start
                             └─ open-claude.sh (checkpoint/memory/context inject)
   popup closes when launch-claude.sh returns
```

`prefix+shift+b` is the same path → action `bucket` → pane `bucket` →
`bin/workspace-new.sh` (fzf category/slug → `substrate.sh workspace-create`).

Why the action indirection: keys bind to `plugin_action`; the interactive UI must
run in a pane. The action is a one-liner whose only job is to open the popup.
(If 0.7.3 turns out to support a direct key→pane type, we drop the two actions and
bind the panes directly — see Open Questions.)

## Layout (in-repo, self-contained)

```
plugins/nexus-fleet/
  herdr-plugin.toml
  bin/
    pick.sh            # popup entrypoint → wraps launch-claude.sh
    workspace-new.sh   # popup entrypoint → bucket creator (ported from current)
  README.md            # install/link, keys, rollback
```

The `bin/*.sh` are the current `~/.config/herdr/*.sh` wrappers, moved into the
plugin and made root-relative via `$HERDR_PLUGIN_ROOT`. Nexus home is resolved as
`NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"` so a non-default install can
override it without editing the plugin.

## `herdr-plugin.toml`

```toml
id = "nexus.fleet"
name = "Nexus Fleet"
version = "0.1.0"
min_herdr_version = "0.7.3"
description = "agents-nexus fleet UX for herdr: fuzzy repo picker + workspace buckets, spawning through the nexus substrate seam (checkpoint/memory/context injected)."
platforms = ["macos", "linux"]

# --- Interactive UIs run as popup panes (real TTY for fzf) ---
[[panes]]
id = "picker"
title = "Fleet repo picker"
placement = "zoomed"
width = 0.6
height = 0.6
command = ["/bin/bash", "-c", "exec \"$HERDR_PLUGIN_ROOT/bin/pick.sh\""]

[[panes]]
id = "bucket"
title = "New workspace bucket"
placement = "zoomed"
width = 0.5
height = 0.4
command = ["/bin/bash", "-c", "exec \"$HERDR_PLUGIN_ROOT/bin/workspace-new.sh\""]

# --- Headless actions: open the popup above ---
[[actions]]
id = "pick"
title = "Fleet repo picker"
contexts = ["workspace"]
command = ["/bin/sh", "-c", "\"$HERDR_BIN_PATH\" plugin pane open --plugin nexus.fleet --entrypoint picker"]

[[actions]]
id = "bucket"
title = "New workspace bucket"
contexts = ["workspace"]
command = ["/bin/sh", "-c", "\"$HERDR_BIN_PATH\" plugin pane open --plugin nexus.fleet --entrypoint bucket"]

# --- Keybindings: NOT declared in the manifest ---
# CORRECTION (probe 2026-07-16): a [[keys.command]] block here is silently ignored
# — plugin manifests don't own keybindings. The chords stay in config.toml,
# repointed to the actions above. See the config.toml section below.
```

## `bin/pick.sh`

```bash
#!/usr/bin/env bash
# herdr popup entrypoint for the fleet repo picker.
# Thin wrapper: robust PATH (herdr server env is stripped) + force the herdr
# substrate backend, then hand to the real launch-claude.sh (fzf repo/worktree
# picker → spawns via open-claude.sh so checkpoint/memory/context is injected).
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export NEXUS_SUBSTRATE=herdr
NEXUS_TMUX_DIR="${NEXUS_TMUX_DIR:-$HOME/.tmux}"
exec "$NEXUS_TMUX_DIR/launch-claude.sh"
```

`bin/workspace-new.sh` = the current `nexus-workspace-new.sh` verbatim, with the
same `NEXUS_TMUX_DIR` indirection for the `env.sh`/`agent-resolve.sh`/`substrate.sh`
source paths.

## `config.toml` change (the Tier 3 cleanup)

CORRECTION (probe 2026-07-16): keybindings can't live in the manifest, so we
**repoint** (not remove) the two existing `[[keys.command]]` blocks in
`tmux/mac/herdr/config.toml` — from `type="pane"` (running `nexus-pick.sh` /
`nexus-workspace-new.sh`) to the plugin actions:

```toml
[[keys.command]]
key = "prefix+shift+n"
type = "plugin_action"
command = "nexus.fleet.pick"

[[keys.command]]
key = "prefix+shift+b"
type = "plugin_action"
command = "nexus.fleet.bucket"
```

Keep `[keys]` prefix/focus_agent/switch_tab and all `[ui]`. The fleet *actions* +
*panes* move into the plugin; the chords stay in config but now dispatch to it.
Mirror the same repoint into `tmux/linux/herdr/config.toml`. (Whether a plugin
`plugin_action` chord displaces herdr's built-in `new_workspace` on `shift+n` is
Open Q #3 — verify during the build.)

`install.sh`: stop symlinking `nexus-pick.sh` / `nexus-workspace-new.sh` into
`~/.config/herdr/`; instead `herdr plugin link` the repo plugin dir (idempotent:
check `herdr plugin list --json` first).

## Test plan (installed 0.7.3, attended)

1. `herdr plugin link plugins/nexus-fleet` → `herdr plugin list` shows
   `nexus.fleet` enabled.
2. `herdr plugin action list` → `pick`, `bucket`.
3. Pane direct: `herdr plugin pane open --plugin nexus.fleet --entrypoint picker`
   → popup opens, fzf renders, arrow/type works (TTY OK).
4. Pick a repo → confirm the agent spawns **through substrate** (herdr agent),
   lands in a workspace, is registered, and got context injected (checkpoint line
   in the new pane).
5. Action path: `herdr plugin action invoke nexus.fleet.pick` → same popup.
6. `herdr server reload-config`; press `prefix+shift+n` and `prefix+shift+b` →
   both fire; confirm `prefix+shift+n` no longer also triggers herdr
   `new_workspace` (displacement verified).
7. `herdr plugin log list --plugin nexus.fleet` → clean invocation log.
8. Bucket path end to end (category/slug fzf → `workspace-create` → id printed).

## Rollback

`herdr plugin disable nexus.fleet` (or `unlink`) and restore the two
`[[keys.command]]` blocks in `config.toml` + `herdr server reload-config`. Fully
reversible; no daemon/launchd changes.

## Distribution

- **Internal (the kit):** `herdr plugin link <agents-nexus checkout>/plugins/nexus-fleet`
  — works from the GitLab checkout teammates already have. This is the v1 path.
- **Marketplace (optional, later):** needs a *public GitHub* repo + topic
  `herdr-plugin` (agents-nexus is GitLab-origin, so this would be a dedicated
  public mirror of just the plugin subdir). Defer until the plugin is nexus-
  decoupled — otherwise a stranger installs something that assumes `~/.tmux`.

## Open questions (verify against 0.7.3 before/while building)

1. **popup TTY + auto-close:** does `placement="popup"` give fzf a full TTY, and
   does the popup close when its command exits? (Design assumes yes; step 3/4
   proves it. Fallback: `placement="zoomed"` tab that we close explicitly.)
2. **direct key→pane:** is there a `[[keys.command]]` `type` that opens a pane
   entrypoint directly (skipping the action)? If so, drop the two `[[actions]]`.
3. **plugin-key displacement parity:** confirm a *plugin* keybinding displaces a
   built-in default the same way a *config* keybinding does (docs say user
   keybindings do; plugin keys should count once enabled).
4. **action cwd:** confirm action/pane commands run with a predictable cwd; we
   rely on `$HERDR_PLUGIN_ROOT`, not cwd, so this is only a belt-and-suspenders
   check.
5. **build step:** none needed (pure bash) — `[[build]]` omitted. Confirm link
   works with no build.
```
