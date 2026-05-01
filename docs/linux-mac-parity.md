# Linux (mini-pc) ↔ Mac parity — what's missing, what already works

State of affairs as of 2026-04-30. Mac is the daily driver and well-exercised; Linux is the canonical-host plan with substantial scaffolding (PLAN.md, install.sh, systemd units) but several gaps. This doc is a concrete checklist for closing them.

## TL;DR

- The bulk of script parity is already handled by `tmux/linux/install.sh` — it symlinks **Mac's** tmux-scripts into `~/.tmux/` for any file Linux doesn't override. Only `hook-notification.sh` is overridden today.
- The real gaps are: shell init (no `bashrc` analog of `tmux/mac/zshrc`), scheduling (launchd plists not yet ported to systemd timers), one missing systemd `*.timer` for `nightly-spark` siblings, and the **launcher source-of-truth split** that bit us this morning (`~/.tmux/open-claude.sh` on the Mac points at the `claude-agents-tmux` repo, not at `agents-nexus/tmux/mac/...`).
- The mac copies of these scripts inside `agents-nexus/tmux/mac/tmux-scripts/` have **drifted** from the Mac runtime symlinked source. Linux currently inherits the stale versions through `install.sh`.

## What exists today

| Concern | Mac | Linux |
|---|---|---|
| Repo dir | `tmux/mac/` | `tmux/linux/` |
| `tmux.conf` | yes | symlinks `mac/tmux.conf` (no Linux-specific config) |
| Shell init | `tmux/mac/zshrc` | **missing** — PLAN.md describes a `bashrc` mirror |
| `install.sh` | yes — copies scripts, loads launchd plists, writes env.sh | yes — symlinks scripts (Mac fallback), writes env.sh, **does NOT install systemd units** |
| tmux-scripts/ | 23 files | 1 file (`hook-notification.sh` only) |
| Scheduling | 5 launchd plists in `launchd/` | 13 systemd units in `tmux/linux/systemd/` (services + timers, partial coverage) |
| Notification mechanism | `osascript` (macOS notifications) | `printf '\a'` per PLAN.md (bell to SSH client); also `notify-send` on console per README.md |
| Reverse proxy | n/a | Caddy (PLAN.md only — not yet wired) |
| Network access | direct | Tailscale (PLAN.md only) |
| Claude launcher (`open-claude.sh`) | symlink → `claude-agents-tmux/mac/tmux-scripts/open-claude.sh` (out-of-tree) | **none deployed** — would inherit stale Mac copy via `install.sh` fallback |

## The launcher source-of-truth split (the most urgent thing)

This was the mess from this morning:

```
~/.tmux/open-claude.sh                       (Mac runtime)
   └─→ symlink to claude-agents-tmux/mac/tmux-scripts/open-claude.sh
                  ↑
                  this is what I edited today (the /sess/<window>/ proxy prefix)

agents-nexus/tmux/mac/tmux-scripts/open-claude.sh
   ↑
   committed, but stale relative to claude-agents-tmux. Used by NEW
   Mac installs and by Linux's install.sh fallback.

agents-nexus/tmux/linux/tmux-scripts/open-claude.sh
   ↑
   does not exist
```

**Decision needed**: which repo owns the launcher?

- **Option A — `claude-agents-tmux` is canonical**. Mac symlinks at install time into the *other* repo. Linux/Windows installs would also need to know about that repo.
- **Option B — `agents-nexus/tmux/<os>/...` is canonical**. Drop the symlink and `cp` from agents-nexus during `tmux/mac/install.sh`. Sync the current Mac edits back into agents-nexus first.

Option B is simpler (one repo to clone) and matches what `install.sh` already assumes. Option A keeps tmux configuration portable across projects but requires every machine to clone two repos.

Once decided, re-deploy on both Mac and the mini PC.

## Tonight's checklist (recommended order)

### 1. Reconcile the launcher (10 min)

```bash
# On Mac:
diff ~/.tmux/open-claude.sh tmux/mac/tmux-scripts/open-claude.sh
# Decide canonical repo. If Option B, copy the runtime version into agents-nexus,
# replace the symlink with a real file copy from agents-nexus, commit.
```

The diff will show today's `ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/}/sess/${MY_NAME}"` block plus any prior drift.

### 2. Sync drifted Mac scripts into agents-nexus (15-30 min)

The Mac runtime scripts in `~/.tmux/` (symlinked into `claude-agents-tmux`) have drifted from the agents-nexus copies. Worth a one-time reconciliation so Linux inherits sane scripts via `install.sh` fallback:

```bash
# From the Mac:
for script in ~/.tmux/*.sh ~/.tmux/*.py; do
  name=$(basename "$script")
  target="$AGENTS_NEXUS_DIR/tmux/mac/tmux-scripts/$name"
  if [ -f "$target" ] && ! diff -q "$script" "$target" >/dev/null; then
    echo "DRIFT: $name"
    diff "$target" "$script"
  fi
done
```

