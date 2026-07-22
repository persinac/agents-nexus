# Install

Single source of truth for `./install.sh` — every flag, every prompt, every file it touches. The use-case READMEs ([`README_SETUP_PERSONAL.md`](README_SETUP_PERSONAL.md), [`README_SETUP_WORK.md`](README_SETUP_WORK.md)) link here for installer mechanics and keep only the use-case-specific bits (proxy verification curls, corporate gateway quirks, etc.).

Supported platforms: **macOS** and **Linux** — plus **Windows via WSL2**, which runs the Linux path verbatim (see [Windows (WSL2)](#windows-wsl2)). The native Windows/MSYS2 path in the script is legacy (pre-herdr) and no longer actively maintained.

## Quick start

```bash
git clone <this-repo> && cd agents-nexus
./install.sh                       # interactive
./install.sh --profile personal    # name the profile up front
```

The installer:

1. Detects your OS and installs system deps (tmux, fzf, node, uv, Python 3.14, Claude Code).
2. Copies platform tmux configs to `~/.tmux/` and `~/.tmux.conf`.
3. Walks an **interactive profile setup** — profile name, compose flavor, per-service selection (which containers run), per-service config, host integrations, optional stack startup.
4. Symlinks global Claude skills from `skills/` into `~/.claude/skills/`.
5. Validates that `uv`, `python3`, and `~/.claude/claude_code_config.json` look healthy.

## Flags

| Flag | Behavior |
|------|----------|
| *(none)* | Full interactive flow. Asks for profile name. |
| `--profile <name>` | Use/create the named profile. Skips the profile-name prompt. |
| `--switch <name>` | **No prompts, no deps reinstall.** Repoint `.env` symlink and `.nexus-profile` at an existing `.env.<name>`. Fails if the profile file doesn't exist. |
| `--finish-langfuse` | Re-prompt for `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` against the active profile and recreate the `proxy` container. Use this after creating an API key in the Langfuse UI (see [Two-phase Langfuse setup](#two-phase-langfuse-setup)). |
| `--finish-slack` | Re-prompt for `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_NEXUS_CHANNEL` against the active profile, `npm install` the bridge, and (macOS) offer to install the launchd supervisor. Use this after creating the Slack app (see [Slack bridge setup](#slack-bridge-setup) and [`docs/slack-bridge.md`](docs/slack-bridge.md)). |
| `--finish-nats` | Set `NATS_URL` + auth (`NATS_CREDS` / `NATS_TOKEN`) for the NATS A2A transport against the active profile and restart the bridge. This is the **cross-machine** step: point every box at the one shared broker. See [A2A bus transport](#a2a-bus-transport) and [`docs/slack-bridge.md`](docs/slack-bridge.md#nats-transport). |
| `--non-interactive` | Skip the entire profile-setup step. Runs deps + tmux configs + skills (matches the pre-rewrite installer behavior). Safe for CI / scripted re-runs. |
| `-h`, `--help` | Print usage. |

## Interactive flow, step by step

### 1. Profile name

Default is `<whoami>-personal` (or whatever you passed to `--profile`). Must be alphanumeric + `-` + `_`. If the profile already exists, you'll be asked whether to overwrite or just repoint the `.env` symlink.

### 2. Compose flavor

- `personal` → `docker-compose.yml` (Postgres is your responsibility — point `DATABASE_URL` at a cloud DB or local install)
- `work` → `docker-compose.work.yml` (bundles a local Postgres container; secrets generated for you)

If the profile name is literally `work` this step is skipped — work flavor is implied.

### 3. Service selection

A numbered TUI lists every Docker service — type a number to toggle, `a` to select all, ENTER to confirm. **All services default ON except Langfuse.** Each service carries a compose profile; your selection is written to `COMPOSE_PROFILES` in the profile `.env`, so every `docker compose up` and `task up` honors it (see [Service selection & `COMPOSE_PROFILES`](#service-selection--compose_profiles)).

| Service | Profile | Available in | Pulls in (closure) |
|---------|---------|--------------|--------------------|
| proxy | `proxy` | both | — (prompts `ANTHROPIC_API_BASE`) |
| ollama | `ollama` | both | — |
| postgres | `postgres` | work only | — |
| mnemon | `mnemon` | both | `ollama` (+ `postgres` on work) |
| langfuse | `langfuse` | both | — (6-container stack) |

Pick a subset for a focused box — e.g. **proxy + langfuse** = an observability-only node, nothing else built or run.

### 4. Per-service configuration

You're only prompted for what the selected services need (the dependency closure runs first, so picking `mnemon` auto-enables `ollama` — and `postgres` on the work flavor):

| Selected | Prompt | Default | Notes |
|----------|--------|---------|-------|
| `proxy` | `ANTHROPIC_API_BASE` | `https://api.anthropic.com` | Proxy upstream. Point at a corporate gateway to route through it. Linux: `host.docker.internal` won't auto-resolve. |
| `postgres` *(work)* | `POSTGRES_PASSWORD` | auto `openssl rand -hex 16` | Or paste your own. Builds the local `DATABASE_URL`. |
| `mnemon` *(personal)* | `DATABASE_URL` | `postgresql://agents:changeme@localhost:5432/agents?sslmode=disable` | Bring your own Postgres connection string. |
| `mnemon` | `HOST_TMUX_DIR` | `~/.tmux` | Where mnemon reads tmux event logs. `~` expanded. |
| `langfuse` | — | — | 6 stack secrets generated; pub/secret API keys handled later via `--finish-langfuse`. |

`REPOS_PATH` (default `~/repos`, your repos directory) is written to every profile `.env`.

### 5. Integrations (host services)

Asked regardless of the Docker selection, since they run outside the stack:

| Integration | Prompt | Notes |
|-------------|--------|-------|
| Slack bridge | Enable? | If yes, optionally paste the 3 Slack tokens now, else empty `SLACK_*` keys are written to finish later via `--finish-slack`. See [Slack bridge setup](#slack-bridge-setup). |
| A2A bus transport | `slack` / `nats` | How agents message each other. `slack` (default) uses the `#nexus-agents` channel. `nats` uses a NATS+JetStream broker — pick a **local container** (single box / dev) or point at a **shared remote broker** (cross-machine). See [A2A bus transport](#a2a-bus-transport). |

### 6. Files written

- `.env.<profile>` (chmod 600) — every variable for this profile, including the `NEXUS_PROFILE` / `NEXUS_COMPOSE_FILE` header and the `COMPOSE_PROFILES` / `NEXUS_SERVICES` selection.
- `.env` — **symlink** to `.env.<profile>`. Docker Compose auto-picks this up; no wrapper changes needed.
- `.nexus-profile` — one-line text file naming the active profile. Useful for shell prompts or scripts that can't follow symlinks.

All three are gitignored.

### 7. Stack startup (optional)

At the end you're asked **"Start the Docker stack now? [Y/n]"**. If you accept, the installer runs (the `up` is gated by your `COMPOSE_PROFILES`):

```
docker compose -f <compose-file> up -d                          # the selected services
# then, for the one-shot init jobs (the 'init' profile is never in COMPOSE_PROFILES):
docker compose -f <work-compose> --profile init --profile ollama --profile postgres run --rm db-migrate   # if postgres selected (work)
docker compose -f <compose-file>  --profile init --profile ollama [--profile postgres] run --rm ollama-init  # if ollama selected
```

> The init jobs name extra profiles because a CLI `--profile` **replaces** (doesn't merge with) the `.env` `COMPOSE_PROFILES`, and the shared `init` profile pulls in both one-shots — so every dependency's profile must be named for the project to validate. `run` still starts only the target job + its direct dependency.

Decline if you want to inspect `.env.<profile>` first or you're on a box without Docker. Because `COMPOSE_PROFILES` lives in `.env`, a later bare `docker compose up -d` brings up the same set.

## Service selection & `COMPOSE_PROFILES`

Every stack service has a compose profile (`proxy`, `ollama`, `postgres`, `mnemon`, `langfuse`). The installer writes the chosen set to `COMPOSE_PROFILES` in the profile `.env`; Docker Compose reads it natively, so `docker compose up`, `task up`, and `task docker:up` all honor it with no extra flags.

- **Inspect the active set:** `task stack:profiles`
- **Change it:** `task stack:profiles -- proxy,langfuse` (rewrites the active profile's `.env`)
- **Ad-hoc observability-only run** (doesn't touch the saved profile): `task observability:up`

### Migrating an existing install

Profiles created **before** this change have no `COMPOSE_PROFILES`, so a bare `docker compose up` would start **nothing**. Fix it either way (idempotent):

- **Installer path** — `./install.sh --switch <profile>` backfills the prior always-on set automatically. The same backfill runs when you re-point an existing profile during a normal `./install.sh`.
- **One-liner** — `task stack:profiles -- proxy,ollama,mnemon` (add `,postgres` on the work flavor; add `,langfuse` if that box runs Langfuse).

## Two-phase Langfuse setup

Langfuse needs two passes because its API keys are created in the UI (which can't exist until the stack is up):

1. **First pass** — `./install.sh` with the `langfuse` service selected. The installer generates the six stack secrets (NextAuth, salt, encryption key, DB password, Redis auth, ClickHouse password) and — since `langfuse` is in `COMPOSE_PROFILES` — starts it with the rest of the stack.
2. **Open the UI** — `http://localhost:3000`. Create a user, create a project, Settings → API Keys → Create new key.
3. **Second pass** — `./install.sh --finish-langfuse`. Paste the `pk-lf-...` and `sk-lf-...` values when prompted. The installer rewrites the active profile's `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` lines and runs `docker compose up -d --force-recreate proxy` so the proxy picks them up.

## Slack bridge setup

The Slack bridge gives you two-way control — message an agent from a `#nexus` Slack channel, and have agents post back when they need input (replies route straight back to the right agent). It needs a Slack app with **Socket Mode** (no public URL/tunnel required). Like Langfuse, it's a two-pass flow because the app/tokens are created in Slack's UI:

1. **First pass** — `./install.sh` and answer **yes** to the Slack bridge integration. If you already have the tokens you can paste them now; otherwise the installer writes empty `SLACK_*` keys to the profile.
2. **Create the Slack app** — follow the manifest + scopes in [`docs/slack-bridge.md`](docs/slack-bridge.md), enable Socket Mode, generate the bot (`xoxb-…`) and app-level (`xapp-…`) tokens, and invite the bot to a private `#nexus` channel.
3. **Second pass** — `./install.sh --finish-slack`. Paste the bot token, app token, and channel id. The installer rewrites the active profile's `SLACK_*` lines, `npm install`s the bridge, and (macOS) offers to install the launchd supervisor. Start it manually any time with `task slack:bridge`.

## A2A bus transport

How agents message each other (agent↔agent). The **human** notify/reply leg always stays on Slack; this only chooses the machine-to-machine medium. Full design + operations in [`docs/slack-bridge.md#nats-transport`](docs/slack-bridge.md#nats-transport).

- **`slack`** (default) — A2A rides the `#nexus-agents` Slack channel. Fine for a single box; it does not scale to a fleet (Slack Socket-Mode caps connections per app).
- **`nats`** — A2A rides a NATS + JetStream broker (durable inbox, presence KV, subject addressing). The installer offers two shapes:
  - **Local container** — adds the `nats` compose profile so this box hosts the broker (`docker compose -f docker-compose.work.yml --profile nats up -d nats`). Great for one machine or dev. A container bound to `localhost` is **not reachable by other machines**.
  - **Remote / shared broker** — no local container; you set `NATS_URL` to a broker every bridge can reach. **This is the cross-machine path**: run ONE broker on a shared host (or the Linux nexus box / dedicated infra) with TLS + per-user creds, and point every box's `NATS_URL` at it.

Cross-machine flow:

1. Stand up the shared broker once (a box running the `nats` profile with a routable bind + firewall + TLS, or managed NATS).
2. On every participating box: `./install.sh` → choose `nats` → **remote broker** → set `NATS_URL`; or set it later with **`./install.sh --finish-nats`** (which also sets `NATS_CREDS`/`NATS_TOKEN` and restarts the bridge).
3. Onboarding a new engineer is issuing a **credential**, not provisioning a Slack app — that is the whole point of moving off Slack.

> The bridge process that speaks NATS **is** the Slack bridge, so it still needs Slack tokens to boot (for the human leg). Enable the Slack bridge too, or the process won't start.

## Switching profiles

```bash
./install.sh --switch work       # active = work
./install.sh --switch personal   # active = personal
cat .nexus-profile               # prints whichever is active
ls -la .env                      # -> .env.<active>
```

`--switch` only re-points the symlink and the `.nexus-profile` file. It does not touch the contents of either profile and does not reinstall deps.

## Staying up to date

If you installed on an earlier revision, a plain `git pull` is **not enough** — the pull
refreshes repo files, but the *installed copies* drift (the `~/.tmux` symlinks,
`~/.claude/settings.json`, the herdr base config + per-plugin keybindings, launchd/systemd
units), and services that were later **removed** (spark, the dashboard, arbiter) keep
running until torn down. Run the updater:

```bash
bash scripts/update.sh            # pull + reconcile installed copies + tear down removed services
bash scripts/update.sh --dry-run  # show what WOULD change; touch nothing
bash scripts/update.sh --no-pull  # reconcile the current checkout without pulling
```

It is **idempotent** and non-destructive to your data (kept-service DB volumes are
untouched). It: pulls (fast-forward only; skips if you have uncommitted changes), re-runs
the platform install (symlinks + `settings.json` merge incl. the MCP tool-search env +
`claude_code_config.json` + units), re-syncs the herdr plugin keybinding blocks (a changed
`keys.toml` doesn't propagate on pull), stops/removes any lingering spark/dashboard/arbiter
containers + volumes + launchd/systemd units, and prunes those from your `.env` profiles
(backup at `.env.pre-update.bak`).

After it runs: **relaunch your agents** so they pick up the new `settings.json` env + hooks
(running sessions keep the old env until restarted). New herdr chords are live immediately —
`prefix+shift+f` (memory search), `prefix+shift+o` (command center).

**Overlays are refreshed separately.** `update.sh` reconciles the *public core* only — it
does **not** re-fetch a private overlay (that's a separate repo with its own auth +
versioning). It detects any applied overlay and prints the re-apply command; if your team's
overlay changed upstream, run it:

```bash
scripts/overlay-apply.sh <its-git-url>     # re-clones + re-layers (source is shown by update.sh, or --status)
scripts/overlay-apply.sh --status          # list applied overlays + their sources
```

## Non-interactive mode

```bash
./install.sh --non-interactive
```

Runs deps + tmux configs + skills only. Skips the entire profile-setup step. Useful for:

- CI / Dockerfile bootstraps where prompts can't be answered
- Re-running the system-deps + skills steps after a `git pull` without touching your env
- The historical pre-rewrite behavior (this is the equivalent)

If `.env` doesn't exist yet, the stack won't start cleanly — generate a profile interactively at least once.

## What the installer reuses from your existing setup

- If `.env.<profile>` already exists you can decline overwrite, and the installer will just re-point the symlink at it.
- System-dep installs are idempotent — they print `[ok] <tool>` and skip when already present.
- Skill symlinks are refreshed on every run (force-relinked via `ln -sfn`).
- Tmux config + scripts are copied each run; if you've edited them in-place under `~/.tmux/`, those edits will be **overwritten**.

## Linux notes

- The interactive flow uses only stock POSIX-y bash 3.2 constructs — no `dialog`, `gum`, `whiptail`, or other TUI deps. Works in plain SSH sessions.
- `host.docker.internal` does **not** auto-resolve on Linux. If you point the proxy's `ANTHROPIC_API_BASE` at a host-local gateway, either:
  - Add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `proxy` service in the relevant compose file, or
  - Set `ANTHROPIC_API_BASE` to a routable IP your container can reach.
- Package manager detection covers `apt`, `dnf`, `pacman` — if you're on something else, install `tmux fzf nodejs npm` yourself first and the script will skip ahead.
- `~/.local/bin` should be early in `PATH` so the uv-managed `python3` shim wins over a system Python.

## macOS notes

- Requires Homebrew. The installer will exit with a pointer to https://brew.sh if `brew` isn't on `PATH`.
- macOS ships bash 3.2 at `/bin/bash`; the script is compatible with it. (You don't need a newer bash installed.)

## Windows (WSL2)

Native Windows is **not** a supported fleet host — the `tmux/windows/` tree is the legacy, pre-herdr MSYS2 path (no herdr config, no `substrate.sh`, no `substrated` service). The supported way to run the full fleet on Windows hardware is **WSL2**, where the box is just Linux and the entire [Linux](#linux-notes) path applies verbatim (herdr installs via the same `curl`, `substrated` runs as a `systemctl --user` unit, the picker works).

> **Already have a Linux fleet host?** You may not need any of this. herdr supports remote attach — `herdr --remote <nexus-host>` from Windows Terminal drives the fleet running on that box, and you point Claude Code at the `agent-memory` MCP endpoint over the SSH/Cloudflare tunnel. Treat Windows as a thin client and skip the install below.

### Steps

1. **Install WSL2 + a distro** (PowerShell as admin), then reboot and create your Linux user:
   ```powershell
   wsl --install -d Ubuntu
   ```
2. **Enable systemd** — the fleet's background pieces (`substrated`, `slack-bridge`, the timers) are `systemctl --user` units, so the distro must run systemd. Add to `/etc/wsl.conf` inside the distro:
   ```ini
   [boot]
   systemd=true
   ```
   Then from PowerShell `wsl --shutdown`, reopen the distro, and confirm `systemctl --user` responds.
3. **Clone into the WSL2 filesystem, _not_ `/mnt/c`.** Put the repo under `~/repos` inside the distro. The Windows drive mount (`/mnt/c/…`) does not preserve Unix exec bits or honor `chmod` reliably — that re-triggers the `secret-run.sh` `203/EXEC` failure and breaks the herdr scripts — and native ext4 is far faster for git/npm/docker.
4. **Run the Linux install** and follow [Linux notes](#linux-notes):
   ```bash
   cd ~/repos/agents-nexus && ./install.sh
   ```

### WSL2-specific gotchas

- **Keeping the fleet alive.** `tmux/linux/install.sh` runs `loginctl enable-linger` so user services survive with no shell attached — but WSL2 tears the whole VM down when the **last** session closes. For an always-on box, keep one WSL session open, or launch the distro from a Windows Task Scheduler job at logon (`wsl -d Ubuntu -u <user> --exec /bin/true` keeps it booted).
- **Docker.** Either turn on Docker Desktop's WSL2 integration (the in-distro `docker` CLI then talks to Docker Desktop), or install Docker Engine directly inside the distro (uses the systemd you enabled above). The knowledge stack (`docker compose up`, named volumes) behaves the same either way.
- **`.env` line endings.** Editing `.env` from a Windows editor (VS Code over `/mnt/c`, Notepad) can introduce CRLF → `$'\r': command not found` on source. Keep it LF: `sed -i 's/\r$//' .env`.
- **Notifications.** Headless WSL has no `notify-send` target, so `nexus.presence` degrades to a terminal bell — route toasts to the Slack bus via `NEXUS_PRESENCE_NOTIFY_CMD`, same as any headless Linux box.
- **Attaching + GPU.** Launch the distro in Windows Terminal and run `herdr`; its panes render correctly there. `ollama` embeddings use the GPU only if your WSL2 has CUDA configured, otherwise they run CPU-only.

See [`docs/herdr-linux-setup.md`](docs/herdr-linux-setup.md) for the herdr substrate specifics and the shared gotcha list (all of which apply under WSL2).

## Common failure modes

| Symptom | Fix |
|---------|-----|
| `ERROR: --switch requires a profile name` | Pass the name: `./install.sh --switch personal`. |
| `ERROR: .env.<name> does not exist` (from `--switch`) | Run `./install.sh --profile <name>` first to create it. |
| `ERROR: no active profile` (from `--finish-langfuse`) | You haven't created a profile yet. Run `./install.sh` first. |
| `docker compose ... up -d` fails with port collisions | Edit the `*_PORT` lines in `.env.<profile>` and re-run `docker compose up -d`. |
| `docker compose up -d` starts **no** containers | The active `.env` has no `COMPOSE_PROFILES` (pre-rewrite profile). Backfill it: `./install.sh --switch <profile>` or `task stack:profiles -- proxy,ollama,mnemon`. |
| `ANTHROPIC_API_BASE` "variable is not set" error on `up` | The `proxy` profile is active but the var is unset. Add `ANTHROPIC_API_BASE=https://api.anthropic.com` (or your gateway) to `.env`, or drop `proxy` from `COMPOSE_PROFILES`. |
| Profile env edits not taking effect | The proxy and most services pick env up at container creation, not restart. Run `docker compose up -d --force-recreate <service>` after editing. |

## Manually-generated profile (no installer)

The installer is the supported path, but the file format is plain. If you'd rather hand-craft:

```bash
cp .env.example .env.mine
$EDITOR .env.mine        # fill values; see comments in .env.example for what each one does
ln -sfn .env.mine .env
echo mine > .nexus-profile
docker compose up -d
```

The compose files don't care whether `.env` was generated by the installer or by hand — they only need the symlink to resolve to a file with the expected keys.
