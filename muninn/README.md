# Muninn

Automatically sync and digitize handwritten notes from a reMarkable 1 tablet into Obsidian. Named after Odin's raven of memory — the one that flies out, gathers the day's information, and brings it home to the second memory (your vault).

**Pipeline:** rM1 (SSH/rclone) → `.rm` stroke parsing → Claude vision OCR + diagram description → vault `Muninn/<title>.md` → optional daily-note merge into your existing `Daily Notes/YYYY-MM-DD.md`

## Requirements

- macOS or Linux host (tested on macOS)
- Python 3.14+, [uv](https://docs.astral.sh/uv/), [Task](https://taskfile.dev)
- [rclone](https://rclone.org/install/) on the host
- SSH key pair for root access to your rM1
- An Anthropic API key (for Claude vision OCR + drawing descriptions)
- (Optional) Tailscale on both host and tablet for off-network sync

## Setup

### 1. Install dependencies

```bash
task install
```

This runs `uv sync` and applies the `rmrl` setuptools-80 compatibility patch.

### 2. SSH key setup

Generate a dedicated key pair if you don't have one:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_remarkable -N ""
ssh-copy-id -i ~/.ssh/id_ed25519_remarkable.pub root@<tablet-ip>
ssh -i ~/.ssh/id_ed25519_remarkable root@<tablet-ip> "echo ok"
```

### 3. Tailscale (optional — off-network sync)

To sync when the tablet and host are on different networks:

1. **Install Toltec** on the rM1 (community package manager):
   ```bash
   ssh root@<tablet-ip>
   wget -qO- http://toltec-dev.org/latest_installer | bash
   ```

2. **Install Tailscale** via Toltec:
   ```bash
   opkg install tailscale
   tailscale up
   ```

3. **Set the tablet's MagicDNS hostname** as `ssh.host` in `config.toml` (e.g. `remarkable.tail12345.ts.net`). Tailscale handles the transport — keep `ssh.identity_file` pointed at the same key.

### 4. Configure Muninn

```bash
cp config.toml.example config.toml
```

Edit `config.toml` — see [Configuration reference](#configuration-reference) below for every field.

### 5. Verify connectivity

```bash
task run -- check
```

Should report `ok` for SSH, the configured OCR provider, and the vision-descriptions check (if enabled).

### 6. First sync

```bash
task dry-run        # preview pending notebooks
task sync           # run the full pipeline
```

By default `muninn sync` processes every new/modified notebook. Scope it with `--folder PREFIX`:

```bash
uv run muninn sync --folder Work/Garner/
```

## Configuration reference

`config.toml` is gitignored — it holds credentials. Every field below is also documented inline in `config.toml.example`.

### `[ssh]`

| Field           | Required | Description                                    |
| --------------- | :------: | ---------------------------------------------- |
| `host`          | ✓        | Hostname or IP of your rM1 (or Tailscale MagicDNS name) |
| `port`          |          | SSH port (default `22`)                        |
| `username`      |          | SSH user (default `root`)                      |
| `identity_file` | ✓        | Path to private key (e.g. `~/.ssh/id_ed25519_remarkable`) |
| `timeout`       |          | Connection timeout in seconds (default `10`)   |

### `[sync]`

| Field         | Description                                    |
| ------------- | ---------------------------------------------- |
| `staging_dir` | Local directory for pulled notebook files (default `~/.muninn/staging`) |
| `rclone_bin`  | Path to the rclone binary (default `rclone`)   |

### `[ocr]`

Controls handwriting transcription.

| Field             | Description                                    |
| ----------------- | ---------------------------------------------- |
| `enabled`         | Turn OCR on/off                                |
| `provider`        | `"claude"` (default) or `"myscript"`           |
| `application_key` | MyScript App Key — only needed for `provider = "myscript"` |
| `hmac_key`        | MyScript HMAC Key — only needed for `provider = "myscript"` |

When `provider = "claude"`, the Anthropic key from `[vision].api_key` (or the `ANTHROPIC_API_KEY` env var) is used.

### `[vision]`

| Field           | Description                                    |
| --------------- | ---------------------------------------------- |
| `enabled`       | Turn drawing-description generation on/off     |
| `api_key`       | Anthropic API key; falls back to `ANTHROPIC_API_KEY` env var if left as the placeholder |
| `model`         | Override the Claude model (default `claude-opus-4-7`) |
| `ocr_prompt`    | Override the handwriting-transcription prompt  |
| `drawing_prompt`| Override the diagram-description prompt        |

### `[[vaults]]`

Define one or more Obsidian vaults. Exactly one vault must have `default = true`. Notebooks whose `rm_folder` matches a vault's `folders` prefix list are routed there; unmatched notebooks fall back to the default vault.

```toml
[[vaults]]
name = "obs-garner"
path = "~/obs-garner"
subfolder = "Muninn"        # generated .md files land in vault/Muninn/
folders = ["Work/Garner/"]   # notebooks under this rM folder route here

[[vaults]]
name = "personal"
path = "~/obs-personal"
subfolder = "Muninn"
default = true              # everything else routes here
```

### `[daily_merge]`

After sync writes `<vault>/Muninn/<title>.md`, also merge the matching notebook's content into the user's existing dated daily notes.

```toml
[daily_merge]
enabled = true
source_notebook = "Daily"          # title of the rM notebook to treat as daily source
target_vault    = "obs-garner"     # name of a [[vaults]] entry
target_dir      = "Garner/Daily Notes"   # path within that vault to the YYYY-MM-DD.md files
```

How it works:

1. After Phase 7 writes `Muninn/Daily.md`, Claude parses the OCR'd content into `{date, todos, questions, notes}` (Pydantic-validated structured output).
2. The most recent daily note before that date is scanned for a `# TODO` markdown table; Claude carries every item forward with the elapsed days added to its age (`52d → 60d`), preserves status (`Todo` / `In Progress` / `In Review` / `Done` / `Blocked`), and dedupes against the newly extracted TODOs.
3. The merged block is inserted into `<target_dir>/<YYYY-MM-DD>.md` between `<!-- muninn-daily-start rm_hash=... -->` and `<!-- muninn-daily-end -->` markers — idempotent re-runs detect the hash and no-op.

## Commands

All commands accept `--config-file PATH` and `--verbose` at the top level.

| Command                  | What it does |
| ------------------------ | ------------ |
| `muninn check`           | Verify SSH, OCR provider, and vision API connectivity |
| `muninn sync`            | Full pipeline: pull → render → OCR → describe → write vault → daily merge |
| `muninn sync --dry-run`  | Preview pending notebooks without writing anything |
| `muninn sync --folder X` | Scope sync to notebooks under reMarkable folder prefix `X` (repeatable, case-insensitive) |
| `muninn write-vault`     | Re-render every notebook in the DB as Markdown into its vault (useful for vault rebuild) |
| `muninn reocr`           | Re-OCR pages whose stored provider doesn't match current config (use after switching providers) |
| `muninn ocr-compare`     | Side-by-side comparison of MyScript vs Claude OCR (and multiple models, via `--model`) |
| `muninn merge-daily`     | Standalone daily-note merge for the current rM Daily content (used by sync; also runnable manually) |
| `muninn backfill-todos`  | Carry the TODO table forward across a date range with ages bumped — no LLM calls |

### Daily merge usage

```bash
# Manual run on whatever's in the DB right now:
uv run muninn merge-daily

# After tweaking the prompt — re-merge ignoring the hash short-circuit:
uv run muninn merge-daily --force

# Test against a different notebook title:
uv run muninn merge-daily --notebook "Standup" --dry-run
```

### Backfilling the TODO chain

If your daily notes have gaps where intermediate days don't carry a TODO table, propagate one from the last known table:

```bash
uv run muninn backfill-todos --from 2026-05-13 --to 2026-05-18
```

Safe by default — skips days whose note already has a muninn-managed block. Use `--force` to overwrite.

### Vault rebuild

```bash
# Re-render every notebook into its routed vault:
uv run muninn write-vault

# Just one vault:
uv run muninn write-vault --vault obs-garner

# Just one rM folder:
uv run muninn write-vault --folder Work/Garner/Chatbot
```

## Scheduling

Run sync on a schedule via launchd (macOS) or cron (Linux).

### launchd (macOS)

A sample plist is included as [`scripts/com.muninn.sync.plist`](scripts/com.muninn.sync.plist). Edit the paths to match your setup, then install:

```bash
# Copy the plist (edit absolute paths first)
cp scripts/com.muninn.sync.plist ~/Library/LaunchAgents/

# Load (starts immediately on next StartInterval boundary)
launchctl load ~/Library/LaunchAgents/com.muninn.sync.plist

# Confirm it's loaded
launchctl list | grep muninn

# Tail the log
tail -f /tmp/muninn-sync.log
```

To stop:

```bash
launchctl unload ~/Library/LaunchAgents/com.muninn.sync.plist
```

The bundled plist runs `muninn sync --folder Work/` every 30 minutes during weekday business hours. Tune `StartInterval`, `StartCalendarInterval`, and the `--folder` filter to match your actual workflow.

### cron (Linux)

```cron
# Every 30 minutes, 8am–6pm, weekdays
*/30 8-18 * * 1-5 cd /path/to/muninn && /path/to/muninn/.venv/bin/muninn sync --folder Work/ >> /tmp/muninn-sync.log 2>&1
```

### Scheduling considerations

- **Cost** — every new/modified page hits the Claude API at least twice (OCR + vision). A typical sync that touches 5 fresh pages runs ~$0.20–0.40 on Opus 4.7. Limit churn by syncing only when you've written new content (e.g. once at the end of the workday, not every 5 minutes).
- **Tailscale on the host** — if you use Tailscale for off-network sync, the host's Tailscale daemon must be running for the launchd job to reach the tablet.
- **Vault sync conflicts** — if your Obsidian vault syncs via iCloud or Syncthing, write conflicts are possible when Obsidian and Muninn write the same file at the same time. The atomic write (`.tmp` + rename) makes this rare, but consider gating sync to a `Muninn/` subfolder if it becomes an issue.

## Troubleshooting

**`Config error: [[vaults]][N].path does not exist`** — vault paths are validated at write time, not load time, but the error fires the first time Muninn tries to write into a vault that's gone. Create the directory or update `[[vaults]].path`.

**`Some data has not been read. The data may have been written using a newer format than this reader supports.`** — emitted by `rmscene` when parsing v6 `.rm` files. Strokes still parse correctly; the warning covers block types we don't care about.

**Sync reports "v6 .rm files skipped" or PDFs missing** — `rmrl` only handles v3/v5 `.rm` files. v6 notebooks (newer firmware) don't render to PDF, but OCR + vision still work because the renderer in `rm_format.render_to_png` parses strokes directly.

**`Daily.md` was OCR'd but not merged into `Daily Notes/`** — check `[daily_merge].enabled = true`, `source_notebook` matches the notebook title exactly, and `target_vault` matches a `[[vaults]].name`.

**Re-running merge-daily says `unchanged` but I want to re-extract** — pass `--force` to skip the rm_hash short-circuit.

## Project layout

```
src/muninn/
├── cli.py         # Click commands: sync, check, reocr, ocr-compare, write-vault, merge-daily, backfill-todos
├── config.py      # config.toml loading + validation
├── conversion.py  # rmrl-based PDF/PNG conversion (best-effort for v6)
├── daily.py       # daily-note merge: section extraction + carry-forward + atomic write
├── db.py          # SQLite schema and per-page cache helpers
├── obsidian.py    # vault routing + markdown rendering + atomic file write
├── ocr.py         # MyScript iink Cloud client (alternative OCR provider)
├── rm_format.py   # .rm v3/v5/v6 stroke parsing and PNG rendering
├── rm_sync.py     # SSH/rclone pull + change detection + folder mapping
└── vision.py      # Claude vision client (default OCR, drawing descriptions)
```

## Taskfile commands

| Command          | Description                                |
| ---------------- | ------------------------------------------ |
| `task install`   | Install dependencies + apply rmrl patch    |
| `task sync`      | Run `muninn sync`                          |
| `task dry-run`   | Preview pending notebooks                  |
| `task check`     | Verify SSH/OCR/vision connectivity         |
| `task lint`      | Lint with ruff                             |
| `task format`    | Format with ruff                           |
| `task test`      | Run tests                                  |
