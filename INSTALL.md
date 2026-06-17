# Install

Single source of truth for `./install.sh` — every flag, every prompt, every file it touches. The use-case READMEs ([`README_SETUP_PERSONAL.md`](README_SETUP_PERSONAL.md), [`README_SETUP_WORK.md`](README_SETUP_WORK.md)) link here for installer mechanics and keep only the use-case-specific bits (proxy verification curls, corporate gateway quirks, etc.).

Supported platforms: **macOS** and **Linux**. A Windows/MSYS2 path exists in the script but is no longer actively maintained.

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
5. Installs dashboard npm deps (skippable with `--no-ui`).
6. Validates that `uv`, `python3`, and `~/.claude/claude_code_config.json` look healthy.

## Flags

| Flag | Behavior |
|------|----------|
| *(none)* | Full interactive flow. Asks for profile name. |
| `--profile <name>` | Use/create the named profile. Skips the profile-name prompt. |
| `--switch <name>` | **No prompts, no deps reinstall.** Repoint `.env` symlink and `.nexus-profile` at an existing `.env.<name>`. Fails if the profile file doesn't exist. |
| `--finish-langfuse` | Re-prompt for `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` against the active profile and recreate the `proxy` container. Use this after creating an API key in the Langfuse UI (see [Two-phase Langfuse setup](#two-phase-langfuse-setup)). |
| `--finish-slack` | Re-prompt for `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_NEXUS_CHANNEL` against the active profile, `npm install` the bridge, and (macOS) offer to install the launchd supervisor. Use this after creating the Slack app (see [Slack bridge setup](#slack-bridge-setup) and [`docs/slack-bridge.md`](docs/slack-bridge.md)). |
| `--non-interactive` | Skip the entire profile-setup step. Runs deps + tmux configs + skills + dashboard (matches the pre-rewrite installer behavior). Safe for CI / scripted re-runs. |
| `--no-ui` | Skip the dashboard `npm install` step. |
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
| spark | `spark` | both | `ollama` |
| mnemon | `mnemon` | both | `ollama` (+ `postgres` on work) |
| dashboard | `dashboard` | both | — |
| langfuse | `langfuse` | both | — (6-container stack) |

Pick a subset for a focused box — e.g. **proxy + langfuse** = an observability-only node, nothing else built or run.

### 4. Per-service configuration

You're only prompted for what the selected services need (the dependency closure runs first, so picking `spark`/`mnemon` auto-enables `ollama`/`postgres`):

| Selected | Prompt | Default | Notes |
|----------|--------|---------|-------|
| `proxy` | `ANTHROPIC_API_BASE` | `https://api.anthropic.com` | Proxy upstream. Point at a corporate gateway to route through it. Linux: `host.docker.internal` won't auto-resolve. |
| `postgres` *(work)* | `POSTGRES_PASSWORD` | auto `openssl rand -hex 16` | Or paste your own. Builds the local `DATABASE_URL`. |
| `mnemon` *(personal)* | `DATABASE_URL` | `postgresql://agents:changeme@localhost:5432/agents?sslmode=disable` | Bring your own Postgres connection string. |
| `mnemon` | `HOST_TMUX_DIR` | `~/.tmux` | Where mnemon reads tmux event logs. `~` expanded. |
| `spark` | `REPOS_PATH` | `~/repos` | Directory spark indexes. `~` expanded. |
| `spark` | GitLab re-indexing? | no | If yes: asks `GITLAB_URL`, `GITLAB_TOKEN`; generates `SPARK_WEBHOOK_SECRET`. |
| `spark` *(work)* | GitHub integration? | no | If yes: asks `GITHUB_URL`, `GITHUB_TOKEN`. |
| `spark` | Cloudflare tunnel? | no | If yes: asks `CLOUDFLARE_TUNNEL_TOKEN` (exposes spark publicly). |
| `langfuse` | — | — | 6 stack secrets generated; pub/secret API keys handled later via `--finish-langfuse`. |

### 5. Integrations (host services)

Asked regardless of the Docker selection, since they run outside the stack:

| Integration | Prompt | Notes |
|-------------|--------|-------|
| Slack bridge | Enable? | If yes, optionally paste the 3 Slack tokens now, else empty `SLACK_*` keys are written to finish later via `--finish-slack`. See [Slack bridge setup](#slack-bridge-setup). |

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

Every stack service has a compose profile (`proxy`, `ollama`, `postgres`, `spark`, `mnemon`, `dashboard`, `langfuse`). The installer writes the chosen set to `COMPOSE_PROFILES` in the profile `.env`; Docker Compose reads it natively, so `docker compose up`, `task up`, and `task docker:up` all honor it with no extra flags.

- **Inspect the active set:** `task stack:profiles`
- **Change it:** `task stack:profiles -- proxy,langfuse` (rewrites the active profile's `.env`)
- **Ad-hoc observability-only run** (doesn't touch the saved profile): `task observability:up`

### Migrating an existing install

Profiles created **before** this change have no `COMPOSE_PROFILES`, so a bare `docker compose up` would start **nothing**. Fix it either way (idempotent):

- **Installer path** — `./install.sh --switch <profile>` backfills the prior always-on set automatically. The same backfill runs when you re-point an existing profile during a normal `./install.sh`.
- **One-liner** — `task stack:profiles -- proxy,ollama,spark,mnemon,dashboard` (add `,postgres` on the work flavor; add `,langfuse` if that box runs Langfuse).

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

## Switching profiles

```bash
./install.sh --switch work       # active = work
./install.sh --switch personal   # active = personal
cat .nexus-profile               # prints whichever is active
ls -la .env                      # -> .env.<active>
```

`--switch` only re-points the symlink and the `.nexus-profile` file. It does not touch the contents of either profile and does not reinstall deps.

## Non-interactive mode

```bash
./install.sh --non-interactive
```

Runs deps + tmux configs + skills + dashboard only. Skips the entire profile-setup step. Useful for:

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

## Common failure modes

| Symptom | Fix |
|---------|-----|
| `ERROR: --switch requires a profile name` | Pass the name: `./install.sh --switch personal`. |
| `ERROR: .env.<name> does not exist` (from `--switch`) | Run `./install.sh --profile <name>` first to create it. |
| `ERROR: no active profile` (from `--finish-langfuse`) | You haven't created a profile yet. Run `./install.sh` first. |
| `WARNING: dashboard/ui/package.json not found, skipping` | Older checkout. `git pull` to get the renamed dashboard layout, then re-run. |
| `docker compose ... up -d` fails with port collisions | Edit the `*_PORT` lines in `.env.<profile>` and re-run `docker compose up -d`. |
| `docker compose up -d` starts **no** containers | The active `.env` has no `COMPOSE_PROFILES` (pre-rewrite profile). Backfill it: `./install.sh --switch <profile>` or `task stack:profiles -- proxy,ollama,spark,mnemon,dashboard`. |
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
