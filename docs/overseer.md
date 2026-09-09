# Overseer — idle-agent reaper + Slack hygiene

The "overseer" is not a single daemon. By design it's **split by concern** so the
core (reaping) works even when Slack isn't installed:

| Concern | Where it lives | Trigger |
|---|---|---|
| Close agents idle >4h (checkpoint first) | `scripts/overseer-reap.sh` | scheduled timer (opt-in) |
| Prune stale Slack cards answered in the CLI | `slack-bridge/index.js` (`pruneStaleCards`) | `setInterval` in the bridge |
| Route unaddressed Slack messages to an agent | `slack-bridge/index.js` | inbound message handler |

Slack-dependent features live in the bridge (opt-in peripheral); the reaper is
standalone and Slack-independent.

---

## The reaper (`scripts/overseer-reap.sh`)

Every run it scans `~/.tmux/registry/*` and, for each agent that is **finished
(`@waiting=2`) or stuck waiting on input (`@waiting=1`)** and has been **idle
≥ `REAP_IDLE_SECS` (default 4h)**, it:

1. Runs a **final memory checkpoint** over the agent's newest transcript
   (`scripts/checkpoint-transcript.sh` — the same haiku curator the auto-checkpoint
   Stop hook uses), then
2. **closes the window** (`tmux kill-window`). The existing pane-died hooks
   (`agent-deregister.sh` + `worktree-cleanup.sh`) handle registry + worktree cleanup.

Idle time = `now − @last_tool` (set on every tool use), falling back to the
registry `AT` if `@last_tool` is unset. Actively-working agents (`@waiting=0`)
and dead panes are skipped.

### Pre-reap self-checkpoint (15 min before the reap)

The step-1 checkpoint above is a *post-hoc transcript scrape* — a haiku curator
reads the tail of a dead-ish conversation and guesses what mattered. A **live**
agent knows far more (its branch, uncommitted work, the decision it just made,
what's left). So `PREREAP_LEAD_SECS` (default **900 = 15 min**) *before* the reap
deadline, the reaper **nudges the still-live agent to checkpoint itself**:

- When an agent's idle time enters `[REAP_IDLE_SECS − PREREAP_LEAD_SECS,
  REAP_IDLE_SECS)`, the reaper injects a checkpoint instruction into its pane
  (`substrate send` → the same keystroke path the A2A bus uses). A fully idle
  agent checkpoints **without a human** — that's the "programmatically kick it
  off" part.
- Only **`@waiting=2`** (finished, sitting at a ready prompt) is nudged.
  `@waiting=1` is a **permission/elicitation prompt** — injecting a sentence
  there would answer the menu with garbage — so those keep the plain
  scrape-at-reap path. Tunable via `PREREAP_WAIT_STATES`.
- The nudge wakes the agent, which bumps `@last_tool` and **resets the idle
  clock**. To keep the reaper reaping, the warning is recorded in a
  reaper-owned marker file under `~/.tmux/overseer-prereap/` (*not* a substrate
  option — the herdr `@`-opt read lags through the substrated cache). Past the
  warning it's this marker's **deadline** (`PREREAP_LEAD_SECS` after the nudge),
  not the reset idle clock, that drives the reap.
- If a human **genuinely re-engages** after the warning (`@last_tool` advances
  more than `PREREAP_ACTIVITY_SLOP` seconds past the nudge, i.e. beyond what the
  triggered checkpoint itself would produce), the reap is **cancelled** and the
  agent re-earns a full idle lease (logged `prereap-cancel`). An agent observed
  actively working (`@waiting=0`) likewise drops its marker.

The post-hoc transcript scrape at reap time (step 1) still runs as the
**fallback** — so if the nudge never lands (agent stuck, send fails), no state is
lost. Set `PREREAP_ENABLED=0` to disable the pre-reap nudge and keep only the
scrape-at-reap behavior. Log lines: `prereap-warn`, `prereap-cancel`, and the
reap `reason=prereap-deadline(…)`.

### What it will NEVER reap (the command post)

The window you drive from must never be closed under you. It is protected four ways:

1. **By name** — windows named `overseer` or `orchestrator` are always skipped.
2. **By tag** — any window with `@orchestrator` set to `1` (the `overseer`
   launcher self-tags; you can tag any window: `tmux set-option -w @orchestrator 1`).
3. **Attached + viewed** — the window an attached client is currently looking at.
4. **Exclude list** — anything in `$REAP_EXCLUDE` (csv of names/panes) or
   `~/.tmux/overseer-exclude` (one name or `%pane` per line, `#` comments allowed).