Pick which side is right per file, sync, commit.

### 3. Apply the launcher edit on the mini-PC (5 min)

Once #1 is decided, the mini-PC needs the `/sess/<name>/` block in its `open-claude.sh`. If using Option B, that's just `git pull && bash tmux/linux/install.sh` after #2 has landed. If using Option A, also clone `claude-agents-tmux` and rerun.

### 4. Linux shell init (30-60 min)

Create `tmux/linux/bashrc` mirroring `tmux/mac/zshrc`. PLAN.md Phase 6.4 lists the deltas:

- `read -rsn1` instead of `read -rk1`
- `source` instead of `.`
- `${BASH_SOURCE[0]}` instead of `${0:A:h}` for script-path resolution
- Otherwise functions (`work`, `q`, `v`, `qa`, `agents`, `wt`) carry over verbatim

Update `tmux/linux/install.sh` to drop a marker line into `~/.bashrc` that sources this (same pattern `mac/install.sh` uses for zshrc).

### 5. Port launchd plists → systemd timers (45–90 min)

`tmux/linux/systemd/` currently has services + timers for: `nightly-obs-tag`, `nightly-spark`, `nightly-repo-sync`, `nightly-vault-commit`, `weekly-obs-decay`, plus `agents-nexus-stack`, `arbiter`, `tmux-agents-session`. The full picture of Mac-side scheduled jobs is broader than just `agents-nexus/launchd/` — there are jobs symlinked in from sibling repos and a couple that are entirely standalone. See the next section for the full inventory.

Make `tmux/linux/install.sh` install/enable units (the loop is sketched in `PLAN.md` Phase 6.1 — currently the install.sh on disk does NOT yet do this).

### 6. Test the notification hook (5 min)

`tmux/linux/tmux-scripts/hook-notification.sh` overrides the Mac one. PLAN.md says use `printf '\a'`; README.md mentions `notify-send`. Pick one and verify it actually surfaces:

- If you mostly SSH in from a Mac terminal, `printf '\a'` lets iTerm2 handle the "ding" — usually preferable.
- If you use the mini-pc with a monitor attached, `notify-send` produces a desktop bubble.

### 7. Caddy + Tailscale (deferred — only if exposing services off-LAN)

PLAN.md phases 5 and 8 cover this. Skip unless you actually need remote access tonight.

## Full inventory of Mac scheduled jobs (and their Linux status)

Captured from `~/Library/LaunchAgents/` on this Mac. Some live in agents-nexus, several are symlinked in from sibling repos, two are entirely standalone, and one is a Homebrew service. Linux needs a deliberate decision per row — not every job belongs on the mini-PC.

### Jobs sourced from `agents-nexus/launchd/` ✓ (in this repo)

| Plist | Schedule | What it runs | Linux status |
|---|---|---|---|
| `com.agents-nexus.docker.plist` | login | starts Docker stack | `agents-nexus-stack.service` covers it (no timer; runs at boot) |
| `com.agents-nexus.gl-reviews.plist` | daily | `task gl:reviews` | **missing systemd unit** |
| `com.agents-nexus.gl-reviews-prune.plist` | daily 16:00 | `task gl:reviews:prune` | **missing systemd unit** |
| `com.agents-nexus.obs-tag.plist` | daily 06:30 | `task obs:tag` | `nightly-obs-tag.{service,timer}` ✓ |
| `com.agents-nexus.obs-decay.plist` | Sun 12:00 | `task obs:decay` | `weekly-obs-decay.{service,timer}` ✓ |

### Jobs sourced from sibling repos (would need Linux ports)

| Plist | Source repo | Schedule | What it runs | Linux status |
|---|---|---|---|---|
| `com.garner.agent-memory.flush.plist` | `claude-agents-tmux/mac/launchd/` | every 120 s | `~/.tmux/flush-events.sh` | **no systemd unit** — flush should run on the mini-PC where mnemon lives |
| `com.garner.guilty-spark.nightly.plist` | `guilty-spark/launchd/` | daily 02:00 | `guilty-spark/scripts/spark-pipeline.sh` | `nightly-spark.{service,timer}` exists in agents-nexus — confirm it points at the right script (might need updating in the guilty-spark repo) |

### Jobs not sourced from any repo (only on Mac filesystem)

These were created ad-hoc on the Mac and aren't checked into anything:

