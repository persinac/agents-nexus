# Multi-vendor agents: adding Codex CLI to the nexus fleet

**Status:** plan / build tonight (2026-07-20)
**Author:** general@alex-nexus
**Scope:** run OpenAI's Codex CLI (`codex` 0.144.6) as a first-class citizen alongside
Claude Code agents — in ad-hoc use, in the conductor's verify stage, as a conductor
DAG worker, and (last) as an interactive bus agent.

---

## TL;DR — why this is mostly wiring, not surgery

The fleet has **two** spawn paths, and "claude" is hard-wired into exactly **one brain
in each** — everything underneath is already vendor-neutral:

| Layer | Vendor-coupled? | Where |
|---|---|---|
| Interactive launcher | **yes** | `tmux/mac/tmux-scripts/open-claude.sh` → `exec claude` (`:277`/`:279`) + claude flags |
| Conductor worker brain | **yes** | `agent-runner/runner.py` uses `claude_agent_sdk` (`:232`, preset `claude_code` `:276`) |
| Spawn substrate | no | `substrate.sh spawn <name> <cwd> <cmd>` runs *any* command in a pane + registers it (`:143`) |
| Messaging / bus | no | `substrate.sh send-keys/send-text` (`:223`–`:243`), `agent-send.sh`, registry — route by **name** |
| Result gathering | no | `conductor_worker.py` writes a `{status,summary,artifacts,handoff}` DB row (`:55`–`:64`); DAG polls it |
| Profiles | no | `conductor.yaml → profiles`; orchestrator picks one per subtask; `run_worker(st,profile,effort)` (`conductor.py:367`) |

So mixed-vendor spawning = teach the **profile** what vendor it is, and branch the one
brain that runs the subtask. The substrate, bus, registry, DB, and DAG are untouched.

**Codex is ready on this box:** `~/.codex/auth.json` present (provider `openai`),
`config.toml` already sets `trust_level = "trusted"` for `/home/persinac` (no trust
dialog), config parses clean.

**Two codex flags do the heavy lifting** (confirmed in `codex exec --help`):
- `--output-schema <FILE>` — force the final message to match a JSON Schema → no brittle parsing.
- `-o, --output-last-message <FILE>` — write the final message to a file we read back.
- plus `-C/--cd <DIR>`, `-s/--sandbox {read-only,workspace-write,danger-full-access}`,
  `-a/--ask-for-approval <policy>`, `--skip-git-repo-check`, `-m/--model`, `-c key=val`.

---

## Pre-flight (do FIRST — ~10 min, blocks everything else)

### P1. Fix the install/PATH split  *(blocking for `codex update`, not for use)*
`codex doctor` reports the running binary is `/usr/local/lib/node_modules/@openai/codex`
but the npm global root is the fnm path
`~/.local/share/fnm/node-versions/v24.15.0/.../@openai/codex`. `codex update` / `npm -g`
would touch a *different* install than what's on `$PATH`.
- **Decide:** keep the `/usr/local` copy (remove the fnm one) **or** switch `$PATH`/npm
  prefix to the fnm one. Recommend keeping `/usr/local/bin/codex` (already first on PATH)
  and `npm -g uninstall @openai/codex` from the fnm node, or pin updates to the running root.
- **Verify:** `codex doctor` → `install` and `updates` rows go ✓.

### P2. Confirm auth + a smoke exec
```bash
codex exec --skip-git-repo-check -s read-only \
  -o /tmp/codex-smoke.txt "Reply with exactly: NEXUS_OK" && cat /tmp/codex-smoke.txt
```
Expect `NEXUS_OK`. If it prompts for login → `codex login` (ChatGPT) first.

### P3. Share the fleet MCP stack with codex  *(makes codex memory-aware — Tier 0 dep)*
Claude agents load MCP from `~/.claude.json` (`mcpServers`) + repo `.mcp.json` (spark).
Mirror the two that exist on this box into codex:
```bash
# read the exact launch command/env from ~/.claude.json first, then:
codex mcp add agent-memory --env <K=V…> -- <command from ~/.claude.json mcpServers.agent-memory>
codex mcp add spark        --env <K=V…> -- <command from .mcp.json spark>
codex mcp list   # confirm both register
```
> `codex mcp add <NAME> [--env K=V]… -- <COMMAND>…` (stdio) or `--url <URL>` (HTTP).
> agent-memory is the shared project memory — same `project: agents-nexus` notes the
> claude fleet reads. This is what makes a codex agent a *citizen* and not a silo.

### P4. Decisions to lock before coding (defaults in **bold**)
- **Model:** `-m gpt-5-codex` (or whatever `codex` defaults to post-login) — leave to
  codex default unless a subtask needs a specific one.
- **Sandbox policy by profile permission:** `read-only → -s read-only`;
  `standard → -s workspace-write`; never `danger-full-access` from the conductor.
- **Approval:** headless workers run `-a never` (non-interactive); rely on the sandbox
  as the guardrail, not human approval.
