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
3. Walks an **interactive profile setup** — profile name, compose flavor, core paths, peripheral multi-select, optional stack startup.
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

### 3. Core prompts

| Prompt | Default | Notes |
|--------|---------|-------|
| `REPOS_PATH` | `~/repos` | Directory spark indexes. `~` is expanded. |
| `HOST_TMUX_DIR` | `~/.tmux` | Where mnemon writes the event log. `~` is expanded. |
| `POSTGRES_PASSWORD` *(work only)* | auto-generated `openssl rand -hex 16` | Or paste your own. Stored in `.env.<profile>` only. |
| `DATABASE_URL` *(personal only)* | `postgresql://agents:changeme@localhost:5432/agents?sslmode=disable` | Override with your cloud Postgres connection string. |

### 4. Peripheral multi-select

Numbered TUI — type a number to toggle, `a` to select all, ENTER to confirm. Visible peripherals depend on flavor:

| # | Peripheral | Available in | What gets generated / asked |
|---|------------|--------------|------------------------------|
| 1 | Langfuse observability | both | 6 stack secrets generated; pub/secret API keys handled later via `--finish-langfuse` |
| 2 | GitLab webhook re-indexing | both | Asks `GITLAB_URL`, `GITLAB_TOKEN`; generates `SPARK_WEBHOOK_SECRET` |
| 3 | Cloudflare tunnel | both | Asks `CLOUDFLARE_TUNNEL_TOKEN` |
| 4 | GitHub integration | work only | Asks `GITHUB_URL`, `GITHUB_TOKEN` |
| 5 | Corporate gateway upstream | work only | Asks `ANTHROPIC_API_BASE`. On Linux, prints a `host.docker.internal` resolution reminder. |

### 5. Files written

- `.env.<profile>` (chmod 600) — every variable for this profile, including a `NEXUS_PROFILE=<name>` and `NEXUS_COMPOSE_FILE=<file>` header that downstream tooling can read.
- `.env` — **symlink** to `.env.<profile>`. Docker Compose auto-picks this up; no wrapper changes needed.
- `.nexus-profile` — one-line text file naming the active profile. Useful for shell prompts or scripts that can't follow symlinks.

All three are gitignored.

### 6. Stack startup (optional)

At the end you're asked **"Start the Docker stack now? [Y/n]"**. If you accept:

```
docker compose -f <compose-file> up -d
docker compose -f <compose-file> --profile langfuse up -d   # only if Langfuse selected
docker compose -f <compose-file> run --rm ollama-init       # pulls embedding model
```

Decline if you want to inspect `.env.<profile>` first or you're on a box without Docker. The summary screen prints the exact commands to run later.

## Two-phase Langfuse setup

Langfuse needs two passes because its API keys are created in the UI (which can't exist until the stack is up):

1. **First pass** — `./install.sh` with Langfuse selected. The installer generates the six stack secrets (NextAuth, salt, encryption key, DB password, Redis auth, ClickHouse password) and starts the stack.
2. **Open the UI** — `http://localhost:3000`. Create a user, create a project, Settings → API Keys → Create new key.
3. **Second pass** — `./install.sh --finish-langfuse`. Paste the `pk-lf-...` and `sk-lf-...` values when prompted. The installer rewrites the active profile's `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` lines and runs `docker compose up -d --force-recreate proxy` so the proxy picks them up.

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
- `host.docker.internal` does **not** auto-resolve on Linux. If you pick the corporate-gateway peripheral, either:
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