### The dedicated overseer window

Run `overseer` (shell function in zshrc/bashrc) to open — or jump to — a
dedicated command-post window. It registers as `overseer`, self-tags
`@orchestrator`, and is therefore exempt from reaping. Drive your fleet from
here; the agents you spawn are separate, reapable windows.

### Protecting working agents — `@keep` (one) and `@cohort` (a design)

The command-post rules above are about not closing the window you drive from. To stop
the reaper closing **worker agents** that are legitimately mid-task but idle between
turns (waiting on the bus, on another agent, or on you):

- **One agent — `@keep`.** Pin a single window so it's never reaped, even under
  `REAP_ALL=1`:
  ```
  scripts/agent-keep.sh <name|slot|%pane>        # pin   (@keep 1)
  scripts/agent-keep.sh <name|slot|%pane> off    # unpin
  scripts/agent-keep.sh                          # list pinned
  ```

- **A whole design — `@cohort`.** When one orchestrator drives a multi-repo design
  across several agents, tag them as a named cohort and release the whole group in one
  shot when it ships. Any window with a non-empty `@cohort` is protected like `@keep`
  (always honored, even under `REAP_ALL=1` — the sweep that otherwise picks off a
  design whose agents sit idle between bus round-trips):
  ```
  scripts/agent-cohort.sh hold <design> <agent…|all>   # protect the working set
  scripts/agent-cohort.sh list                         # active cohorts + members + held-for
  scripts/agent-cohort.sh release <design>             # design shipped → normal reaping resumes
  scripts/agent-cohort.sh release <design> <agent…>    # drop specific members
  ```
  `@keep` and `@cohort` are independent, so releasing a design never unpins a
  manually-kept window. A forgotten cohort surfaces in the reaper log
  (`cohort-held(stale)`) once idle past `COHORT_WARN_SECS` (default 24h) rather than
  becoming immortal.

### Letting an agent cook — `@autoaccept`

A **different axis** from the two above: `@keep`/`@cohort` decide whether the reaper
closes a window; `@autoaccept` decides whether the **permission gate** stops to ask.
All three are independent tags, so flagging an agent never changes whether it gets
reaped, and pinning one never changes how its prompts are answered.

```
scripts/agent-auto.sh <name|slot|%pane>        # flag   (@autoaccept 1)
scripts/agent-auto.sh <name|slot|%pane> off    # unflag
scripts/agent-auto.sh                          # list flagged
```

A flagged pane skips `notify-classify.py`'s model call entirely and auto-approves.
That LLM tier is the one that turns a classifier timeout into a spurious denial, so
removing it is what lets an unattended agent keep working through a flaky network
instead of stopping on a prompt it never should have seen.

**Still asks a human**, flag or no flag: everything `_bash_is_denied` refuses — `rm`,
`kubectl delete`, `DROP TABLE`/`DELETE FROM`, `terraform destroy`, `git push --force`,
`doppler secrets set`. A match falls through to the normal classifier path. Also
untouched: `AskUserQuestion`, which is not a permission decision but the agent asking
*you* something — auto-clearing it would swallow the question and strand the pane.

**Accepted trade, stated plainly:** on a flagged pane a `Write` to a credential-ish
path (`.env`, `*.pem`, kubeconfig) and a mutating MCP call (Slack post, Trello write)
are auto-approved. Only the Bash denylists hold the line. The PreToolUse hooks
(`block-destructive.sh`, `block-credential-dump.sh`) still apply regardless.

Two things it does **not** do:

- It cannot rescue a **native** Claude Code auto-mode denial. Those hard-deny and never
  raise an answerable prompt (see `automode-watchdog.py`), so there is nothing for this
  gate to answer. Run flagged panes in **Manual** mode, where every call raises a prompt
  and lands here.
- It does not bypass PreToolUse. `block-destructive.sh` still refuses a `kubectl delete`
  even under `--dangerously-skip-permissions`.