- **Naming:** codex agents/workers get a `cx-` prefix (vs claude `cw-`) so they're
  distinguishable in `peers` / registry / logs.
- **Observability gap (accept for now):** nexus-proxy:4000 → Langfuse is Anthropic-shaped;
  codex→OpenAI traffic will NOT appear in Langfuse. Codex logs locally to
  `~/.codex/logs_2.sqlite`. Unified tracing is out of scope tonight (see Open Questions).

---

## Tier 0 — Use codex alongside the fleet (0 repo code)

**Goal:** codex is usable today for real tasks and shares fleet memory.
**Depends on:** P1–P3.

- Ad-hoc interactive: `cd <worktree> && codex` (trusted dir, no dialog).
- Ad-hoc headless: `codex exec -C <dir> -s workspace-write "<task>"`.
- Memory-aware: after P3, in a codex session it can call `agent-memory` /`spark` tools —
  same notes the claude fleet writes. Test: ask codex to
  *"search agent-memory (project agents-nexus) for the OmniRoute audit"* → it should find it.
- **Optional:** drop an `AGENTS.md` (codex's `CLAUDE.md` analog) at repo roots you'll use
  codex in, so it inherits project context. Can start as a symlink/copy of `CLAUDE.md`.

**Verify:** codex completes a trivial edit in a scratch worktree and recalls a fleet memory note.
**Rollback:** none (nothing changed in the repo). `codex mcp remove <name>` to unshare.

---

## Tier 1 — Codex as a cross-vendor reviewer in the verify stage  ⭐ recommended first build

**Goal:** one of the conductor's N adversarial reviewers is Codex → genuine second-model
opinion (catches what Opus misses). **Smallest surface: read-only, verdict-only, no
DB-writeback contract.**
**Depends on:** P1–P2. **Files:** `agent-runner/conductor.py`, `~/.tmux/conductor.yaml`.

### The seam
`verify_mission()` (`conductor.py:628`) builds `n` reviewers and gathers
`review_one(...)` (`:597`), each returning `{"lens","verdict":{"pass":bool,"findings":[…]}}`.
Tally code counts `verdict.pass` and flattens `findings`. **Any reviewer that returns the
same shape drops in unchanged.**

### Changes
1. **`conductor.yaml → policy.reviewer`:** add `codex: 1` (how many of `count` reviewers
   run on codex). `0` = off (pure-claude, current behavior).
2. **`conductor.py`:** add `review_one_codex(mid, goal, summaries, probes, lens, cwd)` that
   mirrors `review_one` but shells codex instead of the SDK:
   ```python
   # verdict.schema.json:  {pass:bool, findings:[{severity,where,what}]}
   async def review_one_codex(mid, goal, summaries, probes, lens, cwd):
       prompt = _reviewer_prompt(lens, goal, summaries, probes, cwd)  # reuse review_one's text
       out = tempfile.mktemp(suffix=".json")
       cmd = ["codex","exec","-C",cwd,"-s","read-only","--skip-git-repo-check",
              "-a","never","--output-schema",VERDICT_SCHEMA,"-o",out, prompt]
       r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=600)
       try:    verdict = json.load(open(out))
       except Exception as e:
           verdict = {"pass":False,"findings":[{"severity":"major","where":lens,
                      "what":f"codex reviewer failed: {e}; stderr={r.stderr[:200]}"}]}
       return {"lens":f"{lens}(codex)","verdict":verdict}
   ```
   Factor `review_one`'s prompt string into `_reviewer_prompt(...)` so both vendors share it.
3. **Route in `verify_mission`:** after computing `lenses`, send the last `k = CFG…reviewer.codex`
   indices to `review_one_codex`, the rest to `review_one`:
   ```python
   k = int(POLICY.get("reviewer",{}).get("codex",0))
   tasks = [ (review_one_codex if i >= n-k else review_one)(mid,goal,summaries,probes,lenses[i],cwd)
             for i in range(n) ]
   reviews = await asyncio.gather(*tasks)
   ```
   Do the same one-liner in `review_plan`'s gather (plan-gate) if you want codex on the
   pre-build gate too — optional tonight.

### Alternative (even smaller, if short on time)
Skip the schema plumbing and use **`codex review --uncommitted`** (or `--base <branch>`)
in the building worktree as an *extra advisory probe* appended to `probes[]` — it reviews
the actual diff. Less structured (free-text), but zero new reviewer code. Good fallback.

**Verify:** run a tiny mission with `reviewer.codex: 1`; conductor log shows a
`…(codex)` lens in the reviewer tally; verdict JSON parses; majority math still works.
**Rollback:** set `reviewer.codex: 0` (pure-claude). Code paths dormant.
**Risk:** codex reviewer latency/timeout — the `except` returns a soft-FAIL finding, so a
hung codex can't silently pass a bad mission (it fails closed). Tune `timeout`.

---

## Tier 2 — Codex as a conductor DAG worker (the literal "mixed-vendor conductor")

**Goal:** the orchestrator can *assign a subtask to codex* by choosing a codex profile;
codex builds it and reports the standard result row.
**Depends on:** Tier 1 patterns (schema+exec). **Files:** `~/.tmux/conductor.yaml`,
`agent-runner/conductor.py` (`run_worker`).

### The seam — branch `run_worker`, NOT `spawn_worker`
Keep `conductor_worker.py` as the single uniform entry: it already registers in the fleet
registry, joins the mission cohort, writes the DB result row, and self-deregisters
(`:31`–`:66`). We do **not** want to reimplement that for codex. So the vendor branch goes
*inside* `run_worker(st, profile, effort)` (`conductor.py:367`), which is the "how do we
actually execute this subtask" function. `spawn_worker` and the DB contract stay identical.

### Changes
1. **`conductor.yaml → profiles`:** add codex variants (or a `vendor:` key on existing ones):
   ```yaml
   backend-build-codex:
     vendor: codex               # NEW — absent/`claude` = current SDK path
     tools: [Bash, Edit, Write, Read, Grep, Glob]   # advisory; codex uses its own toolset
     mcp: [agent-memory, spark]  # mirrored into codex via `codex mcp add` (P3)
     permission: standard        # → -s workspace-write
     verify: { mode: code, checks: [tests] }
   ```
2. **`conductor.py:run_worker`:** at the top, branch on vendor:
   ```python
   if profile.get("vendor") == "codex":
       return await _run_worker_codex(st, profile, effort)
   # …existing claude_agent_sdk path unchanged…
   ```
3. **`_run_worker_codex(st, profile, effort)`** — run codex headless, return the SAME dict
   `conductor_worker.py` expects (`{status,summary,artifacts,handoff}`; `status` ∈
   `done|error|blocked`). Use `--output-schema` to force that shape as codex's final message:
   ```python
   # result.schema.json: {status:enum[done,error,blocked], summary:str,
   #                      artifacts:[str], handoff:str|null}
   sandbox = "read-only" if profile.get("permission")=="read-only" else "workspace-write"
   goal = st["goal"]   # execute_dag already folded [Upstream context] into this (:530)
   instr = (goal + "\n\nWhen done, your FINAL message MUST be JSON matching the schema: "
            "status=done|error|blocked, summary=what you did, artifacts=[abs paths you created/edited], "
            "handoff=one-line context for dependents (or null).")
   out = tempfile.mktemp(suffix=".json")
   cmd = ["codex","exec","-C",st_cwd,"-s",sandbox,"-a","never","--skip-git-repo-check",
          "--output-schema",RESULT_SCHEMA,"-o",out, instr]
   r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=1800)
   try:    wr = json.load(open(out))
   except Exception as e:
           wr = {"status":"error","summary":f"codex worker parse fail: {e}; stderr={r.stderr[:300]}",
                 "artifacts":[],"handoff":None}
   return wr
   ```
   - `st_cwd` = the mission worktree `run_worker` already resolves (same cwd the claude path
     uses). `workspace-write` lets codex edit files there; the branch/commit + ground-truth
     `task lint` gate in `verify_mission` is unchanged and still guards the output.
   - `cx-`-prefix the worker name in `spawn_worker` when `profile.vendor==codex` (naming, P4)
     — optional cosmetic; the DB/registry don't care.
4. **Escalation:** the existing effort-escalation loop bumps `effort` after failed verify
   rounds. Map codex effort via `-m`/`-c model_reasoning_effort=…` if desired, else ignore
   (codex picks its own). Not required tonight.

**Verify:** craft a 1-subtask mission whose subtask uses `backend-build-codex`; watch
`peers` show a `cx-…` worker; DB `subtask.result` has a valid row; `execute_dag` marks it
`done`; `verify_mission` runs the ground-truth check on codex's edits.
**Rollback:** point the mission/profile back to the non-codex profile, or remove `vendor:`.
The claude path is the default when `vendor` is absent.
**Risk:** codex model-generated commands blocking on sandbox escalation → keep `-a never`
+ `workspace-write`; watch the first real run's stderr. If a subtask legitimately needs
network/system writes, that's a profile that should stay on claude (or `danger-full-access`,
which we've decided against).

---

## Tier 3 — Interactive codex pane agent on the bus (biggest glue)

**Goal:** a codex agent lives in a herdr pane, appears in `peers`, and receives bus
messages like any claude fleet agent.
**Depends on:** nothing from Tiers 1–2 (independent). **Files:** new
`tmux/*/tmux-scripts/open-codex.sh`, a vendor switch in the launch path, `AGENTS.md`.

### What's already free
- `substrate.sh spawn`/`register`/`send-keys`/`send-text` are vendor-neutral — they'll host
  and message a codex pane exactly as a claude one.
- `agent-send.sh` + registry route by **name**; a `codex` agent named e.g. `store-front-cx`
  is addressable with no bus changes.
- `config.toml` trusts `/home/persinac` → no per-dir trust dialog to seed.

### What needs building
1. **`open-codex.sh`** — analog of `open-claude.sh`. Instead of `exec claude "${args[@]}"`:
   - resolve `MY_NAME` / `MY_HOST` the same way (reuse `open-claude.sh:85` MY_HOST logic).
   - inject base-context: codex has **no `--append-system-prompt`-as-message** convention
     like claude's opening `$prompt`. Two options:
     - (a) write the base-context preamble to `AGENTS.md` in the cwd (codex reads it), **or**
     - (b) pass it as the initial `PROMPT` arg to interactive `codex`.
     Recommend **(a) AGENTS.md** for the standing identity ("you are agent X on host Y, bus
     usage, `--exclude` self") + (b) for the per-launch task. Generate AGENTS.md from the
     same template block `open-claude.sh` uses (`:174`).
   - `exec codex -C "$cwd" [ -m "$model" ] [ "$initial_prompt" ]`.
2. **Vendor switch:** wherever open-claude.sh is invoked (herdr pane spawn / `launch-claude.sh`),
   add `AGENT_VENDOR=codex` → call `open-codex.sh`. Keep claude the default.
3. **Registry self-registration:** claude registers via its session hooks; codex won't fire
   claude hooks. Register the pane explicitly in `open-codex.sh` (call
   `substrate.sh register <pane> <name> <cwd>` on launch; the `_register_self` pattern from
   `conductor.py:460` is the reference) and rely on the pane-died hook / reaper for cleanup.
4. **Message delivery fidelity (the real risk):** the bus flattens newlines to spaces and
   does `send-text` + `enter`. Codex's TUI accepts typed input, but verify: bracketed-paste
   handling, multi-line, and that a pasted line submits cleanly. If the TUI is finicky,
   fall back to a **headless loop** (an inbox file + `codex exec resume --last`) instead of
   an interactive pane — mirrors `runner.py`'s `~/.tmux/sdk-inbox/<name>.inbox` pattern (`:33`).

**Verify:** spawn a codex pane agent, `agent-send.sh <name> "say hi on the bus"`, confirm it
receives and can `agent-send.sh` back; `peers` lists it.
**Rollback:** don't launch with `AGENT_VENDOR=codex`; `open-codex.sh` is additive.
**Risk:** highest of the tiers — TUI send-keys fidelity + no native hooks. Timebox it; if the
interactive path fights back, ship the headless-inbox variant and call Tier 3 done.

---

## Tier 4 — Vendor selection in the interactive spawn picker (fuzzy picker → branch → vendor)

**Goal:** the `ctrl+a N` fuzzy picker gains a **vendor step** — pick repo → (bucket) →
branch → **claude | codex** — so you spawn a codex agent from the same UX you spawn claude.
**Depends on:** Tier 3 (`open-codex.sh` must exist — the picker just chooses which launcher
to spawn). **Files:** `tmux/mac/tmux-scripts/launch-claude.sh` (+ linux mirror if present).

### The seam
`launch-claude.sh` is the fzf picker behind `ctrl+a N` (`tmux.conf:41` → `display-popup … launch-claude.sh`;
herdr equivalent noted in `herdr/config.toml`). Every spawn goes through `nx_spawn <name> <cwd> <cmd>`
(`:89`), and **every call site hardcodes `…/open-claude.sh`** as the cmd — 7 of them:
`[general]` `:99`, `[wt]` `:108`, extra-dir `:126`, fresh repo `:156`, "open here anyway" `:169`,
worktree `:197` (+ the general fast-path). So vendor selection = **one fzf prompt + one `$LAUNCHER` var**.

### Changes
1. **Add a vendor prompt** once, right after `selected` is chosen (before the bucket step),
   gated on codex being installed so a claude-only box is unchanged:
   ```bash
   LAUNCHER="$NEXUS_TMUX_DIR/open-claude.sh"          # default
   AGENT_VENDOR="claude"
   if command -v codex >/dev/null 2>&1 && [ "${NEXUS_VENDORS:-claude,codex}" != "claude" ]; then
     _v=$(printf 'claude\ncodex\n' | fzf --prompt='vendor> ' --height=20% --border=rounded \
                                         --header="agent runtime for $selected")
     case "$_v" in codex) LAUNCHER="$NEXUS_TMUX_DIR/open-codex.sh"; AGENT_VENDOR="codex";; esac
   fi
   ```
2. **Route every spawn through `$LAUNCHER`:** replace the 7 hardcoded `open-claude.sh` with
   `$LAUNCHER` (keep the `env PROJECT_SLUG=general` prefix on the general path, just swap the
   script). Optional: prefix codex window names (`cx-…`) for at-a-glance vendor ID in `peers`.
   Cleaner still: pass `AGENT_VENDOR` in the env and have a thin `open-agent.sh` dispatch to
   `open-claude.sh`/`open-codex.sh` — one edit instead of seven — but the `$LAUNCHER` swap is
   the lowest-risk minimal change tonight.
3. **No keybinding change.** `ctrl+a N` already runs this script; the vendor step lives inside it.

### Fast-path / default behavior
- Esc at the vendor prompt → falls to the default (`claude`) — never blocks.
- `NEXUS_VENDORS=claude` env skips the prompt entirely (opt-out for a claude-only session).
- All picker branches (general / wt / extra-dir / reuse / new-worktree) funnel through
  `nx_spawn`, so `$LAUNCHER` covers every path uniformly — no per-branch edits.

**Verify:** `ctrl+a N` → pick a repo → vendor prompt shows `claude`/`codex` → pick codex →
a codex agent opens in the chosen dir/branch and appears in `peers` (as `cx-…` if named).
**Rollback:** revert `launch-claude.sh` (or set `NEXUS_VENDORS=claude`). Additive; default is claude.
**Risk:** low — pure launcher plumbing. Real risk is entirely inherited from Tier 3 (does the
codex agent behave once spawned). Don't ship Tier 4 before Tier 3 is solid.

---

## Tier 5 — Slash-command & skill parity for codex

**Answers the two questions directly. TL;DR: the fleet's *Claude* slash commands do NOT work
in codex, but codex has its own equivalent surfaces, and — usefully — codex *skills* use the
same `SKILL.md` format as Claude skills, so skills port cleanly.**

### Will the fleet's existing slash commands work in a codex agent? → **No, not as-is.**
Claude Code reads commands from `~/.claude/commands/`, repo `.claude/commands/` /`commands/`
(`/distribute`, `/opsx:*`), and plugin `commands/*.md`. **Codex does not read any of those** —
different runtime, different paths. A codex agent starts with zero fleet commands.

### What codex *does* have (three separate surfaces)
| Surface | Where / how | Analog |
|---|---|---|
| **Built-in slash** | `/model`, `/approvals`, `/review`, … in the codex TUI | Claude built-ins |
| **Custom prompts** (slash) | `~/.codex/prompts/<name>.md` → `/<name>` in the **interactive TUI** | `~/.claude/commands/<name>.md` |
| **Skills** | `~/.codex/skills/<name>/SKILL.md` (YAML frontmatter `name`/`description` + body) — **same shape as Claude skills**; installed ad-hoc or via `codex plugin marketplace` | `~/.claude/skills/…/SKILL.md` |

> Confirmed on-disk: the bundled `openai-curated-remote` plugin ships skills as
> `…/skills/<name>/SKILL.md` with `---\nname:\ndescription:\n---` frontmatter — byte-compatible
> with the fleet's Claude skill format.

### ⚠️ Headless `codex exec` has **no** slash commands
Slash is a TUI-only feature. The conductor's codex reviewer/worker (Tiers 1–2) run `codex exec`,
so **`/foo` is N/A there** — you either **inline the command body** into the prompt or reference a
**skill**. This exactly mirrors the claude conductor worker: it never types `/skill`; the profile's
`skill:` field appends the `SKILL.md` body (`runner.py`/`conductor.py:379`). Same pattern for codex.

### How to register (recipes)
- **A codex slash command (interactive):** `mkdir -p ~/.codex/prompts && $EDITOR ~/.codex/prompts/distribute.md`
  → available as `/distribute` in a codex TUI session. (Confirm this version's arg-substitution
  token — codex uses positional `$1..$9` / `$ARGUMENTS`; verify against 0.144.6 before relying on args.)
- **A codex skill:** `~/.codex/skills/<name>/SKILL.md` with frontmatter. Because the format matches,
  fleet skills can be **symlinked**: `ln -s ~/.claude/skills/<name> ~/.codex/skills/<name>`
  (validate each — a skill that calls Claude-only tools/MCP names won't run under codex).
- **A whole marketplace:** `codex plugin marketplace add <ref>` → `codex plugin add <name>`.

### Parity strategy for the fleet (pick per need)
1. **Skills first (cheapest, highest leverage):** symlink/sync `~/.claude/skills/*` →
   `~/.codex/skills/*`. One-time or a tiny sync in `install.sh`. Covers both interactive codex
   agents AND headless codex workers (which can't use slash anyway).
2. **Port the few commands that matter** (`/distribute`, `/opsx:*`) → `~/.codex/prompts/*.md`.
   These are small; hand-port or a `commands/*.md → prompts/*.md` translator (strip Claude
   frontmatter, map arg tokens). Do this only for commands you'll actually invoke interactively.
3. **For conductor codex workers:** put the procedure in a **skill** (or the profile's inlined
   prompt), never a slash command — headless has no slash. If a fleet workflow only exists as a
   slash command today, convert its body to a skill so both vendors + both modes can use it.

**Verify:** in a codex TUI, `/` lists your `~/.codex/prompts` entries; a symlinked skill shows up
and runs; a `codex exec` worker completes a skill-driven subtask with no slash involved.
**Rollback:** `rm` the prompts/skills symlinks; `codex plugin remove <name>`. Nothing claude-side changes.
**Risk:** low, but audit symlinked skills for Claude-only tool/MCP references before trusting them
under codex (tool names and MCP server IDs must actually resolve in codex — P3 covers agent-memory/spark).

---

## Tonight's runsheet (dependency order)

1. **P1–P4 pre-flight** (~10 min) — PATH fix, smoke exec, `codex mcp add` ×2, lock decisions.
2. **Tier 0** — confirm codex + shared memory working. (~5 min)
3. **Tier 1 reviewer** — `verdict.schema.json`, `review_one_codex`, route in `verify_mission`,
   `reviewer.codex: 1`; run a tiny mission. ⭐ highest value/effort. (~45–60 min)
4. **Tier 2 worker** — `result.schema.json`, `_run_worker_codex`, `vendor:` profile, branch
   `run_worker`; run a 1-subtask codex mission. (~60–90 min)
5. **Tier 3 pane agent** — only if 1–2 land clean and there's appetite; timeboxed, headless
   fallback ready. (~60+ min, may slip)
6. **Tier 4 vendor picker** — ONLY after Tier 3 works (needs `open-codex.sh`); one fzf prompt
   + `$LAUNCHER` swap in `launch-claude.sh`. (~20–30 min)
7. **Tier 5 command/skill parity** — can start ANYTIME (independent); do the skill symlink
   early so codex agents/workers from Tiers 1–4 are actually useful. Port slash prompts later. (~30 min)

**Dependency graph:** P → T0 → {T1, T2 independent} ; T3 → T4 ; T5 independent (do skills sync early).
**Checkpoint after each tier** (git commit on a `codex-multivendor` branch/worktree +
memory note). Tiers 1, 2, and 5 are independently shippable — stopping after any is a win.

### Where the code lands
This touches `conductor.py` + `conductor.yaml` + launcher scripts — **not** routing. Do it
on a fresh `codex-multivendor` branch/worktree off `main` (don't reuse the `routing`
worktree). Schema files → `agent-runner/schemas/{verdict,result}.schema.json`.

---

## Known gaps & pre-build smoke tests

Adversarial review of this plan surfaced the following. **A–D are blocker-class** (verify
before building the dependent tier); **E–I are design gaps** (fold into the tier); **J** is an
opportunity; **K–L** are operational. Smoke-test results recorded inline as we run them.

### Blocker-class — verify BEFORE building (gates the listed tiers)

| # | Gap | Gates | Smoke test | Result |
|---|---|---|---|---|
| **A** | **Headless auth unverified.** `codex exec` may need `OPENAI_API_KEY` not ChatGPT-plan `auth.json`; and OAuth tokens expire — does refresh work in a detached/stripped-env conductor pane? | T1–T3 | `codex exec --skip-git-repo-check -s read-only -o OUT "Reply exactly: NEXUS_A_OK"`, then repeat under a minimal `env -i PATH=… HOME=…`. Inspect `jq keys ~/.codex/auth.json` for auth mode (no secret values). | ✅ **PASS** — exit 0, `NEXUS_A_OK`, both normal env **and** stripped `env -i HOME+PATH`. `auth_mode=chatgpt` (OAuth) works headless; the `OPENAI_API_KEY` in auth.json is unused. No login/refresh errors. |
| **B** | **Concurrent `codex exec` on shared `~/.codex/*.sqlite`** may lock/serialize/corrupt — the conductor fans out worker waves. | T2 | Launch 3 `codex exec` in parallel; check all exit 0 and no "database is locked" in stderr. | ✅ **PASS** — 3 concurrent `codex exec` all exit 0, correct outputs, zero "database is locked". No sqlite contention observed. |
| **C** | **Sandbox asymmetry vs claude:** `-s workspace-write` (a) disables **network** by default → breaks `npm install`-style subtasks; (b) may reject **git-worktree** writes/commits (`.git` is a file). | T2 | `codex sandbox -s workspace-write -- <net cmd>` (expect blocked); `codex sandbox -s workspace-write --cd <worktree> -- sh -c 'echo hi>f && git status'` (expect write+git OK). | ❌ **FAIL (env-level)** — network correctly blocked, but bundled bubblewrap can't init the sandbox at all: `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` (and `uid map: Permission denied` w/o `--unshare-net`). **Root cause:** Ubuntu 25.04 `kernel.apparmor_restrict_unprivileged_userns=1`. Codex can run **no** shell/edit under `-s workspace-write` here → gated T2. **✅ FIXED 2026-07-20** via option 1 (`kernel.apparmor_restrict_unprivileged_userns=0`, persisted in `/etc/sysctl.d/99-codex-userns.conf`): re-ran → `{"wrote":true,"git_ok":true,"network":"blocked"}`, file written on disk, git sees it, network correctly blocked. **T2 unblocked.** |
| **D** | **`--output-schema` is load-bearing but untested** — must *constrain* the final message to parseable JSON, and `-o` must capture it cleanly. | T1, T2 | `codex exec --output-schema SCHEMA -o OUT "return a verdict…"`; assert `jq . OUT` parses and matches shape. | ✅ **PASS** — `--output-schema` constrained the final message to exact JSON; `-o` captured it; parses + matches shape (~12.8k tokens). **Caveat:** OpenAI strict mode requires `required` to list **every** key in `properties`; optionals must be required + nullable (`["string","null"]`) or you get `400 invalid_json_schema`. |

### Smoke-test findings & plan corrections — run 2026-07-20 (general@alex-nexus)

**Verdict: Tier 1 is GREEN to build; Tier 2 is BLOCKED on the sandbox env (Blocker C).**
Corrections to fold into the tiers before/while coding:

- **`stdin` MUST be `/dev/null`.** `codex exec` prints `Reading additional input from stdin…`
  and hangs forever if stdin isn't at EOF. `review_one_codex` / `_run_worker_codex` must pass
  `stdin=subprocess.DEVNULL` — plain `subprocess.run(cmd, …)` inherits the parent's stdin and
  will hang in a detached conductor pane. *This alone would have hung every conductor codex call.*
- **Drop `-a never`.** `codex exec` has no `-a/--ask-for-approval`; approval already defaults to
  `never` (confirmed in the exec header: `approval: never`). The sandbox `-s` is the sole
  guardrail. Remove `-a never` from all T1/T2 exec commands (the plan's cmd arrays are wrong).
- **Strict-schema rule (Blocker D).** Every property must appear in `required`; optionals →
  nullable type. So the plan's `result.schema.json` needs `handoff` BOTH in `required` AND as
  `{"type":["string","null"]}`; same discipline for `verdict.schema.json`.
- **Default model is `gpt-5.6-sol`**, not the plan's guessed `gpt-5-codex`. Leave to default.
- **`workspace-write` writable roots** = `[workdir, /tmp, $TMPDIR]` (exec header) — worktree
  writes *would* be allowed once the sandbox can initialize.

**P3 (share fleet MCP into codex) — functionally BLOCKED (not a T1 blocker).** Both
`agent-memory` (`:8330/sse`) and `spark` (`:8343/sse`) are **SSE-transport**; codex `mcp add --url`
speaks **streamable HTTP** only. `codex mcp add` registered them but `codex mcp list` shows
`Auth: Unsupported` and a `codex exec` sees **no MCP tools** (`MEM_NONE`). Removed them again to
keep the reviewer clean. Fix for Tier 0's "citizen not silo" goal: expose a streamable-HTTP `/mcp`
endpoint on the memory/spark servers (FastMCP can serve both), or add an SSE→HTTP shim, or a stdio
wrapper. **T1 doesn't need this** (the reviewer is verdict-only, no MCP).

**Blocker C fix — APPLIED 2026-07-20: option 1.**
1. ✅ **DONE.** `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, persisted in
   `/etc/sysctl.d/99-codex-userns.conf`. Simplest + reversible; loosens a box-wide hardening feature.
   Verified: bwrap `--unshare-net` now exits 0; codex `-s workspace-write` writes+gits in a worktree.
   Other options (not needed now):
2. A scoped AppArmor profile granting `userns` to the bwrap path (keeps hardening elsewhere). More work.
3. `apt install bubblewrap` + on PATH — only helps if Ubuntu's packaged AppArmor profile grants it
   userns; unverified, and codex would need to prefer the system bwrap over its bundled copy.
4. `--dangerously-bypass-approvals-and-sandbox` for the conductor codex worker — removes sandboxing
   (plan explicitly decided against this from the conductor; box is trusted, but it's a posture change).

### Design gaps — fold into the tier (not blocking, but the plan is wrong without them)

- **E. Parse-fail ≠ work-fail (T2).** Codex may edit files correctly but return an unparseable
  final message → marking the subtask `error` makes the conductor *retry good work and burn quota*.
  `_run_worker_codex` must reconcile against `git diff`/artifact existence before declaring error.
- **F. `_run_worker_codex` ignores `profile.skill` (T2 ↔ T5).** Skill-driven profiles (e.g.
  `ui-design`→`ui-ux-design`) won't follow the skill unless the codex path inlines the `SKILL.md`
  body (as the claude worker does). Hard dependency on Tier 5's skill handling.
- **G. Tier 3 loses the whole Claude-hook scaffold.** Codex fires none of `hook-sessionstart`
  (registry+context), `hook-memory` (auto-capture), `autocache`, `hook-notification`,
  `hook-stop` (checkpoint), `hook-sendmessage-bus`. Inbound bus (send-keys) works; outbound works
  only if codex runs `agent-send.sh` directly; everything else must be re-implemented as *codex*
  hooks. Tier 3 is materially bigger than first scoped.
- **H. Codex writes to its own memory silo** (`~/.codex/memories_1.sqlite`). AGENTS.md must
  explicitly direct it to the shared `agent-memory` MCP or its learnings never reach the fleet.
- **I. No cost/quality measurement for codex.** Langfuse is Anthropic-shaped; codex bypasses it,
  so we can't A/B *"cheaper/better?"* — a core reason to go multi-vendor. Mission pass/fail
  survives (conductor DB) but tokens/cost don't. Needs a codex usage sink before A/B claims.

### Opportunity — candidate new tier

- **J. `codex mcp-server` (codex-as-a-tool).** A Claude agent calls codex as an MCP tool for a
  second opinion or a delegated self-contained subtask with **zero conductor changes** — likely a
  higher-ROI "Tier 1.5" than the interactive pane agent. Decide whether to add as its own tier.

### Operational

- **K. Retry-loop quota burn.** The conductor's escalate/replan loop will retry a *failing* codex
  path (auth/sandbox/parse) → fast OpenAI-quota burn. Add a per-mission codex-failure circuit-breaker.
- **L. Spawn-env PATH skew.** The fnm-vs-`/usr/local` split (P1) + stripped popup PATH could make a
  spawned pane resolve a *different* codex binary/version than interactive. Pin the resolved path.

---

## Open questions / risks (not blocking tonight)

- **RAM.** 12.9GB box, prone to swap-exhaustion freezes. Codex `exec` workers are
  process-per-task (spawn→run→exit) — lighter than long-lived panes. Watch memory when a
  DAG wave mixes claude SDK workers + codex exec. Cap concurrent codex workers if needed.
- **Observability.** Codex→OpenAI bypasses nexus-proxy/Langfuse. If unified traces matter,
  later options: point codex at an OpenAI-compatible tracing proxy via `-c model_providers…`
  base-URL, or ship `~/.codex/logs_2.sqlite` into the same store. Out of scope tonight.
- **Auth / rate limits.** Codex on a ChatGPT plan has its own quota separate from the
  Anthropic subscription; a burst of codex workers can hit OpenAI limits independently.
- **Cross-vendor prompt drift.** The reviewer/worker prompts were tuned for Claude. Codex may
  need slightly different phrasing for the strict-JSON contract — `--output-schema` mitigates
  this by enforcing shape server-side, but watch the first few real verdicts/results.
- **Trust boundary unchanged.** Codex workers run under the same sandbox discipline; the
  cross-person bus caveat (anyone in `#nexus-agents` can send-keys) applies to codex panes too.

---

## Reference — exact anchors

- Launcher: `tmux/mac/tmux-scripts/open-claude.sh` — `exec claude` `:277`/`:279`;
  `claude_args` `:211`–`:227`; base-context template `:174`; `MY_HOST` `:85`.
- Conductor: `agent-runner/conductor.py` — `run_worker` `:367`; `spawn_worker` `:428`;
  worker `cmd` `:437`; `review_one` `:597`; `verify_mission` `:628`; `review_plan` `:696`;
  `_register_self` `:460`; `PROFILES` `:36`.
- Worker + result contract: `agent-runner/conductor_worker.py` — profile lookup `:31`
  (fallback `one-shot`), result dict + DB write `:55`–`:64`.
- Claude worker brain: `agent-runner/runner.py` — SDK import `:232`; preset `:276`;
  CLAUDE.md append `:258`–`:266`; inbox pattern `:33`.
- Substrate (vendor-neutral): `tmux/mac/tmux-scripts/substrate.sh` — `spawn` `:143`;
  `send-keys`/`send-text` `:223`–`:243`.
- Spawn picker (Tier 4): `tmux/mac/tmux-scripts/launch-claude.sh` — `nx_spawn` `:89`;
  hardcoded `open-claude.sh` call sites `:99,:108,:126,:156,:169,:197`; bound to `ctrl+a N`
  (`tmux/mac/tmux.conf:41`).
- Command/skill surfaces (Tier 5): Claude cmds `commands/*.md` (`distribute.md`, `opsx/*.md`),
  `~/.claude/commands/`, `~/.claude/skills/`. Codex: `~/.codex/prompts/<name>.md` (→ `/name`, TUI
  only), `~/.codex/skills/<name>/SKILL.md` (same frontmatter as Claude skills), `codex plugin
  marketplace`. Bundled example: `~/.codex/plugins/cache/openai-curated-remote/openai-templates/0.1.0/skills/`.
- Profiles/policy: `~/.tmux/conductor.yaml` (symlink → repo `config/`), `profiles:` block,
  `policy.reviewer.count: 3`, `policy.model: claude-opus-4-8`.
- Codex: `codex exec` flags `--output-schema` / `-o` / `-C` / `-s` / `-a` / `--skip-git-repo-check`;
  `codex review --uncommitted|--base`; `codex mcp add <NAME> [--env] -- <cmd>`;
  `codex mcp-server` (codex AS an MCP tool — future: claude delegates to codex).
  Auth: `~/.codex/auth.json` (openai). Trust: `~/.codex/config.toml` `/home/persinac` trusted.
  Gotcha: install/PATH split (see P1).
