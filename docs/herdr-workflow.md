# herdr workflow, organization & plugins — planning doc

**Status:** planning / to tackle next session. The herdr migration is functionally
live on the mac (see `docs/herdr-spike.md` + commits `d17c7c2`→`6831567`); this doc
is about the *workflow rethink* now that agents live in herdr's hierarchy instead of
a flat tmux window list.

## Goals

1. **Get more efficient** — rework how agents are organized + navigated so the herdr
   fleet is faster to drive than the old tmux one, not just equivalent.
2. **Build herdr plugins** — package the nexus fleet UX (picker, mission layouts,
   Slack surfacing, context injection) as herdr plugins.
3. **Share with teammates** — make the nexus herdr setup installable by the team.

---

## Current tmux world (what we're moving from)

**Structure:** one `agents` tmux session. Each agent = a full-screen **window**; a
flat, numbered list. The **status bar is the minimap** — every window color-coded by
`@waiting` (green=working, red=needs-input, grey=idle) + an APM bar. One agent visible
at a time; you jump between them.

**Hotkeys** (prefix `ctrl+a`):

| Key | Action |
|---|---|
| `ctrl+a <1-9>` | jump to window N |
| `ctrl+a n` | new claude window (prompt for dir) |
| `ctrl+a N` | fuzzy repo picker → worktree-aware spawn via `open-claude.sh` (context injected) |
| `ctrl+a G` | general (no-repo) session |
| `ctrl+a \|` / `ctrl+a -` | split window h / v |
| `ctrl+a w` / `ctrl+a s` | window tree / session tree |
| `ctrl+a W` | fuzzy worktree prune |
| `ctrl+a A` | APM stats popup |
| `ctrl+a M` | memory-health panel toggle |
| `ctrl+a r` | reload tmux.conf |
| tmux defaults | `d` detach, `z` zoom, `[` copy-mode |

**Shell helpers** (plain commands, not prefixed):

| Cmd | Action |
|---|---|
| `work [session]` | attach/create the `agents` session |
| `v <N>` | peek popup: last 30 lines of agent N (no switch) |
| `q <N> <msg>` | queue/send a message to agent N |

**Workflow:** `work` → `ctrl+a N` spawn (fzf repo/worktree, context-injected) →
`ctrl+a <n>` hop between agents (status bar shows who needs you) → `v`/`q` peek/nudge
without switching → overseer reaps idle agents (>4h, checkpointed; `@keep`/`@cohort`
protect) → Slack bus to message agents by name.

---

## The herdr world (what we're moving to)

**Structure:** headless **server** + a **client** you attach with `herdr`. Hierarchy is
**workspace → tab → pane**; an agent runs in a **pane**. The **sidebar** is a persistent
overview (all agents, grouped, native semantic state — no `@waiting` scraping). Unlike
tmux you can **tile multiple agents on screen at once**, and the workspace/tab/pane
layout **persists** across client detach/reattach. (A herdr *server* restart is the
exception — it kills the in-pane `claude` processes; `scripts/herdr-recover.sh` brings
them back from their last checkpoint, see `docs/herdr-recover.md`.)

**Hotkeys** (prefix `ctrl+a` — our config + herdr defaults):

| Key | Action | Source |
|---|---|---|
| `ctrl+a <1-9>` | focus agent N (window-jump analog) | our override (`focus_agent`) |
| `ctrl+a N` | fuzzy repo picker (same `launch-claude.sh`) | our `[[keys.command]]` |
| `ctrl+a b` | toggle sidebar (expand/collapse) | herdr |
| `ctrl+a w` | workspace picker | herdr |
| `ctrl+a g` | goto (jump to any pane/agent) | herdr |
| `ctrl+a c` | new tab | herdr |
| `ctrl+a p` / `ctrl+a n` | prev / next tab | herdr |
| `ctrl+a v` / `ctrl+a -` | split pane vertical / horizontal | herdr |
| `ctrl+a h/j/k/l` | move focus between panes | herdr |
| `ctrl+a z` | zoom pane | herdr |
| `ctrl+a shift+g` | new git worktree | herdr |
| `ctrl+a shift+w` / `shift+d` | rename / close workspace | herdr |
| `ctrl+a q` detach · `ctrl+a shift+r` reload config | | herdr |

