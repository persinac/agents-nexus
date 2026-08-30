Spawn ONE disposable, unattended "cattle" agent to do a self-contained task headless — fire-and-forget from the **overseer** pane you're chatting from. No fan-out, no chat loop: the cattle runs with `--dangerously-skip-permissions`, does the task, sends exactly ONE line back to you when it finishes (or gets stuck), then stops. You never poll it.

This is the same primitive `/distribute -bg` uses for its DELEGATE mode, with three changes: a `bg-` session prefix (cost-class ceiling, staying on your Anthropic subscription — never `ds-`/DeepSeek, that's API-metered and out of scope for this pattern, see `docs/deepseek-routing.md`), a lean `NEXUS_CONTEXT_MODE=pointer` launch, and a mandatory one-line callback baked into the seed instead of relying on the agent to remember to report.

Arguments: $ARGUMENTS — the task. If empty, ask for it — don't spawn an empty cattle.

## Before you use this

Your OWN pane needs to survive being idle while cattle run: `overseer-reap.sh` drops the
name-based `@orchestrator` exemption whenever `REAP_ALL=1` is set (this box's Linux systemd
timer sets it). If you haven't already, pin yourself once:

```bash
scripts/agent-keep.sh "$AGENT_FROM"   # or your pane's registered name — @keep survives REAP_ALL=1
```

## How to run it

1. **cwd**: if the task names a repo under `$REPO_DIR`, use that checkout; otherwise the current
   directory. Same rule `/distribute -bg` uses.
2. **slug**: a short kebab-case slug of the task. `name="bg-cattle-<slug>"`.
3. **Your own callback address**: `open-claude.sh` already exported `$AGENT_FROM` into this
   session at launch (falls back to `$PROJECT_SLUG`/`$MY_NAME`). Use it — don't ask the user for
   it unless it's genuinely unresolvable (e.g. this session wasn't launched via the substrate).
4. **Spawn** (Bash tool; escape single quotes in `<task>` the same way `/distribute -bg` already
   asks you to escape them in `<goal>`):
   ```bash
   TARGET="${AGENT_FROM:-$PROJECT_SLUG}"
   "$HOME/.tmux/substrate.sh" spawn "bg-cattle-<slug>" "<cwd>" \
     "env PROJECT_SLUG=bg-cattle-<slug> NEXUS_CONTEXT_MODE=pointer CLAUDE_EXTRA_ARGS='--dangerously-skip-permissions' SEED_PROMPT='<task>. Work autonomously and headless: do not ask questions, do not wait for approval, do not chat back and forth. When you finish — success, failure, or genuinely stuck — send EXACTLY ONE line with: agent-send.sh \"$TARGET\" \"<one-line result>\" — then stop. Never send a second message.' $HOME/.tmux/open-claude.sh" \
     --workspace "cattle/<slug>" --no-focus
   ```
   - `--no-focus`: this is background fan-out, not the interactive picker — don't yank the user's view.
   - `CLAUDE_EXTRA_ARGS='--dangerously-skip-permissions'`, **not** `CLAUDE_PERMISSION_MODE=bypassPermissions`.
     `open-claude.sh` documents why: the `bypassPermissions` consent dialog "is persisted nowhere in
     `~/.claude.json`, so it returns every session" — useless with nobody there to answer it. The flag
     is the one that comes up running.
   - Do **not** add `@keep` or `@cohort` to the cattle pane. Both are always-honored by the reaper,
     even under `REAP_ALL=1` — tagging a cattle makes it immortal, which defeats "disposable." Leaving
     it untagged means the existing idle reaper is a free safety net: if the callback never fires
     (stuck at a prompt, crashed to idle), it still gets checkpointed and closed after ~4h.
5. **Report the agent name + bucket to the user, then RETURN.** Do not wait, do not poll `/agents`
   in a loop — the callback in step 4 is how you'll know it's done.
6. **When the callback arrives** (a message from `bg-cattle-<slug>` in your own session), tear the
   bucket down:
   ```bash
   "$HOME/.tmux/substrate.sh" workspace-close "cattle/<slug>"
   ```

## What this does NOT cover

No wall-clock cap. If a cattle gets stuck in a genuinely active-looking loop (not idle — the reaper
only ever catches idle), nothing kills it today except the proxy's own token/USD ceiling, which is
deliberately loose (tuned ~8-18x above the busiest real session — a backstop, not a budget). See
`docs/overseer-cattle.md` for the full writeup and why this is an accepted, explicit gap rather than
a silent one.