| Plist | Schedule | What it runs | Notes |
|---|---|---|---|
| `com.alex.obs-tidy.plist` | daily 06:00 | `~/.local/bin/obs-tidy` | Cleans obsidian vault, posts a Slack summary via `SLACK_OBS_TIDY_WEBHOOK` env. **Script lives only at `~/.local/bin/obs-tidy` (3.5 KB bash, calls Claude CLI against `~/obs-garner/Garner`).** |
| `com.alex.obs-digest.plist` | Mon–Fri 07:00 | `~/.local/bin/obs-digest` | Digests previous business day's notes, posts to Slack via the same webhook. Reads `~/obs-garner/Garner`. |
| `com.garner.devn-relay.plist` | KeepAlive (always-on) | `socat TCP-LISTEN:54777 → 127.0.0.1:54776` | The relay that exposes the corporate Bifrost-style devn proxy on the LAN — this is the upstream the FastAPI proxy points at via `ANTHROPIC_API_BASE`. |

For these, the question is more "should this be in a repo at all" than "Linux port" — currently if this Mac dies, the scripts and plists are gone.

### Homebrew-managed (not user-controlled scheduling)

| Plist | What |
|---|---|
| `homebrew.mxcl.ollama.plist` | Native Ollama service launched by `brew services start ollama`. Runs in addition to the Docker `nexus-ollama` container. May be redundant on Mac. |

### Suggested actions per category

1. **`agents-nexus/launchd/` jobs** — straightforward port. Add `nightly-gl-reviews.{service,timer}` and `nightly-gl-reviews-prune.{service,timer}` to `tmux/linux/systemd/`, mirroring the existing `nightly-obs-tag` files. Update `install.sh` to enable them.

2. **`com.garner.agent-memory.flush.plist`** — once the mini-PC is the canonical mnemon host, add a `agent-memory-flush.{service,timer}` (every 2 min) under `tmux/linux/systemd/` and check the source plist into `claude-agents-tmux` properly. Mac can stop running it.

3. **`com.garner.guilty-spark.nightly.plist`** — the guilty-spark repo owns this. If the mini-PC takes over spark indexing (per PLAN.md), port it to a systemd timer in *that* repo, not agents-nexus.

4. **`com.alex.obs-{tidy,digest}.plist` + the `~/.local/bin` scripts** — these are real automation that should be in a repo. Two options:
   - Move scripts into `agents-nexus/scripts/` and the plists into `agents-nexus/launchd/`. Add `obs:tidy` / `obs:digest` Taskfile entries. Then they're symmetric with the others and Linux gets them for free.
   - Leave them ad-hoc, but at minimum back them up somewhere.

   Either way, **the Slack webhook URL is currently hardcoded in the plist `EnvironmentVariables` block.** Don't commit the plists with the URL inline — move the webhook to `.env` (or `~/.tmux/env.sh`) and reference `${SLACK_OBS_TIDY_WEBHOOK}` in the plist. Rotate the webhook once committed since it's been visible in plain text on disk.

5. **`com.garner.devn-relay.plist`** — Mac-only (it's the bridge to the corporate dev network's proxy). No Linux equivalent needed unless the mini-PC also needs to reach corporate Bifrost, which probably isn't the case at home.

6. **`homebrew.mxcl.ollama.plist`** — Mac has both a native and Docker Ollama running. Pick one; the Docker one is what the rest of the stack already uses. If you stop the brew service, free up port 11434 collisions and reduce memory load.

## Things that DO already work cross-platform

For sanity:

- `task` recipes — same Taskfile.yml everywhere, no shell-specific bits
- Docker Compose stack — both `docker-compose.yml` and `docker-compose.work.yml` are platform-neutral
- Proxy + Langfuse — runs in Docker, works identically
- mnemon MCP server — cross-platform Python; the only OS-specific detail is path resolution in `~/.tmux/memory-recall.py` etc., which uses `pathlib`
- Spark — Docker SSE, fine cross-platform
- The agent-registry mechanism (`~/.tmux/registry/` files keyed by pane ID) is platform-agnostic

## Useful one-liners

```bash
# Show all drift between Mac runtime and agents-nexus checked-in copies
diff -r ~/.tmux/ tmux/mac/tmux-scripts/ --exclude='*.json' --exclude='*.log' 2>&1 | head -100

# List launchd plists vs systemd units side-by-side
ls launchd/ tmux/linux/systemd/

# What scripts would Linux symlink from mac if you ran install.sh today?
ls tmux/mac/tmux-scripts/ | sort
ls tmux/linux/tmux-scripts/ | sort   # only the ones in here are Linux-specific
```

## Open questions (from PLAN.md, still relevant)

- Tailscale MagicDNS subdomains vs Caddy path routing
- SSH-over-MCP for mnemon (current) vs converting mnemon to SSE (cleaner, more work)
- Auth in front of Caddy (basic auth? Tailscale ACLs?)
- Disk sizing for spark index across all repos

These are only blockers if you're actually exposing services off-LAN tonight; otherwise they can wait.