**Sidebar config (set):** `sidebar_width = 32`, `sidebar_collapsed_mode = "compact"`
(always-on rail), `agent_panel_sort = "priority"` (attention-first),
`show_agent_labels_on_pane_borders = true`. In `~/.config/herdr/config.toml` (→ repo
`tmux/mac/herdr/config.toml`).

**⚠️ Known keybinding collisions to resolve:**
- Our picker on `ctrl+a N` **shadows herdr's default `new_workspace`** (`prefix+shift+n`).
- We pointed `1-9` at `focus_agent`, disabling `switch_tab` on those keys.
Decide rebindings once the org model is chosen (below).

---

## Organizing agents in herdr (the decision to make)

tmux was a flat window list; herdr is a hierarchy + sidebar. Three models:

1. **One agent per workspace** — closest to one-window-per-agent. Each picker spawn =
   its own workspace; `ctrl+a <1-9>` / sidebar hops between them. Familiar, flat feel.
2. **Tabs within themed workspaces** — group related agents (e.g. a `search` workspace
   with `example-service`/`svc-authz` tabs). `ctrl+a w` picks workspace, `ctrl+a p/n` tabs.
   Scales when there are many agents.
3. **Tiled panes for a working set** — split several agents into one tab so you *watch
   them at once* (tmux couldn't). Ideal for a Conductor mission: orchestrator + workers
   tiled in a `mission` workspace, all visible.

**Recommendation:** model **1 for interactive/ad-hoc agents** (keeps `ctrl+a <n>` +
sidebar-as-minimap) + model **3 for Conductor missions** (tiled worker view per
mission — a genuine upgrade the flat-window model couldn't give).

**Execution-framework angle:** the Conductor currently fans workers into separate
windows you can't see together. In herdr a mission could be its own workspace with the
workers tiled as panes — you watch the fan-out/verify happen live. Worth prototyping.

---

## Workspace buckets — SHIPPED (Inc 0–5)

The organization model above is now implemented behind the substrate seam (commits
`f838b95`→). A **workspace = a labeled herdr bucket**; agents carry a `WORKSPACE=` field.

- **Spawn into a bucket:** `substrate spawn <name> <cwd> <cmd> --workspace <label> [--split
  right|down]` — resolve-or-creates the labeled bucket, then `herdr agent start --workspace
  <id>`. tmux degrades gracefully (records the label, keeps flat windows). Verbs:
  `workspace-create` / `workspace-list` / `workspace-close <label|id>` / `workspace-of <pane>`.
- **Picker asks the bucket:** `ctrl+a N` (herdr) → pick an existing live workspace, `[new
  bucket…]` (category from `config/workspace-categories.txt` — `interactive` is one shared
  bucket, `mission`/`swarm` are `<category>/<slug>`), or `[flat]`. `ctrl+a shift+b` makes an
  empty bucket. Gate off with `NEXUS_WORKSPACES=0`; tmux `ctrl+a N` is byte-identical (no prompt).
- **Addressing = thin → FQDN → scoped** (`agent-resolve.sh` / `orchestrator.parseAddress`):
  a bare name resolves when unique; `workspace/name` scopes; parsed **right-to-left** with a
  known-host test so it coexists with the legacy cross-PC `host/name` (`mac/general` still
  routes to the bus; `search/example-service` is a local workspace). Labels may contain `/`
  (`mission/db-migrate/agent7`); match is full-label-or-slug.
- **herdr enforces globally-unique names only for agents IT starts** (`agent start` rejects a
  dup) — so bare-name addressing resolves for picker/seam-launched agents. **But the shared
  `interactive` bucket also collects human-launched Claude Code sessions**, which register via
  `hook-sessionstart.sh` and bypass that uniqueness gate, so duplicate names (two `general`s)
  are normal there — NOT impossible. Disambiguate over the bus by **pane handle** (`wN:pN`),
  which is instance-exact (`agent-send.sh --via-slack wQ:pF …`); the FQDN
  (`[host/][workspace/]name`) is organizational + cross-PC scoping, not the collision-resolver.
  See `docs/agent-bus-instance-addressing.md` for the handle-addressing fix.

### Skill-owned isolated workspace (the pattern)

A skill that fans out background agents owns its OWN bucket and tears it down atomically —
two verbs, backend-agnostic:
1. **Spawn** each worker with `substrate spawn <name> <cwd> <cmd> --workspace
   swarm/<slug> --split down` (same label ⇒ resolve-or-create drops them all into one tiled
   bucket).
2. **Teardown** with `substrate workspace-close swarm/<slug>` (herdr closes the bucket; tmux
   kills the `WORKSPACE=`-matched windows).

**Worked example — `/swarm-bg`** (`~/.claude/commands/swarm-bg.md`): reviews an MR in its own
detached session. It used to hardcode `tmux new-window` — so under herdr it wrongly spawned a
tmux window. Now it spawns through the seam into a `swarm/mr<MR>` bucket, following the
caller's substrate, and tears down with `workspace-close`. **Lesson: skills must spawn through
`substrate spawn`, never raw `tmux new-window`** — that's the whole point of the seam.

## Plugins & sharing with teammates

herdr's **CLI + JSON socket API are the plugin surface** — plugins drive herdr the way
an agent does (notifiers, layout presets, link handlers, pickers). Socket methods
include `plugin.link/.list/.enable/.disable/.action.list/.action.invoke/.pane.open`.
The agent skill installs via `npx skills add ogulcancelik/herdr --skill herdr -g`, and
plugins are published/discovered via a GitHub topic. *(Confirm the exact plugin
dev/publish flow at herdr.dev/docs — not yet verified.)*

**Candidate nexus features to package as herdr plugins:**
- **Fleet picker** — the fzf repo/worktree picker (`launch-claude.sh`) as a bound plugin action.
- **Mission-workspace layout** — a layout preset that tiles a Conductor mission (orchestrator + workers).
- **Slack-bus notifier / surfacing** — permission/stop surfacing + bus delivery as a herdr notifier plugin.
- **Context injector** — checkpoint + memory-recall on spawn (today's `open-claude.sh`).
- **Memory-health / APM panel** — the `ctrl+a M`/`ctrl+a A` panels as plugin panes.

**Sharing:** teammates install herdr (`brew install herdr`) + a shared "nexus herdr kit"
(config.toml + the plugins) — installable via the skills mechanism / a GitHub topic.
Open question: which features stay as substrate scripts vs become herdr plugins.

---

## Open decisions / tomorrow's agenda

1. Pick the org model (1 / 3 hybrid recommended) and wire keybindings to it; fix the
   `new_workspace` shadow + `switch_tab` on `1-9`.
2. Prototype a **tiled mission workspace** for the Conductor (model 3).
3. Efficiency pass: which hotkeys/panels to port (`v`/`q` peek/nudge analogs, APM/memory
   panels) — herdr equivalents or plugins.
4. Scope the **first herdr plugin** to build + the **team-sharing** mechanism (kit +
   GitHub topic); verify herdr's plugin dev/publish flow.
5. Finish P4 soak criteria + the mixed-fleet `SUBSTRATE=` per-registry-entry hardening.
6. **Linux parity** — port the herdr setup to the personal Linux stack (`tmux/linux/`);
   see below.
7. **herdr agent env parity** — herdr-spawned agents miss fleet env vars (symptom:
   `/checkpoint`'s agent-memory MCP write fails — `DATABASE_URL` unset). `herdr agent
   start` only passes the `--env` vars we list (PATH/HOME/NEXUS_SUBSTRATE) plus the
   herdr server's (stripped) env; tmux agents inherited the full login-shell env. **Fix:
   source the fleet env at launch in `open-claude.sh`** (`set -a; . "$AGENTS_NEXUS_DIR/.env";
   set +a`, + `env.sh`) so DATABASE_URL + bus config reach the agent *and* its MCP
   servers — rather than enumerating "a shit ton of env vars" into `--env` (brittle).
   Then verify agent-memory + the other MCP servers come up.
8. **Registry parity — every herdr agent writes a `~/.tmux/registry/<handle>` file, ALWAYS.**
   ✅ **DONE for the fleet's own spawners** (the two-roster split is closed for them). Was: only
   `open-claude.sh`-launched agents registered, so seam-spawned agents (Conductor orchestrator +
   workers) were a second roster the substrated daemon saw (`herdr agent list`) but the reaper /
   `peers` / name→handle resolution (which iterate `~/.tmux/registry/*`) did not — so they
   couldn't be reaped or addressed by name.
   - **Shared verbs:** `substrate register <pane> <name> [cwd] [ws]` (the ONE writer of the
     registry format — SLOT/SUBSTRATE backend-derived, WORKSPACE falls back to the pane's bucket)
     + `substrate deregister <pane>`.
   - **Wired:** `open-claude.sh` now calls `register` (was an inline `printf`); the detached
     Conductor registers + tags `@orchestrator` and **deregisters in a `finally`**; each worker
     registers, tags `@cohort mission/<slug>` (so a worker idling between DAG waves isn't reaped
     mid-flight), and deregisters in a `finally`. Headless python panes have no tmux `pane-died`
     hook, so they self-clean — no stale entry (the herdr-restart stale-registry failure mode).
   - **Still TODO:** skill-owned spawners (`swarm-bg`, other `substrate spawn --workspace` skills)
     don't register yet — same `register`/`deregister` pattern applies. When those skills grow a
     headless (non-`claude`) worker, wire the same pair. (claude-command skills that spawn via
     open-claude inherit registration for free.)

## Linux parity (personal stack)

The mac work all lives under `tmux/mac/` + launchd. The personal Linux mini-pc runs
from `tmux/linux/` and uses **systemd + GitHub** (not launchd + the work GitLab), so
this is a real port, not a copy. To reach herdr parity on Linux:

- **Scripts:** mirror the herdr-aware changes into `tmux/linux/tmux-scripts/*`
  (substrate shim + `pane-alive`, the `HERDR_PANE_ID` fold in hooks + open-claude, the
  `agent-send`/`agent-registry` handle+liveness fixes, the unified `launch-claude` picker).
  Much is shared-bash and ports directly; confirm Linux-specific bits (`date`, paths).
- **Services:** create **systemd units** (user services) for the substrated daemon, the
  arbiter, and the bus — the analog of the mac launchd plists. The Linux bus already
  runs from a separate systemd unit (wraps Doppler) per the slack-bridge plist comment.
- **herdr install:** the Linux install path (`curl -fsSL https://herdr.dev/install.sh | sh`
  or the distro package) + the headless server as a systemd unit.
- **Config:** deploy `herdr/config.toml` + `nexus-pick.sh` via the Linux installer.
  herdr is the **repo default** now, so no per-machine flip is needed — set
  `NEXUS_SUBSTRATE=tmux` only to keep a box on the legacy tmux backend.

## Where the migration stands (context for next session)

- **Live on mac (herdr):** substrated daemon + arbiter + slack-bridge under launchd on
  herdr. **herdr is the repo DEFAULT now (flipped 2026-07-16)** — the old per-machine
  `NEXUS_SUBSTRATE=herdr` plist/env overrides are redundant; tmux stays a flag-selectable
  deprecated fallback (`NEXUS_SUBSTRATE=tmux`).
- **Agents first-class:** context injection, hooks (`HERDR_PANE_ID` fold), registration,
  delivery, human-name surfacing all work. Bus delivery to herdr agents verified.
- **Commits (main):** `d17c7c2` daemon+shim · `3156560` arbiter identity · `721cca4` hook
  parity+picker · `bf8672b` bus presence routing · `6831567` identity layer (pane-alive).
- **Restart resilience:** ✅ **DONE** — `scripts/herdr-recover.sh` is herdr's missing
  `pane-died` analog: it reconciles the stale rosters after a server restart and (opt-in)
  respawns lost agents from their last checkpoint. On-demand via `nexus-recover`; the
  unattended Linux box auto-respawns via `herdr-recover.timer`. See `docs/herdr-recover.md`.
- **Remaining:** watch the soak now that herdr is the default; mixed-fleet `SUBSTRATE=`
  field for robust liveness; eventually retire the tmux backend code (kept for now as the
  one-flag rollback path). See agent-memory notes (project `agents-nexus`, tag `herdr`).
