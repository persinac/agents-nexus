Fire a goal off to the fleet to complete **without babysitting** — either a full Conductor mission (default: decomposes + fans out a network of worker agents) or a single delegated background agent (`-bg`). Fire-and-forget: it runs headless, coordinates over herdr + the Slack bus, and reports back durably.

Arguments: $ARGUMENTS
- `/distribute <goal>` — hand `<goal>` to the **Conductor**: it classifies, plans, fans workers into a tiled `mission/<slug>` herdr bucket, verifies (reviewer fleet), synthesizes, and reports to Jira/MR/Slack.
- `/distribute -bg <goal>` — **delegate** to ONE durable background fleet agent (lighter; no fan-out). Good for a self-contained job.
- `/distribute --sdlc <ticket|goal>` — drive the **the org sdlc pipeline** autonomously (requirements → domain-model? → tech-design → validation → plan) through to `plan.md`, then hand the code phase to a human. Uses the plugin's `scan.py` as the router; the Conductor answers the pipeline's forks itself.
- Recurring: wrap with `/loop` — `/loop 2h /distribute <goal>` re-dispatches every 2h; a bare `/distribute` is one-shot.

## How to run it

Parse `$ARGUMENTS`: if the first token is `-bg` / `--bg` / `-delegate`, strip it → **DELEGATE** mode; otherwise → **DISTRIBUTE** (Conductor) mode. The remainder is the `<goal>`. If `<goal>` is empty, ask for it — don't dispatch an empty mission.

### DISTRIBUTE (default) — a Conductor mission
Run with the **Bash tool** (fire-and-forget — it returns immediately after dispatching a DETACHED conductor):
```bash
"$AGENTS_NEXUS_DIR/agent-runner/.venv/bin/python" \
  "$AGENTS_NEXUS_DIR/agent-runner/conductor.py" --distribute "<goal>"
```
It prints the mission bucket + agent name. The detached conductor runs the full pipeline (classify → plan → fan workers into the `mission/<slug>` bucket → verify → synthesize → report), then on success **closes its own bucket** (ephemeral) and posts a ✅/⚠️ completion ping to the Slack bus. Report the bucket name to the user and that it's running detached — then **RETURN; do not wait or poll**.

Preview without spawning: append `--dry-run`.

### DELEGATE (`-bg`) — one durable background agent
For a self-contained task that doesn't need a fan-out. Spawn ONE headless fleet agent with the goal as its seed prompt, into a `delegate/<slug>` bucket, then return:
1. **cwd:** if the goal names a repo under `$REPO_DIR`, use that checkout; else the current directory.
2. **slug:** a short kebab slug of the goal; `name=delegate-<slug>`.
3. Spawn (Bash tool) — honors the caller's substrate (herdr → the bucket; tmux → a detached window):
   ```bash
   "$HOME/.tmux/substrate.sh" spawn "delegate-<slug>" "<cwd>" \
     "env PROJECT_SLUG=delegate-<slug> SEED_PROMPT='<goal>. Work autonomously and headless; when done, relay a short result summary to the Slack bus with: agent-send.sh --relay \"...\".' $HOME/.tmux/open-claude.sh" \
     --workspace "delegate/<slug>"
   ```
   (Escape single quotes in `<goal>` for the `SEED_PROMPT='…'`.) Report the agent name + bucket, then **RETURN**. Tear it down when done with `"$HOME/.tmux/substrate.sh" workspace-close "delegate/<slug>"`.

### SDLC (`--sdlc`) — drive the sdlc pipeline to plan.md
For a feature that should go through the staged SDLC pipeline (spec artifacts before code). Runs a **scan-driven staged mission**: `scan.py` says the next `/sdlc:` leaf, the Conductor runs it headlessly (reading + following the leaf's SKILL.md, answering forks itself), re-scans, and repeats until `plan.md` is ready — then stops and hands the code phase to a human (trust boundary = plan). Each stage is a DB-tracked subtask; it commits the artifact set to a docs MR + files a Jira mission issue + pings Slack; forks it can't answer escalate to the Slack bus.
```bash
"$AGENTS_NEXUS_DIR/agent-runner/.venv/bin/python" \
  "$AGENTS_NEXUS_DIR/agent-runner/conductor.py" --sdlc "<ticket|goal>"
```
Preview the resolved project + stage plan without running anything: append `--dry-run`. Requires the `project-context-*` repos cloned under a workspace root (set `CUSTOM_WORKSPACE_ROOTS` or `sdlc.workspace_root` in `conductor.yaml`). Mac-only (the sdlc plugin lives there).

## Notes
- **Durable + no babysitting.** Both modes run headless and survive this session. Completion surfaces on the Slack bus — the Conductor's ✅/⚠️ ping, or the delegate's `--relay`. Address a running agent by name over the bus (`agent-send.sh <name> "<msg>"`), or open its bucket in herdr.
- **Distribute vs delegate.** Distribute = a Conductor mission (decompose → fan out → verify → Jira/MR) — use for multi-step or needs-review work. Delegate (`-bg`) = one agent doing the whole thing — use for a quick, self-contained job.
- **Recurring.** `/loop <interval> /distribute <goal>` (or `-bg`) re-fires on a cadence — each tick dispatches fresh. Bare = one-shot + notify.
- A goal carrying a Jira key (e.g. `FC-1234`) makes it a **ticket-sourced** mission — the Conductor comments on + transitions the source ticket and files a tracking issue under the Claude Queue epic.
