# agents-nexus

Batteries-included toolkit for running **fleets of AI coding agents** on the
[herdr](https://herdr.dev) multiplexer. Spawn Claude Code agents across many repos at once,
give them memory that survives the session, drive them from Slack, and dispatch autonomous
multi-repo missions — from one terminal.

Everything past the multiplexer is **opt-in and degrades gracefully**: no Postgres, no
Docker, no Slack workspace? The fleet notices and skips those paths.

Linux and macOS. (tmux is supported as a fallback substrate; a historical Windows/MSYS2 path
exists but is no longer maintained.)

<!-- TODO: demo.gif — fleet spawn via ctrl+a shift+N, sidebar status transitions, `v 2` peek. -->

## Quick start

**Just trying it?** → [`QUICKSTART.md`](QUICKSTART.md) — a driveable fleet in ~5 minutes, knowledge
stack left off. Start there.

**Full setup:**

```bash
git clone <this-repo> && cd agents-nexus
./install.sh                  # deps (incl. herdr), profile, .env, plugin picker, optional stack
./install.sh --profile work   # or jump straight to a named profile
```

[`INSTALL.md`](INSTALL.md) is the complete installer reference — every flag, every prompt, every
file it writes. Use-case gotchas live in [`README_SETUP_PERSONAL.md`](README_SETUP_PERSONAL.md)
and [`README_SETUP_WORK.md`](README_SETUP_WORK.md).

## What's inside

| Component | What it does |
|---|---|
| **herdr** | The substrate. Spawns agents into panes, tracks `working`/`blocked`/`idle`, pushes status transitions over a local socket. Default backend; tmux still supported via `NEXUS_SUBSTRATE=tmux`. |
| **`substrated`** | Read-path daemon (`:8422`). Holds a cached fleet view and serves it over a tiny HTTP API so hot readers don't spawn a subprocess per read. Subscribes to herdr status **pushes**, so the idle-gate fires the instant an agent goes idle. |
| **plugins** | Four herdr plugins — Fleet, Presence, Observe, Mission — offered as a multi-select after base install. See [Plugins](#plugins). |
| **mnemon** | Agent memory. Layered store (L2 tuplespace → L3 knowledge graph), weighted retrieval, entity extraction, salience decay. Exposed to every agent over MCP. See [mnemon](#mnemon). |
| **slack-bridge** | Agent-to-agent bus and human control plane. Orchestrator + presence + delivery over NATS; spawn, message, and approve agents from Slack. |
| **Conductor** (`agent-runner/`) | Mission orchestrator — turns a task into a *verified* result. Deterministic spine, scoped LLM judgment nodes, verification loop. See [Conductor missions](#conductor-missions). |
| **`proxy`** | Transparent Anthropic pass-through (`:4000`) with per-session upstream routing (work vs. personal) and Langfuse tracing on every `/v1/messages` call. |
| **Langfuse** | Optional observability stack. Traces memory ops (L2/L3 reads, retrieval scoring, archive jobs, entity extraction) and proxy generations. |
| **overlays** | Layer org- or machine-specific config over a generic checkout at install time, without committing it. |

## Architecture

```mermaid
graph TB
  subgraph host["Host"]
    subgraph herdr["herdr (substrate)"]
      agents["Claude Code agents<br/>1..N panes"]
      plugins["nexus plugins<br/>fleet · presence · observe · mission"]
    end
    substrated["substrated :8422<br/>cached fleet view"]
    proxy_svc["nexus-proxy :4000<br/>routing + tracing"]
    agents -->|"MCP stdio"| mnemon_mcp["mnemon MCP"]
    agents -->|"Anthropic traffic"| proxy_svc
    herdr -->|"agent_status push"| substrated
  end

  subgraph bus["Bus"]
    nats["NATS"]
    bridge["slack-bridge<br/>orchestrator"]
    slack["Slack"]
    substrated --> bridge
    bridge <--> nats
    bridge <--> slack
  end

  subgraph conductor["Conductor"]
    mission["classify → plan → provision<br/>→ dispatch → verify → synthesize"]
    mission -->|"dispatch"| nats
  end

  subgraph docker["Docker Compose"]
    pg[("Postgres + pgvector")]
    ollama["Ollama :11434"]
    lf["Langfuse (optional)"]
  end

  mnemon_mcp --> pg
  mnemon_mcp -->|"embeddings"| ollama
  proxy_svc --> lf
  mnemon_mcp --> lf
```

## Plugins

herdr plugins are the fleet's UX layer. The base install deploys a **plugin-free** config;
the installer then offers a multi-select from [`plugins/catalog.toml`](plugins/catalog.toml).

| Plugin | Default | What it gives you |
|---|:---:|---|
| **`nexus.fleet`** | on | `prefix+shift+n` fuzzy repo/worktree picker → spawns a context-injected agent. `prefix+shift+b` creates a workspace bucket. |
| **`nexus.presence`** | on | Desktop toast the instant an agent goes `blocked`. Pure event hook — no keybinding, no daemon, no polling. Deliberately a *different* channel from Slack so it doesn't double-notify. |
| **`nexus.observe`** | on | Split-alongside dashboards: memory health (`shift+m`), fleet APM (`shift+a`), note search (`shift+f`), command center (`shift+o`). Degrades to a status view when the memory DB is unreachable. |
| **`nexus.mission`** | off | Launch Conductor missions from a chord (`ctrl+a shift+p`). Heavier deps — opt-in. |

```bash
bash scripts/plugin-install-flow.sh              # re-run the picker
scripts/herdr-plugin-install.sh nexus-observe    # add one directly
herdr plugin list                                # what's enabled
```

## mnemon

Durable memory for agents, in Postgres, reachable from any session over MCP. Named after the
memory implants in Iain M. Banks' *Culture* novels.

It is **layered, not a flat vector pile**:

- **L2 — tuplespace.** A shared working cache using Linda coordination primitives. Agents on the
  same mission read and write facts here without talking to each other directly.
- **L3 — knowledge graph.** On job completion the archiver promotes L2 facts into durable nodes,
  extracting entities (file paths, `[[wikilinks]]`, `@mentions`) and wiring backlinks.
- **Retrieval** is MAGMA-style weighted scoring — semantic similarity, tag overlap, recency, and
  access frequency — not similarity alone.
- **Decay** reduces salience of stale, unaccessed nodes. High-access nodes resist it: a node
  survives unless `access_count < log2(age_days)`.
- **Context building** renders L3 knowledge as Obsidian-style markdown, token-budgeted, for
  injection at spawn.

MCP tools: `log_event`, `create_note`, `query_notes`, `search_similar`, `query_entity`,
`recent_events`, `query_session`.

Embeddings are local — Ollama `nomic-embed-text`, 768-dim. No external embedding API.

Full detail in [`mnemon/README.md`](mnemon/README.md). Register it with Claude Code:

```json
{
  "mcpServers": {
    "agent-memory": {
      "type": "stdio",
      "command": "/path/to/agents-nexus/mnemon/.venv/bin/python3",
      "args": ["-m", "agent_memory.server.mcp_server"]
    }
  }
}
```

## Conductor missions

The Conductor turns a *task* into a *verified result*: a deterministic spine (routing,
provisioning, dispatch, state, I/O) with a few scoped judgment nodes that emit typed output
the spine acts on. The spine never asks an LLM "what next?" open-endedly.

- **`--distribute "<goal>"`** — fan-out mission: classify → plan → provision worktrees →
  dispatch workers into a tiled `mission/<slug>` bucket → verify → adjudicate → synthesize →
  report. Runs detached.
- **`--sdlc "<ticket|goal>"`** — drives a staged pipeline (requirements → domain model →
  tech design → validation → `plan.md`) and stops at the plan. Code is left to a human.

Verification is non-negotiable — a mission cannot report done until verify passes, and failed
verification feeds findings back into re-dispatch rather than into a dead end. Small
single-repo work takes the one-shot escape hatch (worker + verify) and skips the coordination tax.

Design notes: [`docs/conductor-design.md`](docs/conductor-design.md).

## The proxy

`nexus-proxy` (`:4000`) sits between Claude Code and Anthropic, forwards requests verbatim, and
logs every `/v1/messages` call to Langfuse. It routes per session: sessions tagged `work-<repo>`
go to the work gateway, everything else goes straight to Anthropic. **Personal traffic never
touches the work gateway** — no corp-auth injection, no attribution, no dependency on that
gateway being up — while both still get tracing.

Routing and resilience design: [`docs/model-routing.md`](docs/model-routing.md).

## Overlays

Anything org-, machine-, or person-specific stays out of this repo and layers in at install time:

```bash
./install.sh --overlay <your-overlay-repo-url>
```

Overlays compose — run it once per overlay, each declaring its own `name` in `overlay.toml`.
`scripts/overlay-apply.sh --status` lists what's applied, `--remove <name>` un-applies one.
Without `--overlay` you get a generic standalone setup. See
[`overlay.example/README.md`](overlay.example/README.md) and [`MAINTAINERS.md`](MAINTAINERS.md).

## The knowledge stack

Postgres (+pgvector), Ollama, and the mnemon flush daemon run as Docker services; the mnemon MCP
server runs natively (stdio). Postgres is bundled locally by default — point `DATABASE_URL` at a
managed instance and skip the `postgres` profile to bring your own.

**Prerequisites:** Docker, [`task`](https://taskfile.dev), Postgres with pgvector (bundled).

```bash
./install.sh --profile personal   # deps, profile, .env, optional stack start
task mnemon:migrate               # migrations
task docker:init                  # pull the embedding model (~270 MB, once)
task up                           # start native services in the background
task systemd:install              # autostart on boot (Linux; launchd:install on macOS)
```

### Lifecycle

```bash
task up / kill / restart / logs      # native services (mnemon)
task docker:up / down / logs / status
task docker:logs -- mnemon-mcp       # single service
task langfuse:up / down / logs / status / update
```

Langfuse is a self-documenting opt-in Compose profile — it's in the compose file so you know it
exists, but plain `task up` will not start it. After `task langfuse:up`, open `localhost:3000`,
create an account, generate keys, and add `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` to `.env` (or run `./install.sh --finish-langfuse`).

### Ports

| Service | Port |
|---|---|
| Ollama | 11434 |
| nexus-proxy | 4000 |
| substrated | 8422 |
| mnemon MCP (SSE) | 8330 |
| slack-bridge health | 8788 |
| Langfuse UI | 3000 |

## Driving the fleet

Prefix is `ctrl+a`.

| Key / command | Action |
|---|---|
| `ctrl+a shift+n` | Fuzzy repo picker → spawn a context-injected agent |
| `ctrl+a shift+b` | New workspace bucket |
| `ctrl+a shift+p` | Launch a Conductor mission (nexus.mission) |
| `ctrl+a b` | Toggle the agent sidebar |
| `ctrl+a 1..9` | Jump to agent N |
| `v 2` | Peek at agent 2 — status summary + last output |
| `agents` | List registered agents with slot, name, directory |
| `q 2 use JWT` | Queue a message to agent 2 (quote if it contains `? ! *`) |
| `q 2 1` | Approve — instant select, no Enter |

Status colors: grey idle, green running, yellow stuck (>10min), red needs input.

### Agent-to-agent messaging

On startup each agent registers itself and receives a peer list — slot, project, directory for
every other active agent. Agents use `/msg <slot> <message>` without you telling them who's where.
With the Slack bus enabled the same traffic rides NATS and surfaces in `#nexus`.

### APM

The status bar shows a rolling 60s count: `42a/7h` = 42 agent actions, 7 human actions.
`prefix+shift+a` opens today's totals, average response time, and active agent count.
Log at `~/.tmux/apm.log`, pruned to 24h.

### Session key profiles

Multiple named API keys can live in `~/.tmux/keys/` and be swapped per session; new agent panes
inherit the active key.

```bash
mkdir -p ~/.tmux/keys
echo 'sk-ant-...' > ~/.tmux/keys/personal
echo 'sk-ant-...' > ~/.tmux/keys/work
chmod 600 ~/.tmux/keys/*

usekey work      # activate
whichkey         # active key name + first 12 chars
keys             # list profiles (* = active)
```

These are **your own** keys for separating personal and work usage — not a mechanism for
sharing credentials. The status bar shows `[key:name]` in red when a non-default key is active.
Keys are gitignored and never committed.

## Repository layout

```
agents-nexus/
├── install.sh              # unified installer (detects OS)
├── Taskfile.yml            # task runner (docker, mnemon, langfuse, systemd/launchd)
├── docker-compose.yml      # knowledge stack + optional Langfuse profile
├── substrated/             # cached fleet-state read daemon
├── plugins/                # herdr plugins + installer catalog
│   ├── catalog.toml        #   the installer<->plugin seam
│   └── nexus-{fleet,presence,observe,mission}/
├── mnemon/                 # agent memory (MCP server + Postgres)
│   └── migrations/
├── slack-bridge/           # A2A bus, orchestrator, NATS transport
├── agent-runner/           # Conductor: missions, workers, verification
├── proxy/                  # Anthropic pass-through + routing + tracing
├── skills/                 # agent skills
├── commands/               # slash commands (opsx, distribute)
├── overlay.example/        # overlay contract + worked example
├── openspec/               # change specs
├── scripts/                # plugin install, overlay apply, recovery, secrets
├── docs/                   # design docs
└── tmux/{linux,mac}/       # substrate configs, hooks, systemd/launchd units
```

## Platform notes

Linux is the primary target (systemd units under `tmux/linux/systemd/`); macOS is fully supported
(launchd plists under `launchd/`). Differences are shell (`bash`/`zsh`), notifications
(`notify-send`/`osascript`), and GNU vs BSD `date`.

## Prefer tmux?

herdr is the default and the smoothest path, but tmux is a supported fallback — set
`NEXUS_SUBSTRATE=tmux`. `substrated` serves the same contract from either backend, so the rest of
the stack doesn't care which one you run.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