Approvals are logged as the `autoaccept` tier in `~/.tmux/gate-decisions.log`, so
`tmux-scripts/gate-report.sh` breaks them out under `by tier` with no change needed
there. `NEXUS_AUTOACCEPT=1|0` overrides the pane option (used by the test suite).

### Config (env)

| Var | Default | Meaning |
|---|---|---|
| `REAP_IDLE_SECS` | `14400` (4h) | idle threshold |
| `REAP_DRY_RUN` | `0` | `1` = log decisions, close nothing (also suppresses the pre-reap nudge) |
| `REAP_EXCLUDE` | _(empty)_ | csv of extra names/panes to protect |
| `REAP_ALL` | `0` | `1` = prune everything idle, command post included (see below) |
| `TMUX_SESSION` | `agents` | session to scan |
| `COHORT_WARN_SECS` | `86400` (24h) | a held `@cohort` idle past this is logged (not reaped) as stale |
| `PREREAP_ENABLED` | `1` | `0` = disable the pre-reap self-checkpoint nudge (keep only scrape-at-reap) |
| `PREREAP_LEAD_SECS` | `900` (15m) | how long before the reap deadline to nudge the agent to checkpoint |
| `PREREAP_WAIT_STATES` | `2` | which `@waiting` states to nudge (`2`=finished-idle; `1`=perm prompt, unsafe) |
| `PREREAP_ACTIVITY_SLOP` | `300` (5m) | `@last_tool` advancing >this past the nudge = human re-engaged → cancel the reap |
| `PREREAP_CHECKPOINT_MSG` | _(built-in)_ | the one-line instruction injected into the agent's pane |

### `REAP_ALL` — unattended "leave it for days" boxes

By default the reaper protects your command post (Mac: you're actively driving
it). For a box that just sits idle — the personal Linux mini-pc — set
`REAP_ALL=1` to prune **everything** idle, command post included, so the whole
box gets checkpointed and cleaned without manual hygiene. It still honors
`~/.tmux/overseer-exclude` / `$REAP_EXCLUDE`, and still won't yank a window an
attached client is actively viewing (so an SSH session you're looking at is
safe; everything gets cleaned once you detach and it goes idle).

**The Linux systemd unit sets `REAP_ALL=1` by default; the Mac launchd job does
not** (Mac = active driver, command post protected).

Decisions + actions are logged to `~/.tmux/overseer-reap.log`; each reap also
appends a `reap` event to `~/.tmux/apm.log`.

### Try it safely first

```bash
task overseer:reap:dry          # log what WOULD be closed, change nothing
REAP_IDLE_SECS=0 task overseer:reap:dry   # ...treating every idle agent as over-threshold
cat ~/.tmux/overseer-reap.log
```

### Enable the schedule (opt-in — it closes windows)

Units live under `optional/` so the normal installers do **not** turn them on.

```bash
# macOS (launchd) — every 15 min
task launchd:install:overseer-reap
# uninstall: task launchd:uninstall:overseer-reap

# Linux (systemd user timer) — every 15 min
NEXUS_DIR="$(pwd)"
for u in overseer-reap.service overseer-reap.timer; do
  sed -e "s|__HOME__|$HOME|g" -e "s|__AGENTS_NEXUS_DIR__|$NEXUS_DIR|g" \
    tmux/linux/systemd/optional/$u > ~/.config/systemd/user/$u
done
systemctl --user daemon-reload
systemctl --user enable --now overseer-reap.timer
# disable: systemctl --user disable --now overseer-reap.timer
```

**Before enabling, make sure your command post is protected** — open it with
`overseer`, or tag your current window: `tmux set-option -w @orchestrator 1`.

---

## Slack card pruning

The bridge posts a card when an agent needs input. Cards answered **in Slack**
self-resolve, but a prompt answered **locally in the CLI** (PreToolUse clears the
pane's `@wait_since`) or an agent whose window closed used to leave the card
orphaned until its 7-day TTL — the "12 stale prompts in a thread" problem.

`pruneStaleCards()` runs every `SLACK_PRUNE_INTERVAL_MS` (default 10s) and applies
the same staleness test as the same-prompt guard across all tracked cards,
deleting the dead ones (and the agent anchor when its last pending card clears).
No config needed; it's on whenever the bridge has a channel configured.
