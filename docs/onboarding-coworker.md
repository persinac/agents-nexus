# Onboarding a new box (fresh install)

Get a teammate from zero → a running agents-nexus fleet on their own machine. This is the
**single-host** path: everything runs locally, no shared infrastructure. Cross-machine A2A
(agents on different boxes messaging each other) is an optional follow-up — see
[Going multi-host](#going-multi-host-later).

> **No tmux experience needed.** The fleet's backend is **herdr**, not raw tmux; the installer
> sets it up. You drive it through a fuzzy picker, not tmux commands.

Supported: **macOS**, **Linux**, **Windows via WSL2** (runs the Linux path). Full installer
reference: [`INSTALL.md`](../INSTALL.md).

---

## Before you start (prereqs the installer can't do for you)

| Need | Why | Check |
|---|---|---|
| **Homebrew** (macOS) | the installer bootstraps deps + herdr through it; it **aborts** without it | `brew --version` |
| **Claude Code, authenticated** | the agents are Claude Code sessions | `claude --version` and you can start a session |
| **git** | clone the repo (+ any private overlay) | `git --version` |
| *(if your org has a private overlay)* **access to that git host** (e.g. SSH/token) | to fetch the overlay + register the plugin marketplace | you can clone the overlay repo |

Everything else — **herdr, fzf, node, uv, Python 3.14, Claude Code** — the installer installs.

---

## Steps

### 1. Clone and run the installer

```bash
git clone <this-repo> && cd agents-nexus
./install.sh
```

The installer detects your OS, installs system deps, copies configs into `~/.tmux/`, symlinks the
skills in `skills/` into `~/.claude/skills/`, then walks an **interactive profile setup**.

### 2. Answer the profile prompts

| Prompt | Pick for a standard box | Notes |
|---|---|---|
| **Profile name** | accept the default (`<you>-personal`) | alphanumeric + `-`/`_` |
| **Compose flavor** | `work` | bundles a local Postgres + generates its secret. Choose `personal` only if you're bringing your own DB (`DATABASE_URL`). |
| **Service selection** | keep the default (all on **except** Langfuse) | numbered TUI — toggle by number, `a` = all, ENTER = confirm. Trim to taste. |
| **A2A bus transport** | `nats` | how agents message each other. |
| ↳ **Run a LOCAL NATS container?** | **Yes** | = **single-host**: a local broker on `:4222`, NATS is the sole A2A medium, no auth needed. (Answering **No** is the multi-host path — see below.) |
| **Slack tokens** | paste, or skip | powers the human notify/reply leg (`/notify`, threads). Can finish later with `./install.sh --finish-slack`. |

When it's done, the local stack is up and the profile `.env` is written (git-ignored).

### 3. Layer your org's overlay (if you have one)

An **overlay** is a private repo that fills the org/personal seams the public core leaves open —
team plugin catalog, shared configs, workflow scripts. If your team publishes one, apply it:

```bash
./install.sh --overlay <your-overlay-git-url>
# already installed? equivalently:
scripts/overlay-apply.sh <your-overlay-git-url>
```

It clones the overlay, drops its files into the (git-ignored) slots, templates any host paths, and
records everything in `.git/info/exclude` so the public core can never re-export it. Check it and
un-apply with:

```bash
scripts/overlay-apply.sh --status
scripts/overlay-apply.sh --remove <overlay-name>
```

> Your overlay's own README documents its exact URL, any one-time **plugin marketplace registration**,
> and which per-engineer `.env` values to set (e.g. issue-tracker project/account, tokens). Follow it
> after this step.

### 4. Fill in per-engineer `.env` values

The shared overlay carries **no** per-person literals — you supply your own in the profile `.env`
(git-ignored). Typical ones (your overlay's README lists the exact set):

- an org git token (for review/automation tooling)
- your issue-tracker project key, your account id, a default parent epic
- a docs-space key for write-ups

### 5. Verify

```bash
curl -s localhost:8788/health
#   → "bus":true, "transport":"nats", "a2a_mode":"single-host"
```

Then launch an agent from the **herdr repo picker** and confirm your repos list appears. If the
picker is empty, your repo root isn't where the launcher looks — set `REPO_DIR` in `~/.tmux/env.sh`
to the directory that holds your checkouts.

You're running. Same-box agents message each other by name via the bus; the conductor, spark, and
your overlay's tooling are all live locally.

---

## Going multi-host later

Single-host uses a **loopback** broker, so other machines can't reach it. To let boxes A2A across
machines, one shared, reachable NATS broker is needed and every box switches to `multi-host`. Full
runbook: [`docs/slack-to-nats-cutover.md`](slack-to-nats-cutover.md). Short version:

1. Stand up a reachable, authenticated broker (bind beyond loopback + firewall + **TLS + creds/token**),
   or a dedicated NATS box.
2. On **every** box: `./install.sh --finish-nats` → point `NATS_URL` at the shared broker, set auth.
   This flips the box to `multi-host`.
3. Verify per box: `curl -s localhost:8788/health` (`a2a_mode:multi-host`) and
   `curl -s localhost:8788/agents` (combined fleet). Cross-host send: `agent-send.sh <host>/<name> "…"`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Installer aborts on macOS | Install Homebrew first, re-run `./install.sh`. |
| herdr repo picker is empty | Set `REPO_DIR=<your repos dir>` in `~/.tmux/env.sh` (defaults to `~/repos`). |
| Overlay plugins don't appear in the picker | Run the one-time `claude plugin marketplace add <url>` from your overlay's README. |
| `/health` shows `transport:slack` | Re-run `./install.sh` and choose the `nats` transport, or set `NEXUS_BUS_TRANSPORT=nats` in `.env` and restart the bridge. |
| Broker down (nats mode) | Bring it up: `docker compose -f docker-compose.work.yml --profile nats up -d nats`. |
