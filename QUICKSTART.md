# Quickstart — try the fleet in ~5 minutes

The fastest path from "I have Claude Code" to "I'm driving a fleet of agents." This is the
**minimal trial**: the multiplexer + the spawn/picker UX, with the heavy knowledge stack
(memory, search, dashboard, Slack) left OFF. Everything degrades gracefully, so it just runs.

For the full setup (persistent memory, semantic search, the dashboard, the Slack bus), see
[`INSTALL.md`](INSTALL.md) and the profile guides once you've kicked the tires here.

## What you need

- **macOS or Linux**
- **Claude Code** — already installed (`claude --version`)
- **git** + a package manager (`brew` on mac, `apt`/etc. on Linux) — `install.sh` uses it to install the deps, **including herdr** (the default backend; you don't install it separately)
- ~5 minutes

You do **not** need Postgres, Docker, or a Slack workspace for the trial.

## The steps

```bash
# 1. Get agents-nexus.
git clone <agents-nexus-repo> && cd agents-nexus

# 2. Run the installer. It installs the deps INCLUDING herdr (the fleet's default backend —
#    a single binary), links the fleet scripts + hooks, deploys the herdr config, and then
#    OFFERS a plugin multi-select (the Fleet picker is pre-checked — just accept the defaults).
#    When it offers the optional Docker knowledge stack, DECLINE it for the trial — the fleet
#    runs fine without it (memory/search/bus cleanly no-op when absent).
./install.sh

# 3. Attach and go.
herdr
```

> Manage plugins any time with `bash scripts/plugin-install-flow.sh` (re-run the picker), or add
> one directly with `scripts/herdr-plugin-install.sh nexus-fleet`.

## Driving it

Inside herdr (prefix is `ctrl+a`):

| Key | Does |
|-----|------|
| `ctrl+a shift+n` | **fuzzy repo picker** → spawns a Claude agent in a new pane, context-injected |
| `ctrl+a shift+b` | new workspace bucket (group related agents) |
| `ctrl+a <1-9>` | jump to agent N |
| `ctrl+a b` | toggle the agent sidebar (your fleet at a glance) |
| `ctrl+a v` / `ctrl+a -` | split panes (watch two agents side by side) |

Spawn one agent, give it a task, spawn another in a different repo — that's the fleet.

## Verify it's wired up

```bash
herdr plugin list                 # nexus.fleet → enabled
curl -sf localhost:8788/health    # (only if you enabled the Slack bus — otherwise skip)
```
Then press `ctrl+a shift+n` and pick a repo — an agent should spawn in a new pane.

## What the trial does NOT include (and how to add it later)

| Feature | Trial | Add via |
|---------|:-----:|---------|
| Spawn/manage/observe agents, context injection, picker | ✅ | (included) |
| Persistent cross-session **memory** (Postgres/mnemon) | ❌ | the Docker stack — `INSTALL.md` |
| Semantic **codebase search** (spark) | ❌ | the Docker stack — `INSTALL.md` |
| Pixel **dashboard** | ❌ | the Docker stack — `INSTALL.md` |
| **Slack bus** (A2A + control from Slack) | ❌ | `./install.sh --finish-slack` |

None of these block the trial — the fleet notices they're absent and skips them.

## Rolling back

herdr sessions persist and are self-contained; to stop, just close the agents (or `ctrl+a q`
to detach). To remove the picker: `herdr plugin unlink nexus.fleet`. Nothing was installed
system-wide beyond the deps `install.sh` reported.

## Prefer tmux?

herdr is the default and the smoothest path. The tmux backend is still fully supported as a
fallback (`NEXUS_SUBSTRATE=tmux`) if you already live in tmux — see [`INSTALL.md`](INSTALL.md).
