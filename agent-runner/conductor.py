#!/usr/bin/env python3
"""Conductor (Slice B) — mission orchestrator: single-subtask end-to-end.

Deterministic spine with four scoped judgment nodes (classify / plan / adjudicate /
synthesize), each a one-shot opus-4.8 call at `max` effort returning structured JSON.
Everything is logged to agents.missions / mission_subtasks / mission_events.

Slice B proves the whole loop on one subtask, running the worker IN-PROCESS (a
profile-scoped SDK query). Slice C swaps in real bus dispatch to separate runner
workers + the DAG + the re-plan loop; Slice D adds the reviewer fleet; E adds
Jira/Confluence; F adds resume-from-DB.

Run:  .venv/bin/python conductor.py "<goal>"
"""
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import yaml

from conductor_db import Db

REPO = os.environ.get("AGENTS_NEXUS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
REPO_ROOT = os.environ.get("CONDUCTOR_REPO_ROOT") or os.path.dirname(REPO)
PROXY = "http://localhost:4000"
HOST = socket.gethostname().split(".")[0]

CFG = yaml.safe_load(open(os.path.join(HOME, ".tmux", "conductor.yaml")))
POLICY = CFG["policy"]
PROFILES = CFG["profiles"]

# Repo root (agents-nexus dir) — where .env lives. Defined here (early) because the CONDUCTOR_*
# override constants below read os.environ at import time, so the .env fill MUST happen first or
# an env-only var (present in .env but not the process env — e.g. a detached herdr-spawned
# conductor with a stripped env) would be silently missed by every constant.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """Fill missing env from the repo .env (DATABASE_URL, bus config, tokens, CONDUCTOR_* knobs).
    Fill-gaps only (setdefault): never override an already-set process-env var. Called at import
    (before the override constants) AND idempotent, so a detached conductor spawned into a herdr
    pane with a stripped env still reaches the missions DB + honors .env-only overrides."""
    try:
        with open(os.path.join(_REPO_ROOT, ".env")) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)
    except OSError:
        pass


_load_dotenv()   # BEFORE the constants below (ordering fix) — env-only vars now reach them

# Config is the source of truth; env vars let a smoke test run cheap (haiku/medium)
# without editing the policy.
MODEL = os.environ.get("CONDUCTOR_MODEL", POLICY["model"])
ORCH_EFFORT = os.environ.get("CONDUCTOR_ORCH_EFFORT", POLICY["orchestrator_effort"])
WORKER_EFFORT = os.environ.get("CONDUCTOR_WORKER_EFFORT", POLICY["worker_effort"])
ESC_EFFORT = POLICY.get("worker_effort_escalated", "xhigh")   # after ESCALATE_AFTER fails
MAX_REPLANS = int(POLICY.get("max_replans", 5))
ESCALATE_AFTER = int(POLICY.get("escalate_after_fails", 2))
# Worker turn budget. Skill-less build subtasks (a config value + N call sites + tests) were
# dead-looping on the old 24-turn floor: hit the cap mid-implementation, re-plan, repeat with
# zero progress. Default 60 for all workers (matches what the skill-attached path already used);
# override per box via policy.worker_max_turns or CONDUCTOR_WORKER_MAX_TURNS.
WORKER_MAX_TURNS = int(os.environ.get("CONDUCTOR_WORKER_MAX_TURNS", POLICY.get("worker_max_turns", 60)))
# Terminal behavior when a building mission exhausts MAX_REPLANS without passing verify.
# `escalate` (default) = today's behavior byte-for-byte: mark escalated + stop, work stranded.
# `partial` = best-effort: open a DRAFT MR for the attempt + file the residual reviewer findings
# as Claude-Queue tickets, finish `partial`. Preserves the work + enumerates the gaps instead of
# dead-ending. Opt in per box via policy.on_exhausted or CONDUCTOR_ON_EXHAUSTED.
ON_EXHAUSTED = os.environ.get("CONDUCTOR_ON_EXHAUSTED", POLICY.get("on_exhausted", "escalate"))
# Transient-flake retry budget for judgment nodes (judge()): the CLI occasionally exits
# non-zero after an is_error result (surfaced as a ClaudeSDKError) or returns an empty turn.
# A one-off flake must not abort a mission — especially synthesize() on the reporting tail of
# an already-verified mission, which historically crashed the run before the MR was opened.
JUDGE_MAX_ATTEMPTS = int(os.environ.get("CONDUCTOR_JUDGE_MAX_ATTEMPTS", POLICY.get("judge_max_attempts", 3)))
JUDGE_RETRY_BACKOFF = float(os.environ.get("CONDUCTOR_JUDGE_RETRY_BACKOFF", POLICY.get("judge_retry_backoff", 2.0)))

PYEXE = sys.executable
WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conductor_worker.py")
SUBSTRATE = os.path.expanduser("~/.tmux/substrate.sh")   # tmux<->herdr seam (NEXUS_SUBSTRATE)
CONDUCTOR_WORK = os.path.join(HOME, ".tmux", "conductor")   # per-mission worktrees/scratch

# ── sdlc pipeline integration (drives an EXTERNAL sdlc plugin, if installed) ──
# The Conductor can drive an external sdlc plugin's staged pipeline autonomously, using
# its deterministic driver `scan.py` as the router. Config in conductor.yaml `sdlc:`.
SDLC = CFG.get("sdlc", {}) or {}
SDLC_ENABLED = bool(SDLC.get("enabled"))
def _find_sdlc_plugin():
    import glob
    ns = SDLC.get("plugin", "sdlc")
    # explicit config override wins (a private overlay can pin the exact dir)
    cfg_dir = SDLC.get("plugin_dir")
    if cfg_dir:
        return os.path.expanduser(os.path.expandvars(cfg_dir))
    # else discover the plugin under ANY installed marketplace (org-agnostic)
    hits = glob.glob(os.path.expanduser(f"~/.claude/plugins/marketplaces/*/plugins/{ns}"))
    if hits:
        return hits[0]
    # generic fallback (may not exist; callers guard on isfile(SDLC_SCAN))
    return os.path.expanduser(f"~/.claude/plugins/marketplaces/{ns}/plugins/{ns}")
SDLC_PLUGIN_DIR = _find_sdlc_plugin()
SDLC_SCAN = os.path.join(SDLC_PLUGIN_DIR, "scripts", "scan.py")
SDLC_WS_ROOT = os.path.expanduser(os.path.expandvars(SDLC.get("workspace_root", "~/code")))
SDLC_MAX_STAGES = int(SDLC.get("max_stages", 12))
SDLC_PLUGIN_NS = SDLC.get("plugin", "sdlc")   # namespace for the leaf skills (sdlc:create-requirements)
SDLC_WS_ENV = SDLC.get("workspace_env", "")   # env var name the external scan.py reads for its workspace root (org-specific; empty = scan.py self-discovers from cwd)
SDLC_CHAIN = ["requirements", "domain-model", "tech-design", "validation", "plan"]


def _skill_md(skill: str):
    """Resolve a skill name → its SKILL.md path. `plugin:leaf` → that plugin's skills/<leaf>/;
    bare `name` → ~/.claude/skills/<name>/. Returns the path, or None if not found.

    The Skill TOOL can't invoke plugin skills headlessly ("Unknown skill"), so the reliable
    mechanism is to point a worker at this file and have it read + follow the procedure."""
    import glob
    if ":" in skill:
        plug, leaf = skill.split(":", 1)
        cands = ([os.path.join(SDLC_PLUGIN_DIR, "skills", leaf, "SKILL.md")]
                 if plug == SDLC_PLUGIN_NS else [])
        cands += glob.glob(os.path.expanduser(
            f"~/.claude/plugins/marketplaces/*/plugins/{plug}/skills/{leaf}/SKILL.md"))
    else:
        cands = [os.path.expanduser(f"~/.claude/skills/{skill}/SKILL.md")]
    return next((p for p in cands if os.path.isfile(p)), None)


# (_load_dotenv is defined + called near the top of the module — before the CONDUCTOR_* override
# constants — so env-only vars reach them. Kept there to fix the import-time ordering.)


REVIEWERS = int(os.environ.get("CONDUCTOR_REVIEWERS", POLICY.get("reviewer", {}).get("count", 5)))
REVIEW_LENSES = ["correctness", "completeness", "requirements-match", "safety/regressions", "edge-cases"]

# ── plan-gate (pre-build adversarial review of the design + plan) ─────────────
# Personal-box hybrid SDLC flow. The general pipeline already does build + adversarial verify +
# Trello; this inserts the ONE missing step — an adversary that critiques the design/plan BEFORE
# any code is written — plus a lightweight tech-design brief. OFF unless conductor.yaml sets
# `plan_gate.enabled` (absent in the work config → the work-laptop path is byte-for-byte unchanged).
PLAN_GATE = CFG.get("plan_gate", {}) or {}
PLAN_GATE_ON = bool(PLAN_GATE.get("enabled"))
PLAN_GATE_DESIGN = bool(PLAN_GATE.get("design", True))
PLAN_GATE_REVIEWERS = int(os.environ.get("CONDUCTOR_PLAN_REVIEWERS", PLAN_GATE.get("reviewers", 3)))
PLAN_GATE_MAX_ROUNDS = int(PLAN_GATE.get("max_rounds", 2))
PLAN_GATE_ON_EXHAUSTED = PLAN_GATE.get("on_exhausted", "proceed")   # proceed | escalate | stop


def _slack_relay(msg: str):
    """Best-effort ping to the agent bus / #nexus (same mechanism sdlc_report uses)."""
    try:
        subprocess.run([os.path.expanduser("~/.tmux/agent-send.sh"), "--relay", msg],
                       timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _write_design_md(mid: str, goal: str, design_brief: dict):
    """Persist the design brief as design.md at the mission root (audit + worker-readable)."""
    try:
        root = os.path.join(CONDUCTOR_WORK, mid[:8])
        os.makedirs(root, exist_ok=True)
        lines = [f"# Design — {_title(goal)}", "",
                 f"**Approach:** {design_brief.get('approach', '')}", "", "## Key decisions"]
        lines += [f"- **{d.get('decision', '')}** — {d.get('why', '')}"
                  for d in design_brief.get("key_decisions", [])]
        lines += ["", "## Risks / unknowns"] + [f"- {r}" for r in design_brief.get("risks", [])]
        lines += ["", "## Test strategy", design_brief.get("test_strategy", "")]
        with open(os.path.join(root, "design.md"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _check_cmds():
    env = os.environ.get("CONDUCTOR_CHECK_CMD")   # smoke override; "" disables the gate
    if env is not None:
        return [env.strip()] if env.strip() else []
    vc = POLICY.get("verify_command", "task lint")
    return vc if isinstance(vc, list) else [vc]


CHECK_CMDS = _check_cmds()

from claude_agent_sdk import (
    query, ClaudeAgentOptions,
    PermissionResultAllow, PermissionResultDeny,
    AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    ClaudeSDKError,
)

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}   # real SDK write tools (MultiEdit isn't one)

# The worker runs in a git worktree PRE-CHECKED-OUT on the mission branch. A worker doing
# reflexive "good git hygiene" (git checkout -b, switch, branch rename) silently FORKS the
# mission: its commit rides an invented branch while the conductor commits/pushes/MRs the mission
# branch it was left on (empty) → two branches, two MRs, the empty one reported (FC-1249). Tell
# every worker it is already on the right branch and must never touch branch state.
_WORKER_BRANCH_RULE = (" You are already on the correct git branch in this worktree — commit "
                       "directly to it; do NOT create, switch, rename, or reset branches "
                       "(no `git checkout -b`, `git switch -c`, `git branch`).")


# ── helpers ──────────────────────────────────────────────────────────────────
def _set_sess(name: str):
    """Route model traffic through the proxy under a mission-scoped session (traced)."""
    if os.popen(f"curl -sf -m 0.4 {PROXY}/health/liveliness >/dev/null 2>&1 && echo up").read().strip() == "up":
        os.environ["ANTHROPIC_BASE_URL"] = f"{PROXY}/sess/{name}"


def _extract_json(text: str) -> dict:
    """First balanced, string-aware {...} object in the model output."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    depth = 0; instr = False; esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False; continue
        if c == "\\":
            esc = True; continue
        if c == '"':
            instr = not instr; continue
        if instr:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in model output")


def _load_mcp(names):
    avail = {}
    for path in (os.path.join(HOME, ".claude.json"), os.path.join(REPO, ".mcp.json")):
        try:
            avail.update(json.load(open(path)).get("mcpServers", {}) or {})
        except Exception:
            pass
    return {n: avail[n] for n in names if n in avail}


_REPO_CACHE = {}


def _repo_dir(repo):
    if not repo or repo == "agents-nexus":
        return REPO
    if repo in _REPO_CACHE:
        return _REPO_CACHE[repo]
    cand = os.path.join(REPO_ROOT, repo)
    if not os.path.isdir(cand):
        # nested repo (e.g. search/concierge/example-service) — find a git dir by basename
        try:
            out = subprocess.run(
                ["find", REPO_ROOT, "-maxdepth", "5", "-type", "d", "-name", os.path.basename(repo),
                 "-not", "-path", "*/.git/*", "-not", "-path", "*/.worktrees/*"],
                capture_output=True, text=True, timeout=15).stdout.splitlines()
            cand = next((d for d in sorted(out, key=len) if _is_git(d)), REPO)
        except Exception:
            cand = REPO
    _REPO_CACHE[repo] = cand
    return cand


def _is_git(path):
    return os.path.isdir(os.path.join(path, ".git"))


def workspace(mid, repo):
    """Deterministic worker cwd: a git worktree of `repo` on the mission branch, or a
    per-mission scratch dir for repo-less work — so a relative-path write never lands
    in a real checkout."""
    root = os.path.join(CONDUCTOR_WORK, mid[:8])
    if repo and _is_git(_repo_dir(repo)):
        return os.path.join(root, "wt", repo.replace("/", "_"))
    return os.path.join(root, "scratch")


def _slug(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")[:48]


def _title(goal):
    """A human title for the mission: the parenthetical after a ticket key if present,
    else the first line of the goal."""
    m = re.search(r"\(([^)]+)\)", goal or "")
    t = m.group(1) if m else (goal or "mission").splitlines()[0]
    return t.strip()[:80]


def _base_branch(goal):
    """Extract an explicit `base=<ref>` (or `base:<ref>`) directive from a goal — the branch-point
    a mission should stack on instead of the default branch. Deterministic and opt-in: absent the
    token this returns None and missions branch off origin/main exactly as before. The leading
    word-boundary guard means a token like `database=...` never matches. See ensure_workspace()."""
    m = re.search(r"\bbase[=:]([A-Za-z0-9._/-]+)", goal or "")
    return m.group(1) if m else None


def _branch(goal, mid):
    """A meaningful, CI-safe branch name (no '/'): <ticket>-<slug>, else conductor-<slug>.
    When a ticket key is present it already identifies the branch, so the slug is derived from
    the goal with the ticket key + its trailing prose stripped (avoids ugly names like
    `fc-1395-branch-off-origin-main-open-a-standalone-non-dra` slugged from goal instructions),
    kept short (≤24). Falls back to the ticket alone if nothing meaningful remains.

    With NO ticket key, the slug is derived from the goal the SAME way — instruction
    parentheticals stripped, capped ≤24 — so a prose goal ("branch off origin/main; the MR
    must target main") yields `conductor-branch-off-origin-main` rather than the full sentence.
    Falls back to the mission id when nothing meaningful remains."""
    src = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", goal or "")
    if src:
        # goal text minus the ticket key + a following parenthetical of instructions
        rest = re.sub(r"\(([^)]*)\)", " ", (goal or "").replace(src.group(1), " "))
        slug = _slug(rest)[:24].strip("-")
        return f"{src.group(1).lower()}-{slug}" if slug else src.group(1).lower()
    # No ticket: same treatment — drop parentheticals, cap the slug ≤24 (was uncapped ≤48).
    rest = re.sub(r"\(([^)]*)\)", " ", goal or "")
    slug = _slug(rest)[:24].strip("-")
    return f"conductor-{slug or mid[:6]}"


def _mission_ws(goal, mid):
    """herdr workspace-bucket label for a mission: mission/<branch-slug> (shares the slug with
    the git branch). Organizational — groups the mission's tiled workers in one bucket you can
    watch at once and tear down with `substrate.sh workspace-close mission/<slug>`."""
    return f"mission/{_branch(goal, mid)}"


def _resolve_base(rp, base):
    """Resolve a requested base ref in `rp`, preferring the remote-tracking form so a mission
    stacks on the pushed branch rather than a possibly-stale local one. Returns the first of
    (origin/<base>, <base>) that git can verify, else None."""
    for ref in (f"origin/{base}", base):
        if subprocess.run(["git", "-C", rp, "rev-parse", "--verify", ref],
                          capture_output=True).returncode == 0:
            return ref
    return None


def ensure_workspace(mid, repo, branch=None, base=None):
    """Create the worktree/scratch (Conductor-side; the worker just uses the path).
    Returns (cwd, branch|None). The branch is the mission's building output → the MR.

    `base` optionally pins the branch-point to a specific ref (e.g. an unmerged feature branch a
    mission must stack on, surfaced from the goal via `_base_branch()`). It is FAIL-CLOSED: if the
    ref can't be resolved we raise instead of silently branching off the default branch — a silent
    main fallback is exactly the misroute this override exists to prevent."""
    ws = workspace(mid, repo)
    if repo and _is_git(_repo_dir(repo)):
        rp = _repo_dir(repo)
        branch = branch or f"conductor-{mid[:8]}"
        if not os.path.isdir(ws):
            os.makedirs(os.path.dirname(ws), exist_ok=True)
            if base:
                start = _resolve_base(rp, base)
                if not start:
                    raise RuntimeError(
                        f"ensure_workspace: base '{base}' not found in {rp} "
                        f"(tried origin/{base}, {base}) — push the branch or fix the goal's base= directive")
            else:
                # branch off the default branch, not whatever's checked out in the main tree
                start = next((r for r in ("origin/main", "origin/master", "main", "master")
                              if subprocess.run(["git", "-C", rp, "rev-parse", "--verify", r],
                                                 capture_output=True).returncode == 0), "")
            add = ["git", "-C", rp, "worktree", "add", "-b", branch, ws] + ([start] if start else [])
            r = subprocess.run(add, capture_output=True, text=True)
            if r.returncode != 0:   # branch already exists (retry round) → attach it
                subprocess.run(["git", "-C", rp, "worktree", "add", ws, branch],
                               capture_output=True, text=True)
        return ws, branch
    os.makedirs(ws, exist_ok=True)
    return ws, None


async def judge(instruction: str, schema_hint: str) -> dict:
    """A judgment node: one-shot opus/max call, structured JSON out.

    Retries on transient SDK flakes — the CLI can exit non-zero after an is_error
    result (surfaced as "Claude Code returned an error result: …", a ClaudeSDKError)
    or return an empty turn with no parseable JSON. A single such flake on a judgment
    node (classify/plan/design/synthesize) must not abort the whole mission, so we
    retry a few times with a short backoff before letting the error propagate."""
    prompt = (f"{instruction}\n\nRespond with ONLY a single JSON object (no prose, no code fence) "
              f"matching this shape:\n{schema_hint}")
    opts = ClaudeAgentOptions(
        model=MODEL, effort=ORCH_EFFORT, setting_sources=[],
        permission_mode="bypassPermissions", allowed_tools=[],   # no tools → no turn cap needed
    )
    last_err = None
    for attempt in range(JUDGE_MAX_ATTEMPTS):
        try:
            text = []
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            text.append(b.text)
            return _extract_json("".join(text))
        except (ClaudeSDKError, ValueError) as e:
            last_err = e
            if attempt + 1 < JUDGE_MAX_ATTEMPTS:
                await asyncio.sleep(JUDGE_RETRY_BACKOFF * (attempt + 1))
    raise last_err


# ── judgment nodes ─────────────────────────────────────────────────────────
async def classify(goal: str) -> dict:
    return await judge(
        f"You are the Conductor's classifier. Classify this engineering task:\n\n{goal}",
        '{"type":"building|investigation|analysis","goal":"<one line>",'
        '"repos":["<repo>"],"route":"one-shot|conductor","datasources":["snowflake?","datadog?"]}')


async def plan(cr: dict) -> dict:
    return await judge(
        f"You are the Conductor's planner. Decompose this mission into the SMALLEST correct set "
        f"of subtasks (prefer 1 for simple tasks). For each, pick a profile from: {list(PROFILES)}.\n\n"
        f"Mission: {json.dumps(cr)}",
        '{"strategy":"<1-2 sentences>","subtasks":[{"id":"s1","goal":"<what to do>",'
        '"repo":"<repo|null>","profile":"<profile name>","depends_on":[]}],'
        '"verify":{"mode":"code|report","checks":["tests","exercise","swarm-review"]}}')


async def adjudicate(goal: str, worker_result: dict, probes: list, checks: list) -> dict:
    return await judge(
        "You are the Conductor's verifier. Decide if the subtask is genuinely done and correct. "
        "Weigh the deterministic probe results heavily (they are ground truth). Be strict — a "
        "plausible-but-unverified result is NOT a pass.\n\n"
        f"Goal: {goal}\nWorker result: {json.dumps(worker_result)}\n"
        f"Declared checks: {checks}\nProbe results (ground truth): {json.dumps(probes)}",
        '{"pass":true,"findings":[{"severity":"blocker|major|minor","where":"","what":"","fix_hint":""}],'
        '"recommendation":"ship|retry|escalate"}')


async def synthesize(goal: str, worker_result: dict, verdict: dict) -> dict:
    return await judge(
        f"You are the Conductor. Write a short result artifact for this completed, verified mission.\n\n"
        f"Goal: {goal}\nResult: {json.dumps(worker_result)}\nVerdict: {json.dumps(verdict)}",
        '{"artifact":"<a concise markdown summary of what was done and how it was verified>"}')


async def design(cr: dict) -> dict:
    """Pre-plan tech-design brief (hybrid plan-gate). Guides plan() + the adversary; not a spec."""
    return await judge(
        "You are the Conductor's tech-design node. BEFORE the mission is decomposed or any code is "
        "written, produce a COMPACT technical design brief: the approach, the key decisions (each "
        "with a one-line rationale), the main risks/unknowns, and how correctness will be validated. "
        f"Keep it tight — this guides the plan and the build, it is not a full spec.\n\nMission: {json.dumps(cr)}",
        '{"approach":"<2-4 sentences>","key_decisions":[{"decision":"","why":""}],'
        '"risks":["<risk or unknown>"],"test_strategy":"<how correctness is checked>"}')


# ── worker (in-process for slice B) ──────────────────────────────────────────
def _worktree_changed_paths(cwd: str) -> list:
    """Absolute paths of files changed in the worker's worktree (git). Empty if `cwd` isn't a git
    tree or nothing changed. Used to reconcile a codex parse-failure against real work (gap E):
    codex may edit files correctly yet return a final message we can't parse."""
    try:
        out = subprocess.run(["git", "-C", cwd, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    paths = []
    for ln in out.splitlines():
        f = ln[3:].strip().strip('"')
        if f:
            paths.append(f if os.path.isabs(f) else os.path.join(cwd, f))
    return sorted(set(paths))


async def _run_worker_codex(subtask: dict, profile: dict, effort: str) -> dict:
    """run_worker's Codex path: build the subtask with `codex exec` instead of the Claude SDK, and
    return the SAME {subtask_id,status,summary,artifacts,handoff} dict conductor_worker.py writes to
    the DB — so dispatch/verify stay vendor-agnostic. Sandbox by profile permission (read-only →
    read-only, else workspace-write); verify_mission's ground-truth `task lint` gate still guards the
    output. `effort` is advisory (codex chooses its own reasoning)."""
    read_only = profile.get("permission") == "read-only"
    sandbox = "read-only" if read_only else "workspace-write"
    cwd = workspace(subtask["mission_id"], subtask.get("repo"))   # worktree/scratch — never a live checkout

    # Gap F: codex can't invoke the Skill TOOL headlessly → inline the SKILL.md body so skill-driven
    # profiles (ui-design→ui-ux-design, …) actually follow the procedure, mirroring the claude path.
    instr = subtask["goal"]
    skill_md = _skill_md(profile["skill"]) if profile.get("skill") else None
    if skill_md:
        try:
            body = open(skill_md).read()
        except OSError:
            body = ""
        instr = (f"{instr}\n\n--- Assigned procedure — follow it to completion "
                 f"(its references/ are alongside {skill_md}) ---\n{body}")
    instr += ("\n\nYou are a Conductor worker: do exactly this subtask, then stop." + _WORKER_BRANCH_RULE
              + " Your FINAL message "
              "MUST be JSON matching the schema — status=done|error|blocked, summary=what you did, "
              "artifacts=[absolute paths you created/edited], handoff=one line for dependent subtasks "
              "(or null).")

    fd, out = tempfile.mkstemp(prefix="cx-result-", suffix=".json")
    os.close(fd)
    cmd = [CODEX_BIN, "exec", "-C", cwd, "-s", sandbox, "--skip-git-repo-check",
           "--output-schema", RESULT_SCHEMA, "-o", out, instr]
    r = None
    try:
        # stdin=DEVNULL is REQUIRED (codex exec hangs on stdin otherwise); no -a flag (exec approval
        # defaults to "never"); the -s sandbox is the guardrail.
        r = await asyncio.to_thread(subprocess.run, cmd, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=CODEX_WORKER_TIMEOUT)
        wr = json.load(open(out))
        status = wr.get("status") if wr.get("status") in ("done", "error", "blocked") else "error"
        summary = str(wr.get("summary") or "")[-1500:]
        artifacts = [a for a in (wr.get("artifacts") or []) if a]
        handoff = wr.get("handoff")
    except Exception as e:
        # Gap E: a parse/timeout failure is NOT necessarily a WORK failure. If codex actually changed
        # files in the worktree, reconcile to done from git — don't discard good work and let the
        # escalate loop retry it (OpenAI-quota burn). Only truly-empty runs stay error.
        tail = f"; stderr={r.stderr[-300:].strip()}" if (r is not None and r.stderr) else ""
        changed = _worktree_changed_paths(cwd)
        if changed:
            status = "done"
            summary = (f"codex worker: final-message parse failed ({e}){tail}; reconciled from git — "
                       f"{len(changed)} file(s) changed, treated as done.")
            artifacts, handoff = changed, None
        else:
            status, summary, artifacts, handoff = "error", f"codex worker failed: {e}{tail}", [], None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass

    artifacts = sorted({a if os.path.isabs(a) else os.path.join(cwd, a) for a in artifacts})
    return {"subtask_id": subtask["id"], "status": status,
            "summary": summary, "artifacts": artifacts, "handoff": handoff}


async def run_worker(subtask: dict, profile: dict, effort: str) -> dict:
    if profile.get("vendor") == "codex":   # T2: mixed-vendor DAG worker — codex builds this subtask
        return await _run_worker_codex(subtask, profile, effort)
    tools = list(profile.get("tools", []))
    mcp = _load_mcp(profile.get("mcp", []))
    allowed = tools + [f"mcp__{s}__*" for s in mcp]
    read_only = profile.get("permission") == "read-only"
    cwd = workspace(subtask["mission_id"], subtask.get("repo"))   # worktree or scratch — never a live checkout

    # A profile MAY name a `skill:` = the mission procedure (techdebt → pull-techdebt, etc.).
    # The Skill TOOL can't invoke plugin skills headlessly ("Unknown skill"), so we resolve the
    # skill's SKILL.md and have the worker read + follow it directly (proven reliable). Bare name
    # = a user skill (~/.claude/skills); `plugin:skill` = a plugin skill. Skill-less profiles keep
    # the base prompt (setting_sources=[]); both paths share the WORKER_MAX_TURNS budget.
    skill = profile.get("skill")
    skill_md = _skill_md(skill) if skill else None
    append = "You are a Conductor worker. Do exactly the assigned subtask, then stop." + _WORKER_BRANCH_RULE
    if skill_md:
        if "Read" not in allowed:
            allowed = allowed + ["Read"]
        append = (f"You are a Conductor worker. Your assigned procedure is the skill at {skill_md} — "
                  f"read it and follow it to completion (its references/ are alongside it), then stop."
                  + _WORKER_BRANCH_RULE)

    # Workers are autonomous within an approved mission → bypassPermissions.
    # read-only profiles disallow the write tools (Bash-write hardening is slice C+,
    # which brings back the classifier gate via a streaming worker).
    opts = ClaudeAgentOptions(
        model=MODEL, effort=effort, cwd=cwd,
        setting_sources=(["user", "project"] if skill_md else []),
        mcp_servers=mcp, allowed_tools=allowed,
        disallowed_tools=(list(WRITE_TOOLS) if read_only else []),
        permission_mode="bypassPermissions", max_turns=WORKER_MAX_TURNS,
        system_prompt={"type": "preset", "preset": "claude_code", "append": append},
    )
    artifacts, text, status = [], [], "error"
    async for msg in query(prompt=subtask["goal"], options=opts):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    text.append(b.text.strip())
                elif isinstance(b, ToolUseBlock):
                    if b.name in WRITE_TOOLS:
                        fp = b.input.get("file_path") or b.input.get("notebook_path")
                        if fp:
                            artifacts.append(fp if os.path.isabs(fp) else os.path.join(cwd, fp))
                    elif b.name == "Bash":   # catch files written via redirect / tee / touch
                        for m in re.finditer(r'(?:>>?\s*|(?:^|\s)(?:tee|touch)\s+)([^\s;|&>]+)',
                                             b.input.get("command") or ""):
                            fp = m.group(1)
                            artifacts.append(fp if os.path.isabs(fp) else os.path.join(cwd, fp))
        elif isinstance(msg, ResultMessage):
            status = "done" if msg.subtype == "success" else "error"
    joined = " ".join(text)
    return {"subtask_id": subtask["id"], "status": status,
            "summary": joined[-1500:], "artifacts": sorted(set(artifacts)), "handoff": joined[-400:]}


# ── dispatch (slice C: cross-process workers, one tmux window per subtask) ────
def _tmux_session():
    s = os.popen("tmux display-message -p '#{session_name}' 2>/dev/null").read().strip()
    return s or os.environ.get("TMUX_AGENT_SESSION", "agents")


def spawn_worker(mid: str, st: dict, ws_label: str = None) -> str:
    """Spawn a headless worker for one subtask in its own window/pane (fleet-visible, repo
    cwd). When ws_label is set (herdr), the worker tiles into that mission bucket so the whole
    fan-out is watchable at once. Falls back to a detached subprocess if the backend is down."""
    cwd = workspace(mid, st.get("repo"))
    name = f"cw-{st['subtask_key']}-{mid[:4]}"
    env_prefix = "".join(f"{k}={os.environ[k]} " for k in
                         ("CONDUCTOR_MODEL", "CONDUCTOR_ORCH_EFFORT", "CONDUCTOR_WORKER_EFFORT")
                         if os.environ.get(k))
    cmd = f"{env_prefix}{PYEXE} {WORKER_SCRIPT} {mid} {st['id']}"
    # Route spawn through the substrate seam (tmux today; herdr under NEXUS_SUBSTRATE=herdr).
    # The shim keeps the trailing-colon next-FREE-index behavior (the 523dfff trap fix); we
    # pass the resolved session so it targets the same session the conductor runs in. With a
    # mission bucket, --split tiles the workers together (herdr); tmux ignores it (stays flat).
    env = {**os.environ, "TMUX_AGENT_SESSION": _tmux_session()}
    args = [SUBSTRATE, "spawn", name, cwd, cmd]
    if ws_label:
        args += ["--workspace", ws_label, "--split", "down"]
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    if r.returncode == 0:
        return os.environ.get("NEXUS_SUBSTRATE", "herdr")  # default herdr; NEXUS_SUBSTRATE=tmux for the legacy fallback
    subprocess.Popen(cmd, shell=True, cwd=cwd, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "subprocess"


def _pane_self() -> str:
    """This process's own fleet pane handle, or "" if it has none (foreground conductor
    sharing the caller's pane). Folds herdr's HERDR_PANE_ID into TMUX_PANE like the hooks do."""
    return os.environ.get("TMUX_PANE") or os.environ.get("HERDR_PANE_ID") or ""


def _register_self(name: str, cwd: str = None, ws: str = None, orchestrator: bool = False) -> None:
    """Write THIS detached agent's ~/.tmux/registry entry so the registry-driven consumers
    (reaper, `peers`, name→handle bus resolution) can see it — the register-always hardening
    (docs/herdr-workflow.md #8). Seam-spawned agents (conductor orchestrator + workers) bypass
    open-claude.sh, so without this they were a second roster invisible to the registry. Pairs
    with _deregister_self() in a finally (headless python has no tmux pane-died hook). Optionally
    also tags @orchestrator. Best-effort; a foreground conductor (no own pane) is a no-op."""
    pane = _pane_self()
    if not pane:
        return
    reg = [SUBSTRATE, "register", pane, name, cwd or os.getcwd()]
    if ws:
        reg.append(ws)
    try:
        subprocess.run(reg, check=False, capture_output=True, timeout=5)
        if orchestrator:
            subprocess.run([SUBSTRATE, "tag-orchestrator", pane],
                           check=False, capture_output=True, timeout=5)
    except Exception:
        pass


def _deregister_self() -> None:
    """Remove this agent's registry entry on exit. Headless python panes have no tmux
    pane-died hook (that's what deregisters claude agents), so they must self-clean or leave a
    stale entry — the failure mode register-always must not introduce."""
    pane = _pane_self()
    if not pane:
        return
    try:
        subprocess.run([SUBSTRATE, "deregister", pane], check=False, capture_output=True, timeout=5)
    except Exception:
        pass


async def wait_terminal(db, sids, timeout=1200):
    """Poll subtask rows until all reach a terminal state (done|error|blocked)."""
    pending = set(sids)
    end = time.time() + timeout
    while pending and time.time() < end:
        await asyncio.sleep(2)
        for sid in list(pending):
            s = db.get_subtask(sid)
            if s and s["status"] in ("done", "error", "blocked"):
                pending.discard(sid)
    for sid in pending:   # timed out
        db.update_subtask(sid, status="error",
                          result={"status": "error", "summary": "worker timeout", "artifacts": []})


async def execute_dag(db, mid: str):
    """Run pending subtasks wave by wave, respecting depends_on; inject upstream
    handoffs into a dependent's goal before spawning it."""
    goal = (db.get_mission(mid) or {}).get("goal", "")
    while True:
        subs = db.list_subtasks(mid)
        by_key = {s["subtask_key"]: s for s in subs}
        done = {k for k, s in by_key.items() if s["status"] == "done"}
        ready = [s for s in subs if s["status"] == "pending"
                 and all(d in done for d in (s["depends_on"] or []))]
        if not ready:
            break
        for s in ready:
            deps = [by_key[d] for d in (s["depends_on"] or []) if d in by_key]
            if deps:
                ctx = "\n".join(
                    f"- {d['subtask_key']}: "
                    f"{((d.get('result') or {}).get('handoff') or (d.get('result') or {}).get('summary', ''))[:300]}"
                    for d in deps)
                base = s["goal"].split("\n\n[Upstream context]")[0]
                db.update_subtask(s["id"], goal=f"{base}\n\n[Upstream context]\n{ctx}")
            ws, branch = ensure_workspace(mid, s.get("repo"), _branch(goal, mid), base=_base_branch(goal))
            db.log_event(mid, "workspace", {"repo": s.get("repo"), "cwd": ws, "branch": branch}, subtask_id=s["id"])
            via = spawn_worker(mid, db.get_subtask(s["id"]),
                               ws_label=os.environ.get("CONDUCTOR_MISSION_WS") or _mission_ws(goal, mid))
            db.log_event(mid, "dispatched",
                         {"subtask": s["subtask_key"], "profile": s["profile"], "via": via}, subtask_id=s["id"])
        print(f"[conductor] dispatched wave: {[s['subtask_key'] for s in ready]}")
        await wait_terminal(db, [s["id"] for s in ready])
    for s in db.list_subtasks(mid):   # anything still pending has unsatisfiable deps
        if s["status"] == "pending":
            db.update_subtask(s["id"], status="blocked")


# ── verify (mission-level: deterministic probes + adversarial reviewer fleet) ─
def _run_check(cwd):
    """Run the repo's real check command in a worktree — the ground-truth gate. Tries
    the CHECK_CMDS candidates in order and runs the FIRST that actually exists in this
    repo. Returns a probe dict, or None when none exist (so a repo without a lint task
    doesn't false-fail)."""
    for cmd in CHECK_CMDS:
        if not cmd.strip():
            continue
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            return {"probe": "check", "command": cmd, "ok": False, "exit": -1, "output_tail": "timed out (900s)"}
        out = (r.stdout or "") + (r.stderr or "")
        low = out.lower()
        if r.returncode == 127 or "no such task" in low or "does not exist" in low or "command not found" in low:
            continue   # this candidate isn't defined in this repo → try the next
        return {"probe": "check", "command": cmd, "ok": r.returncode == 0, "exit": r.returncode,
                "output_tail": out[-4000:]}
    return None


_BASE_CHECK = {}   # cache: same lint on origin/main per mission (pre-existing debt)


def _run_check_base(repo, mid):
    """Run the same check on the repo's default branch (worktree, cached) so we can tell
    pre-existing lint debt apart from failures the mission actually introduced."""
    key = (mid[:8], repo)
    if key in _BASE_CHECK:
        return _BASE_CHECK[key]
    rp = _repo_dir(repo)
    base_ws = os.path.join(CONDUCTOR_WORK, mid[:8], "base", repo.replace("/", "_"))
    if not os.path.isdir(base_ws):
        os.makedirs(os.path.dirname(base_ws), exist_ok=True)
        ref = next((r for r in ("origin/main", "origin/master", "main", "master")
                    if subprocess.run(["git", "-C", rp, "rev-parse", "--verify", r], capture_output=True).returncode == 0), "HEAD")
        subprocess.run(["git", "-C", rp, "worktree", "add", "--detach", base_ws, ref], capture_output=True, text=True)
    _BASE_CHECK[key] = _run_check(base_ws)
    return _BASE_CHECK[key]


def _changed_files(ws):
    """The mission's changed files in a worktree (paths + basenames), for attributing lint failures."""
    out = subprocess.run(["git", "-C", ws, "status", "--porcelain"], capture_output=True, text=True).stdout
    files = set()
    for ln in out.splitlines():
        f = ln[3:].strip().strip('"')
        if f:
            files.add(f); files.add(os.path.basename(f))
    return files


# ── cross-vendor reviewer (Codex) ─────────────────────────────────────────────
# One of the N adversarial reviewers can run on OpenAI Codex instead of the Claude SDK —
# a genuine second-model opinion in the verify stage. Read-only, verdict-only, same
# {lens, verdict} contract as review_one, and fails CLOSED (pass=False) on any error so a
# hung/broken codex can never silently pass a bad mission. Set `policy.reviewer.codex` (or
# CONDUCTOR_REVIEWER_CODEX) to how many of the `count` reviewers run on codex; 0 = off.
REVIEW_CODEX = int(os.environ.get("CONDUCTOR_REVIEWER_CODEX",
                                  POLICY.get("reviewer", {}).get("codex", 0)))
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "/usr/local/bin/codex"
CODEX_TIMEOUT = int(os.environ.get("CONDUCTOR_CODEX_TIMEOUT", "600"))
CODEX_WORKER_TIMEOUT = int(os.environ.get("CONDUCTOR_CODEX_WORKER_TIMEOUT", "1800"))
VERDICT_SCHEMA = os.path.join(REPO, "agent-runner", "schemas", "verdict.schema.json")
RESULT_SCHEMA = os.path.join(REPO, "agent-runner", "schemas", "result.schema.json")


def _reviewer_prompt(goal: str, summaries: list, probes: list, lens: str, cwd: str,
                     can_inspect: bool = True) -> str:
    """Shared adversarial-reviewer prompt used by BOTH the Claude and Codex reviewers.
    `can_inspect=False` (codex) tells the model to judge from the inline summaries/probes only:
    codex's read-only sandbox can't run shell on this box (unprivileged-userns/bubblewrap is
    AppArmor-restricted), and the probes already carry each artifact's head + the check output."""
    inspect = (f"You MAY inspect artifacts under {cwd} (Read/Grep/Bash, read-only)."
               if can_inspect else
               "Judge ONLY from the summaries and ground-truth probes below; do not run shell commands.")
    return (
        f"You are an ADVERSARIAL reviewer using the '{lens}' lens. Find why this mission is NOT "
        f"correctly/completely done. Be strict: if anything is unverified, missing, or wrong, FAIL it. "
        f"{inspect}\n\n"
        f"Goal: {goal}\nWork summaries: {json.dumps(summaries)}\n"
        f"Ground-truth probes: {json.dumps(probes)}\n\n"
        'Respond with ONLY JSON: {"pass":true,"findings":[{"severity":"blocker|major|minor","where":"","what":""}]}'
    )


async def review_one(mid: str, goal: str, summaries: list, probes: list, lens: str, cwd: str) -> dict:
    """One adversarial reviewer (read-only) with a distinct lens. Returns {lens, verdict}."""
    profile = PROFILES.get("reviewer", {})
    mcp = _load_mcp(profile.get("mcp", []))
    tools = list(profile.get("tools", [])) + [f"mcp__{s}__*" for s in mcp]
    prompt = _reviewer_prompt(goal, summaries, probes, lens, cwd)
    opts = ClaudeAgentOptions(
        model=MODEL, effort=ORCH_EFFORT, cwd=cwd, setting_sources=[],
        mcp_servers=mcp, allowed_tools=tools, disallowed_tools=list(WRITE_TOOLS),
        permission_mode="bypassPermissions",
    )
    text = []
    try:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text.append(b.text)
        return {"lens": lens, "verdict": _extract_json("".join(text))}
    except Exception as e:
        return {"lens": lens, "verdict": {"pass": False,
                "findings": [{"severity": "major", "where": lens, "what": f"reviewer failed: {e}"}]}}


async def review_one_codex(mid: str, goal: str, summaries: list, probes: list, lens: str, cwd: str) -> dict:
    """review_one's twin, running on OpenAI Codex (`codex exec`) instead of the Claude SDK.
    Read-only sandbox; the final message is constrained to the verdict schema via --output-schema
    and captured with -o. Same {lens, verdict} contract; fails CLOSED on any error (parse/timeout/
    nonzero exit) so codex can never rubber-stamp a bad mission."""
    prompt = _reviewer_prompt(goal, summaries, probes, lens, cwd, can_inspect=False)
    fd, out = tempfile.mkstemp(prefix="cx-verdict-", suffix=".json")
    os.close(fd)
    cmd = [CODEX_BIN, "exec", "-C", cwd, "-s", "read-only", "--skip-git-repo-check",
           "--output-schema", VERDICT_SCHEMA, "-o", out, prompt]
    r = None
    try:
        # stdin=DEVNULL is REQUIRED — `codex exec` blocks reading stdin otherwise (would hang the
        # whole gather). No -a flag: exec is non-interactive (approval defaults to "never"); the
        # -s read-only sandbox is the guardrail. Model/effort left to codex defaults.
        r = await asyncio.to_thread(subprocess.run, cmd, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=CODEX_TIMEOUT)
        verdict = json.load(open(out))
        if not isinstance(verdict.get("pass"), bool) or not isinstance(verdict.get("findings"), list):
            raise ValueError(f"bad verdict shape: {verdict!r}")
        return {"lens": f"{lens}(codex)", "verdict": verdict}
    except Exception as e:
        tail = f"; stderr={r.stderr[-200:].strip()}" if (r is not None and r.stderr) else ""
        return {"lens": f"{lens}(codex)", "verdict": {"pass": False,
                "findings": [{"severity": "major", "where": lens,
                              "what": f"codex reviewer failed: {e}{tail}"}]}}
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


async def verify_mission(mid: str, goal: str, subtasks: list) -> tuple:
    """Deterministic probes + a fleet of adversarial reviewers (majority-vote, no blockers)."""
    probes = []
    for s in subtasks:
        for a in (s.get("result") or {}).get("artifacts", []):
            exists = bool(a) and os.path.exists(a)
            p = {"probe": "artifact", "subtask": s["subtask_key"], "path": a, "exists": exists}
            if exists:
                try:
                    with open(a, errors="replace") as f:
                        p["content_head"] = f.read(500)
                except OSError:
                    pass
            probes.append(p)
    summaries = [{"subtask": s["subtask_key"], "status": s["status"],
                  "summary": (s.get("result") or {}).get("summary", "")} for s in subtasks]

    # Ground-truth gate: run the repo's real check command (`task lint`) in each
    # building worktree. A failing check is a HARD fail regardless of the reviewers.
    check_ok, ran_check = True, False
    for repo in sorted({s.get("repo") for s in subtasks if s.get("repo")}):
        if not (repo and _is_git(_repo_dir(repo))):
            continue
        ws = workspace(mid, repo)
        if not os.path.isdir(ws):
            continue
        cp = await asyncio.to_thread(_run_check, ws)
        if not cp:
            continue
        ran_check = True
        probes.append(cp)
        if cp["ok"]:
            print(f"[conductor] check `{cp['command']}` in {repo}: PASS")
            continue
        # Branch check failed — baseline-diff: is it the mission's fault or pre-existing debt?
        changed = _changed_files(ws)
        if any(cf and cf in cp["output_tail"] for cf in changed):
            check_ok = False
            print(f"[conductor] check FAIL in {repo} on a mission-changed file → HARD FAIL")
        else:
            base_cp = await asyncio.to_thread(_run_check_base, repo, mid)
            if base_cp and not base_cp["ok"]:
                cp["baseline_dirty"] = True   # pre-existing failures, none in mission files → advisory
                print(f"[conductor] check FAIL in {repo} but baseline also fails (no mission files) → advisory")
            else:
                check_ok = False
                print(f"[conductor] check FAIL in {repo} (baseline clean) → HARD FAIL")

    cwd = os.path.join(CONDUCTOR_WORK, mid[:8])
    if not os.path.isdir(cwd):
        cwd = REPO
    n = max(1, REVIEWERS)
    lenses = [REVIEW_LENSES[i % len(REVIEW_LENSES)] for i in range(n)]
    # Route the last k reviewers to Codex (cross-vendor second opinion); 0 = pure-claude.
    k = min(REVIEW_CODEX, n) if (REVIEW_CODEX > 0 and os.path.exists(CODEX_BIN)) else 0
    tag = f" — last {k} on codex" if k else ""
    print(f"[conductor] verify: {n} reviewers ({', '.join(dict.fromkeys(lenses))}){tag}")
    reviews = await asyncio.gather(*[
        (review_one_codex if i >= n - k else review_one)(mid, goal, summaries, probes, lenses[i], cwd)
        for i in range(n)])
    passed = sum(1 for r in reviews if r["verdict"].get("pass"))
    findings = [dict(f, lens=r["lens"]) for r in reviews for f in (r["verdict"].get("findings") or [])]
    blockers = [f for f in findings if f.get("severity") == "blocker"]
    reviewers_ok = passed >= (n // 2 + 1) and not blockers   # majority pass AND no blocker
    overall = (check_ok if ran_check else True) and reviewers_ok
    verdict = {"pass": overall, "recommendation": "ship" if overall else "retry",
               "check": ({"ran": ran_check, "ok": check_ok} if ran_check else None),
               "reviewers": {"count": n, "passed": passed}, "findings": findings[:25]}
    return verdict, probes


# ── plan-gate (pre-build adversarial review) ─────────────────────────────────
async def review_plan(mid: str, goal: str, design_brief: dict, plan_obj: dict, lens: str) -> dict:
    """One adversarial reviewer of the design + plan, PRE-build (no code exists yet). Read-only.
    Mirrors review_one; on this box the `reviewer` profile is absent → a tool-less reasoning
    reviewer (same graceful degradation as the verify stage)."""
    profile = PROFILES.get("reviewer", {})
    mcp = _load_mcp(profile.get("mcp", []))
    tools = list(profile.get("tools", [])) + [f"mcp__{s}__*" for s in mcp]
    prompt = (
        f"You are an ADVERSARIAL PLAN reviewer using the '{lens}' lens. NO code has been written "
        f"yet — critique the DESIGN and PLAN below and find why, if built as written, the result "
        f"would be wrong, incomplete, untestable, or unsafe. Be strict: a plausible but "
        f"underspecified plan FAILS. You MAY read existing repo code (read-only) to judge feasibility.\n\n"
        f"Goal: {goal}\nDesign: {json.dumps(design_brief)}\nPlan: {json.dumps(plan_obj)}\n\n"
        'Respond with ONLY JSON: {"pass":true,"findings":[{"severity":"blocker|major|minor","where":"","what":"","fix_hint":""}]}'
    )
    opts = ClaudeAgentOptions(
        model=MODEL, effort=ORCH_EFFORT, cwd=REPO, setting_sources=[],
        mcp_servers=mcp, allowed_tools=tools, disallowed_tools=list(WRITE_TOOLS),
        permission_mode="bypassPermissions",
    )
    text = []
    try:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text.append(b.text)
        return {"lens": lens, "verdict": _extract_json("".join(text))}
    except Exception as e:
        return {"lens": lens, "verdict": {"pass": False,
                "findings": [{"severity": "major", "where": lens, "what": f"plan reviewer failed: {e}"}]}}


async def revise_plan(cr: dict, design_brief: dict, plan_obj: dict, findings: list) -> dict:
    """Regenerate the plan to address adversarial findings (re-decompose if needed)."""
    return await judge(
        "You are the Conductor's planner, revising after an adversarial plan review. Address EVERY "
        "blocker and major finding by fixing the plan (and its design assumptions) — re-decompose if "
        "needed. Keep the SMALLEST correct set of subtasks; each picks a profile from: "
        f"{list(PROFILES)}.\n\nMission: {json.dumps(cr)}\nDesign: {json.dumps(design_brief)}\n"
        f"Current plan: {json.dumps(plan_obj)}\nReview findings: {json.dumps(findings)}",
        '{"strategy":"<1-2 sentences>","subtasks":[{"id":"s1","goal":"<what to do>",'
        '"repo":"<repo|null>","profile":"<profile name>","depends_on":[]}],'
        '"verify":{"mode":"code|report","checks":["tests","exercise","swarm-review"]}}')


async def run_plan_gate(db, mid: str, goal: str, cr: dict, design_brief: dict, plan_obj: dict, card_id):
    """Adversarial pre-build gate: a reviewer fleet critiques the design+plan; revise + re-review
    up to PLAN_GATE_MAX_ROUNDS. Returns (plan_obj, decision) where decision ∈ {approved, forced,
    stop}. `forced` = gate never passed but on_exhausted=proceed (build anyway, logged + pinged)."""
    n = max(1, PLAN_GATE_REVIEWERS)
    findings, blockers = [], []
    for rnd in range(PLAN_GATE_MAX_ROUNDS + 1):
        lenses = [REVIEW_LENSES[i % len(REVIEW_LENSES)] for i in range(n)]
        reviews = await asyncio.gather(*[review_plan(mid, goal, design_brief, plan_obj, lenses[i])
                                         for i in range(n)])
        passed = sum(1 for r in reviews if r["verdict"].get("pass"))
        findings = [dict(f, lens=r["lens"]) for r in reviews for f in (r["verdict"].get("findings") or [])]
        blockers = [f for f in findings if f.get("severity") == "blocker"]
        ok = passed >= (n // 2 + 1) and not blockers
        db.log_event(mid, "plan_reviewed", {"round": rnd, "reviewers": n, "passed": passed,
                                            "pass": ok, "findings": findings[:25]})
        print(f"[conductor] plan-gate round {rnd}: {passed}/{n} pass{' ✓' if ok else ' ✗'}")
        if ok:
            db.log_event(mid, "plan_approved", {"round": rnd, "reviewers": n, "passed": passed})
            return plan_obj, "approved"
        if rnd < PLAN_GATE_MAX_ROUNDS:
            print(f"[conductor] plan-gate: revising plan (round {rnd + 1})…")
            plan_obj = await revise_plan(cr, design_brief, plan_obj, findings)
    # gate exhausted without a pass
    if PLAN_GATE_ON_EXHAUSTED == "proceed":
        db.log_event(mid, "plan_gate_forced", {"reviewers": n, "blockers": len(blockers),
                                               "findings": findings[:25]})
        top = "; ".join(f"[{f.get('severity')}] {(f.get('what') or '')[:120]}" for f in findings[:5])
        trello_comment(db, mid, card_id,
                       f"⚠ plan-gate did not pass in {PLAN_GATE_MAX_ROUNDS} round(s); proceeding to build. "
                       f"Top findings: {top}")
        _slack_relay(f"⚠ Conductor {mid[:8]} plan-gate unresolved after {PLAN_GATE_MAX_ROUNDS} round(s) — "
                     f"building with {len(blockers)} blocker(s) logged · {_title(goal)}")
        return plan_obj, "forced"
    return plan_obj, "stop"


# ── execute + verify + re-plan loop ──────────────────────────────────────────
async def run_and_verify(db, mid: str, goal: str, start_round: int = 0) -> tuple:
    """DAG → verify → re-dispatch on failure up to MAX_REPLANS, escalating worker
    effort (high → xhigh) after ESCALATE_AFTER failed rounds. `start_round` lets a
    resumed mission continue from its persisted replan_count."""
    verdict = {"pass": False}
    for rnd in range(start_round, MAX_REPLANS + 1):
        effort = WORKER_EFFORT if rnd < ESCALATE_AFTER else ESC_EFFORT
        db.log_event(mid, "round", {"round": rnd, "worker_effort": effort})
        print(f"[conductor] ── round {rnd} (worker effort={effort}) ──")
        for s in db.list_subtasks(mid):
            if s["status"] != "done":
                db.update_subtask(s["id"], status="pending", effort=effort, attempt=s["attempt"] + 1)
        db.update_mission(mid, status="dispatched", replan_count=rnd)

        await execute_dag(db, mid)

        subs = db.list_subtasks(mid)
        failed = [s for s in subs if s["status"] != "done"]
        db.update_mission(mid, status="verifying")
        db.log_event(mid, "verify_started", {"round": rnd})
        if failed:
            verdict = {"pass": False, "recommendation": "retry",
                       "findings": [{"severity": "blocker", "where": s["subtask_key"],
                                     "what": f"subtask ended {s['status']}",
                                     "fix_hint": (s.get("result") or {}).get("summary", "")[:200]} for s in failed]}
            probes = []
        else:
            verdict, probes = await verify_mission(mid, goal, subs)
        db.update_mission(mid, verdict=verdict)
        db.log_event(mid, "verdict", {"round": rnd, "verdict": verdict, "probes": probes})
        print(f"[conductor] round {rnd} verdict: pass={verdict.get('pass')} rec={verdict.get('recommendation')}")
        if verdict.get("pass"):
            return verdict, True
        if rnd < MAX_REPLANS:
            db.log_event(mid, "replan", {"round": rnd, "findings": verdict.get("findings")})
            fb = json.dumps(verdict.get("findings", []))[:800]
            failed_keys = {s["subtask_key"] for s in failed}
            for s in db.list_subtasks(mid):
                # re-run failed subtasks; if all were done but verify still failed, re-run all
                if (failed_keys and s["subtask_key"] in failed_keys) or not failed_keys:
                    base = s["goal"].split("\n\n[Verification feedback]")[0]
                    db.update_subtask(s["id"], goal=f"{base}\n\n[Verification feedback]\n{fb}", status="pending")
    return verdict, False


# ── report (slice E: branch / Jira / Confluence / MR — gated, dry-run default) ─
def _expandvars_deep(x):
    """Resolve ${VAR}/$VAR from the environment in every string leaf so reporting
    targets can be env-templated — the shared conductor config carries no per-user
    literals (e.g. jira project/assignee, confluence space come from the box's .env)."""
    if isinstance(x, str):
        return os.path.expandvars(x)
    if isinstance(x, dict):
        return {k: _expandvars_deep(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_expandvars_deep(v) for v in x]
    return x

REPORTING = _expandvars_deep(CFG.get("reporting", {}))


def _jira_epic():
    """The configured parent epic key, or "" when there is none. Guards the two ways
    "no epic" shows up: an omitted/empty key, and an UNRESOLVED ${VAR} template (env-
    expansion leaves `${JIRA_EPIC}` verbatim when the var is unset). Either way → no
    parent, so a tracking Task is created top-level rather than under a bogus key."""
    e = (REPORTING.get("jira", {}) or {}).get("epic") or ""
    e = e.strip()
    return "" if (not e or e.startswith("${") or e.startswith("$")) else e


def _epic_clause(src=None):
    """Reporter-prompt fragment for the parent epic, encoding the precedence:
      1. INHERIT the source ticket's own epic (keep the tracking Task with its work), else
      2. the configured DEFAULT epic (reporting.jira.epic / JIRA_EPIC), else
      3. top-level (no parent).
    The reporter agent has the atlassian MCP, so 'inherit' is an instruction it resolves.
    _jira_epic() collapses None/empty/unresolved-${VAR} → "" so we never emit a bogus key."""
    default = _jira_epic()
    if src:
        s = (f"parent epic: use {src}'s parent epic (look it up via the atlassian MCP — the epic "
             f"it is a child of); ")
        s += (f"if {src} has no parent epic, use {default!r}" if default
              else f"if {src} has no parent epic, create this as a top-level issue (no parent)")
        return s
    return (f"parent epic={default!r}" if default
            else "no parent epic (create it as a top-level issue)")
def _resolve_run_mode():
    """Resolve the mission run mode. ONE explicit knob, no goal-text prefixes: CONDUCTOR_RUN_MODE
    (dry|live) with a SANE DEFAULT of 'dry' (reporting is logged, never sent). The --live/--dry-run
    flags override it at the entrypoint. CONDUCTOR_DRY_RUN=1 is honored as a legacy alias for dry.
    Because it's an env var, it is inherited by the detached --distribute-run mission (which is why
    a 'dry' distributed run used to leak REAL Jira tickets — the old flag never crossed that seam)."""
    m = (os.environ.get("CONDUCTOR_RUN_MODE") or "").strip().lower()
    if m in ("dry", "live"):
        return m
    if os.environ.get("CONDUCTOR_DRY_RUN") == "1":   # legacy alias
        return "dry"
    return "dry"   # safe default: an accidental run never files/sends anything

RUN_MODE = _resolve_run_mode()
DRY_RUN = RUN_MODE != "live"   # log reporting payloads, don't send. report() reads this at call time.


# ── Trello lifecycle (optional) — deterministic REST; no MCP/LLM. Creds from env, else a
# configured Doppler project/config (reporting.trello.doppler_project/_config). A mission's
# card walks on-deck → in-progress → done|failed across the configured lists as it runs.
_TRELLO_CREDS = None
_TRELLO_LISTS: dict = {}
_TRELLO_PHASE_KEY = {"on_deck": "on_deck_list", "in_progress": "in_progress_list",
                     "done": "done_list", "failed": "failed_list"}
# Generic fallback list names; a mission's conductor.yaml sets the real board's list names
# via reporting.trello.{on_deck_list,in_progress_list,done_list,failed_list}.
_TRELLO_PHASE_DEFAULT = {"on_deck": "on-deck", "in_progress": "in-progress",
                         "done": "done", "failed": "failed"}


_SECRET_GET = os.path.join(_REPO_ROOT, "scripts", "secrets", "secret-get.sh")


def _secret(name, project=None, config=None, backends=None):
    """Resolve one secret via the shared chain resolver (scripts/secrets/secret-get.sh).
    Returns '' on any failure — fail-soft, never aborts a mission (mirrors the old doppler
    behavior). `backends` overrides the chain for this call; project/config set Doppler scope."""
    cmd = ["bash", _SECRET_GET]
    if project:
        cmd += ["--project", project]
    if config:
        cmd += ["--config", config]
    cmd.append(name)
    env = dict(os.environ)
    if backends:
        env["NEXUS_SECRETS_BACKENDS"] = backends
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except FileNotFoundError:   # bash / resolver absent → degrade, never abort
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _trello_creds():
    """Resolve {key, token, board}; env first, else a configured Doppler project/config.
    Cached; {} if unavailable."""
    global _TRELLO_CREDS
    if _TRELLO_CREDS is None:
        key = os.environ.get("TRELLO_API_KEY"); tok = os.environ.get("TRELLO_TOKEN")
        board = os.environ.get("TRELLO_BOARD_ID")
        _tcfg = REPORTING.get("trello", {})
        dop_project = _tcfg.get("doppler_project"); dop_config = _tcfg.get("doppler_config")
        if not (key and tok) and dop_project and dop_config:
            # Resolve through the shared chain. Only opt into doppler when a project/config is
            # configured (today's explicit gate); otherwise env-only. secret-get fail-softs when
            # the doppler CLI is absent (a work laptop), so Trello degrades to "no creds".
            _b = "env,doppler"
            key = key or _secret("TRELLO_API_KEY", dop_project, dop_config, _b)
            tok = tok or _secret("TRELLO_TOKEN", dop_project, dop_config, _b)
            board = board or _secret("TRELLO_BOARD_ID", dop_project, dop_config, _b)
        _TRELLO_CREDS = ({"key": key, "token": tok,
                          "board": board or _tcfg.get("board_id")}
                         if (key and tok) else {})
    return _TRELLO_CREDS


def _trello_api(method, path, **params):
    creds = _trello_creds()
    if not creds:
        return {"error": "trello creds unavailable"}
    import urllib.parse
    import urllib.request
    params["key"] = creds["key"]; params["token"] = creds["token"]
    body = urllib.parse.urlencode(params).encode()
    url = f"https://api.trello.com/1{path}"
    req = (urllib.request.Request(f"{url}?{body.decode()}", method="GET") if method == "GET"
           else urllib.request.Request(url, data=body, method=method))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _trello_list_id(name):
    creds = _trello_creds()
    if not (creds and name):
        return None
    if not _TRELLO_LISTS:
        for l in (_trello_api("GET", f"/boards/{creds['board']}/lists", fields="name") or []):
            if isinstance(l, dict) and l.get("id"):
                _TRELLO_LISTS[l["name"].lower()] = l["id"]
    return _TRELLO_LISTS.get(name.lower())


def _trello_target(phase):
    t = REPORTING.get("trello", {})
    return t.get(_TRELLO_PHASE_KEY[phase], _TRELLO_PHASE_DEFAULT[phase])


def trello_open(db, mid, goal):
    """Create the mission's card in the on-deck list; persist its id in missions.jira_key
    (the external-tracker slot). No-op unless reporting.trello.enabled. Returns card id
    ('DRYRUN' under --dry-run so downstream moves still log)."""
    if not REPORTING.get("trello", {}).get("enabled"):
        return None
    name = f"[Conductor] {_title(goal)}"
    if DRY_RUN:
        db.log_event(mid, "trello_dryrun", {"action": "create", "list": _trello_target("on_deck"), "name": name})
        return "DRYRUN"
    lid = _trello_list_id(_trello_target("on_deck"))
    card = (_trello_api("POST", "/cards", idList=lid, name=name, desc=f"Conductor mission `{mid[:8]}`\n\n{goal[:800]}")
            if lid else {"error": f"list {_trello_target('on_deck')!r} not found"})
    cid = card.get("id")
    if cid:
        db.update_mission(mid, jira_key=cid)   # jira_key doubles as the external-tracker id
    db.log_event(mid, "trello", {"action": "create", "card": cid,
                                 "url": card.get("shortUrl") or card.get("url"), "error": card.get("error")})
    return cid


def trello_move(db, mid, card_id, phase, goal=""):
    """Move the mission card to the list configured for `phase` (on_deck|in_progress|done|failed)."""
    if not (REPORTING.get("trello", {}).get("enabled") and card_id):
        return
    target = _trello_target(phase)
    if DRY_RUN:
        db.log_event(mid, "trello_dryrun", {"action": "move", "phase": phase, "list": target})
        return
    lid = _trello_list_id(target)
    res = _trello_api("PUT", f"/cards/{card_id}", idList=lid) if lid else {"error": f"list {target!r} not found"}
    db.log_event(mid, "trello", {"action": "move", "phase": phase, "list": target, "error": res.get("error")})


def trello_comment(db, mid, card_id, text):
    """Comment the deliverable summary on the mission card."""
    if not (REPORTING.get("trello", {}).get("enabled") and card_id):
        return
    if DRY_RUN:
        db.log_event(mid, "trello_dryrun", {"action": "comment", "text": text[:200]})
        return
    res = _trello_api("POST", f"/cards/{card_id}/actions/comments", text=text[:16000])
    db.log_event(mid, "trello", {"action": "comment", "error": res.get("error")})


def _report_body(goal, artifact, verdict, branches):
    body = (f"**Goal:** {goal}\n\n{artifact}\n\n"
            f"**Verification:** pass={verdict.get('pass')} · reviewers={verdict.get('reviewers')}")
    if branches:
        body += "\n\n**Branch(es):** " + ", ".join(f"{b['repo']}:{b['branch']}" for b in branches)
    return body


def _commit_worktrees(mid, subtasks, goal):
    """Commit each building subtask's worktree onto the mission branch (local). The
    branch is the deliverable; pushing/MR is gated separately. Pre-commit hooks that
    flag pre-existing (non-mission) issues shouldn't block the checkpoint — the reviewer
    fleet + CI on the MR are the real gate — so retry with --no-verify on hook failure
    (recorded, never silent)."""
    branches = []
    repos = {s.get("repo") for s in subtasks if s.get("repo") and _is_git(_repo_dir(s["repo"]))}
    for repo in repos:
        ws = workspace(mid, repo)
        if not os.path.isdir(ws):
            continue
        subprocess.run(["git", "-C", ws, "add", "-A"], capture_output=True, text=True)
        msg = f"conductor: {goal[:70]}"
        r = subprocess.run(["git", "-C", ws, "commit", "-m", msg], capture_output=True, text=True)
        bypassed = False
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr).lower():
            r = subprocess.run(["git", "-C", ws, "commit", "--no-verify", "-m", msg], capture_output=True, text=True)
            bypassed = r.returncode == 0
        # Reconcile a worker that forked its own branch (belt for the instruction rule): the
        # worktree is the source of truth for the WORK. If HEAD is on a different branch than the
        # mission branch, point the mission branch at the worktree's HEAD so the commit that
        # actually exists is what we push/MR — not the empty mission branch the worker abandoned.
        mission_branch = _branch(goal, mid)
        cur_branch = subprocess.run(["git", "-C", ws, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
        forked = bool(cur_branch) and cur_branch != "HEAD" and cur_branch != mission_branch
        if forked:
            # `checkout -B` (not `branch -f` + checkout): one atomic step that force-moves the
            # mission ref to the worker's HEAD AND makes it current, so HEAD == mission branch ==
            # the worker's commit and every downstream rev-parse/rev-list/push is consistent (no
            # window where HEAD still points at the fork). The tree is already clean here — the
            # commit above ran `git add -A && commit` — so -B won't complain about a dirty switch.
            subprocess.run(["git", "-C", ws, "checkout", "-B", mission_branch, "HEAD"],
                           capture_output=True, text=True)
        head = subprocess.run(["git", "-C", ws, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        # Does this branch actually differ from the MR target? (commits ahead). A mission that
        # concludes "no change needed" leaves the branch == target → an empty MR; flag that so
        # report() can short-circuit. Unknown/bad ref → assume changed (never skip a real diff).
        target = REPORTING.get("mr", {}).get("target", "main")
        ahead = subprocess.run(["git", "-C", ws, "rev-list", "--count", f"{target}..HEAD"],
                               capture_output=True, text=True).stdout.strip()
        changed = (not ahead.isdigit()) or int(ahead) > 0
        branches.append({"repo": repo, "branch": mission_branch, "worktree": ws, "head": head,
                         "committed": r.returncode == 0, "hooks_bypassed": bypassed, "changed": changed,
                         "forked_from": cur_branch if forked else None})
    return branches


async def reporter_agent(instruction, mcp_names):
    """One-shot agent with the given MCP servers (e.g. atlassian) that performs a
    reporting action and returns structured JSON."""
    mcp = _load_mcp(mcp_names)
    opts = ClaudeAgentOptions(model=MODEL, effort=ORCH_EFFORT, setting_sources=[], mcp_servers=mcp,
                              allowed_tools=[f"mcp__{s}__*" for s in mcp], permission_mode="bypassPermissions")
    text = []
    try:
        async for msg in query(prompt=instruction, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text.append(b.text)
        return _extract_json("".join(text))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _first_url(text):
    m = re.search(r"https?://\S+", text or "")
    return m.group(0) if m else None


def _open_mr(b, title, description, draft=False):
    subprocess.run(["git", "-C", b["worktree"], "push", "-u", "origin", b["branch"]],
                   capture_output=True, text=True)
    # Idempotent: reuse an existing open MR for this source branch instead of a second
    # `glab mr create` (which fails "Failed to create merge request" + drops a recover/mr.json).
    # This happens when a worker self-opened an MR during the build, and on a partial re-run.
    existing = subprocess.run(
        ["glab", "mr", "list", "--source-branch", b["branch"], "--output", "json"],
        cwd=b["worktree"], capture_output=True, text=True)
    if existing.returncode == 0:
        try:
            mrs = json.loads(existing.stdout or "[]")
        except (ValueError, TypeError):
            mrs = []
        url = next((m.get("web_url") for m in mrs if m.get("web_url")), None)
        if url:
            return f"reused existing MR: {url}"
    args = ["glab", "mr", "create", "--source-branch", b["branch"],
            "--target-branch", REPORTING.get("mr", {}).get("target", "main"),
            "--title", title, "--description", description, "--yes"]
    if draft or REPORTING.get("mr", {}).get("draft"):   # per-call override (partial forces draft)
        args.append("--draft")
    r = subprocess.run(args, cwd=b["worktree"], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


# Findings the false-positive gate rejects before filing (cheap heuristic, not a full verify):
# carried from the queue-techdebt skill. A finding whose `what`/`fix_hint` matches is skipped.
_TRIAGE_REJECT_PATTERNS = [
    r"except\s+\w+\s*,\s*\w+\s*:",          # py2 `except A, B:` — a SyntaxError under py3.14, not a real bug
    r"\bexcept .*,.* is a syntax error",
    r"formatter would revert|linter would revert|auto-?format would (revert|undo)",
]


def _norm_where(where):
    """Normalize a finding's `where` to a dedupe key: collapse to file:function or file:line so
    coupled findings from multiple lenses (same locus) fold into one ticket."""
    w = (where or "").strip().lower()
    w = re.sub(r"\s+", " ", w)
    m = re.match(r"([\w./-]+)(?::(\d+)|\b.*?\b(\w+)\(\))?", w)
    if not m:
        return w[:80]
    file, line, fn = m.group(1), m.group(2), m.group(3)
    return f"{file}:{fn or line or ''}".rstrip(":")


def _triage_rejected(f):
    """True if a finding hits the false-positive gate (skip filing)."""
    blob = f"{f.get('what', '')} {f.get('fix_hint', '')}".lower()
    return any(re.search(p, blob) for p in _TRIAGE_REJECT_PATTERNS)


async def _file_triage_tickets(db, mid, goal, verdict, src, tracking_key, mr_url, cap=6):
    """File the residual reviewer findings as child tickets under the Claude Queue epic (FC-1239)
    via the queue-techdebt contract. Keep blocker+major, dedupe by locus, cap at `cap` (roll the
    remainder into one 'N more' ticket — never silently drop), and skip false positives. Returns
    the created keys. DRY_RUN → logs the would-file payloads instead of creating. Reuses
    reporter_agent (atlassian) — does not re-implement Atlassian calls."""
    j = REPORTING.get("jira", {})
    findings = verdict.get("findings") or []
    # 1. filter severity + false-positive gate
    kept, rejected = [], 0
    for f in findings:
        if f.get("severity") not in ("blocker", "major"):
            continue
        if _triage_rejected(f):
            rejected += 1
            db.log_event(mid, "triage_rejected", {"where": f.get("where"), "what": (f.get("what") or "")[:120]})
            continue
        kept.append(f)
    # 2. dedupe by normalized locus (coupled multi-lens findings → one ticket)
    by_where, order = {}, []
    for f in kept:
        k = _norm_where(f.get("where"))
        if k not in by_where:
            by_where[k] = f
            order.append(k)
    deduped = [by_where[k] for k in order]
    # 3. cap: keep first `cap`, roll the rest into a single "N more" ticket (never drop silently)
    head, overflow = deduped[:cap], deduped[cap:]
    if overflow:
        db.log_event(mid, "triage_capped", {"kept": len(head), "overflow": len(overflow)})

    epic = "FC-1239"   # the Claude Queue tech-debt epic — triage findings route HERE, not the src epic
    team_id = "bee517c7-f0ef-499f-83ff-5a0ff5446959"   # Finding Care (customfield_10001)
    assignee = j.get("assignee")
    refs = (f"\n\n### References\n"
            f"- Tracking Task: {tracking_key or '(none)'}\n"
            f"- Draft MR: {mr_url or '(none)'}\n"
            + (f"- Source ticket: {src}\n" if src else ""))

    def _payload(f):
        lens = f.get("lens", "review")
        return {
            "summary": f"[{lens}] {(f.get('what') or 'finding')[:100]}"[:110],
            "severity": f.get("severity"),
            "body": (f"### Problem\n{f.get('what', '')}\n\n"
                     f"### Where\n{f.get('where', '(unspecified)')}\n\n"
                     + (f"### Fix\n{f['fix_hint']}\n\n" if f.get("fix_hint") else "")
                     + refs.lstrip()),
        }

    payloads = [_payload(f) for f in head]
    if overflow:
        more = "\n".join(f"- [{f.get('lens')}] {f.get('what', '')[:90]} — {f.get('where', '')}" for f in overflow)
        payloads.append({
            "summary": f"[triage] {len(overflow)} more findings from mission {mid[:8]}"[:110],
            "severity": "major",
            "body": f"### Problem\n{len(overflow)} additional blocker/major findings not filed individually:\n\n{more}\n{refs}",
        })

    if not payloads:
        db.log_event(mid, "triaged", {"tickets": [], "rejected": rejected, "capped": 0})
        return []

    if DRY_RUN or not (j.get("enabled") and j.get("project")):
        db.log_event(mid, "triage_dryrun",
                     {"would_file": [{"summary": p["summary"], "parent": epic, "severity": p["severity"]} for p in payloads],
                      "rejected": rejected, "capped": len(overflow)})
        return []

    keys = []
    for p in payloads:
        # Two-call gotcha: Team (customfield_10001) is SILENTLY DROPPED on create → create first,
        # then editJiraIssue to set it. Encoded in the prompt exactly as the skill documents.
        res = await reporter_agent(
            f"Create a Jira issue, then set its Team field in a SECOND call. "
            f"Step 1 — create: project key={j['project']!r}, issue type='Task', "
            f"parent epic={epic!r}, assignee accountId={assignee!r}, summary={p['summary']!r}, "
            f"description (markdown):\n\n{p['body']}\n\n"
            f"Step 2 — the Team field customfield_10001 is a PLAIN STRING {team_id!r} and is silently "
            f"dropped on create, so after creating, call editJiraIssue to set customfield_10001={team_id!r}. "
            'Return ONLY JSON: {"key":"<created issue key>"}.', ["atlassian"])
        if res.get("key"):
            keys.append(res["key"])
        db.log_event(mid, "triage_ticket", {"key": res.get("key"), "summary": p["summary"],
                                            "severity": p["severity"], "error": res.get("error")})

    db.log_event(mid, "triaged", {"tickets": keys, "rejected": rejected, "capped": len(overflow)})
    return keys


async def report(db, mid, goal, artifact, subtasks, verdict, draft=False, triage=False):
    """Report a finished mission. `draft` forces a DRAFT MR (partial missions — code failed
    verification, never signal ready). `triage` files the residual reviewer findings as
    Claude-Queue tickets after the tracking Task (best-effort exhaustion path)."""
    targets = ["db"]
    m = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", goal or "")
    src = m.group(1) if m else None

    branches = _commit_worktrees(mid, subtasks, goal)
    changed = [b for b in branches if b.get("changed")]
    # No-change short-circuit: the mission ran but produced no diff (e.g. the fix was already in
    # place). Skip the empty MR + the misleading source-ticket transition; record it + comment.
    no_changes = bool(branches) and not changed
    if branches:
        targets.append("branch")
        db.log_event(mid, "committed", {"branches": [
            {k: b[k] for k in ("repo", "branch", "head", "committed", "hooks_bypassed", "changed")} for b in branches]})

    # Build the MR description + title BEFORE opening the MR (can't reference its own URL).
    desc = _report_body(goal, artifact, verdict, branches)
    if draft:
        # Honesty banner: a partial mission's code did NOT pass verification. The draft state +
        # this banner + the queued findings keep it from ever reading as "ready to merge".
        n_replans = (db.get_mission(mid) or {}).get("replan_count", MAX_REPLANS)
        desc = (f"> ⚠️ This MR did not pass Conductor verification (exhausted {n_replans} replans). "
                f"It is a preserved best-effort with the open findings tracked below. Do not merge as-is.\n\n"
                + desc)
    if src:
        desc += f"\n\n**Source ticket:** {src}"
    _mark = "[DRAFT/partial] " if draft else ""
    title = f"{_mark}[Conductor] {src}: {_title(goal)}" if src else f"{_mark}[Conductor] {_title(goal)}"

    mr_url = None
    if REPORTING.get("mr", {}).get("enabled") and not DRY_RUN and changed:
        for b in changed:
            out = _open_mr(b, title, desc, draft=draft)
            mr_url = mr_url or _first_url(out)
            db.log_event(mid, "mr", {"repo": b["repo"], "branch": b["branch"], "url": _first_url(out), "out": out[:300]})
        targets.append("mr")
    elif no_changes:
        db.log_event(mid, "no_changes", {"reason": "no diff vs target on any branch — skipped MR + source transition"})
    elif branches:
        db.log_event(mid, "mr_dryrun", {"branches": [b["branch"] for b in branches]})

    # Fuller body for Jira/Confluence includes the MR URL.
    body = desc + (f"\n\n**MR:** {mr_url}" if mr_url else "")

    # Jira — mission tracking issue under the Claude Queue epic, assigned.
    j = REPORTING.get("jira", {})
    jira_key = None
    if j.get("enabled") and not DRY_RUN and j.get("project") and not no_changes:
        _pfx = "[partial] " if triage else ""
        summary = f"{_pfx}[Conductor] {src}" if src else f"{_pfx}[Conductor] {goal.splitlines()[0][:80]}"
        res = await reporter_agent(
            f"Create a Jira issue and return it. Fields: project key={j['project']!r}, "
            f"issue type={j.get('issue_type', 'Task')!r}, summary={summary!r}, "
            f"{_epic_clause(src)}, assignee accountId={j.get('assignee')!r}. "
            f"Description:\n\n{body}\n\n"
            f"Use the atlassian Jira tools; if a field can't be set on create for this project type, "
            f"set it in a follow-up edit. "
            'Return ONLY JSON: {"key":"<created issue key>"}.', ["atlassian"])
        jira_key = res.get("key")
        if jira_key:
            db.update_mission(mid, jira_key=jira_key)
            targets.append("jira")
        db.log_event(mid, "jira", {"key": jira_key, "epic": _jira_epic() or None, "error": res.get("error")})
    else:
        db.log_event(mid, "jira_dryrun", {"project": j.get("project"), "summary": goal[:120]})

    # Triage (partial missions): file the residual reviewer findings as Claude-Queue tickets so
    # "fix it later" is an actionable queue, not a stranded DB row. The human reviewing the draft
    # MR is the router. Runs even under DRY_RUN (logs the would-file payloads).
    if triage:
        tkeys = await _file_triage_tickets(db, mid, goal, verdict, src, jira_key, mr_url)
        if tkeys:
            targets.append("triage")

    # Source ticket — comment + (only when we changed something) transition (ticket-sourced).
    if src and j.get("enabled") and not DRY_RUN and j.get("update_source"):
        if no_changes:
            _tgt = REPORTING.get("mr", {}).get("target", "main")
            instr = (f"On Jira issue {src}: add a comment that a Conductor mission verified this is "
                     f"ALREADY SATISFIED on {_tgt} — no change was needed (no MR opened). Summary: {goal[:200]}. "
                     f"Do NOT change the ticket's status. "
                     'Return ONLY JSON: {"commented":true,"transitioned":null}.')
        else:
            instr = (f"On Jira issue {src}: (1) add a comment that a Conductor mission completed this work"
                     f"{' — tracking issue ' + jira_key if jira_key else ''}"
                     f"{', MR ' + mr_url if mr_url else ''}"
                     f"{', branch ' + changed[0]['branch'] if changed else ''}. Summary: {goal[:200]}. "
                     f"(2) Look up {src}'s available transitions and move it to the code-review / in-review state "
                     f"(work complete, awaiting review); if none matches cleanly, leave the status. "
                     'Return ONLY JSON: {"commented":true,"transitioned":"<new status or null>"}.')
        res = await reporter_agent(instr, ["atlassian"])
        db.log_event(mid, "jira_source", {"ticket": src, "result": res, "no_changes": no_changes})
        targets.append("jira-source")

    # Confluence — DRAFT writeup under the Missions folder.
    c = REPORTING.get("confluence", {})
    if c.get("enabled") and not DRY_RUN and c.get("space") and not no_changes:
        res = await reporter_agent(
            f"Create a Confluence page. space key={c['space']!r}, "
            f"{'parent page/folder id=' + str(c['parent']) + ', ' if c.get('parent') else ''}"
            f"status={c.get('status', 'draft')!r}, title={('Mission: ' + goal[:120])!r}, "
            f"body (markdown):\n\n{body}\n\n"
            'Return ONLY JSON: {"url":"<page url>"}.', ["atlassian"])
        if res.get("url"):
            targets.append("confluence")
        db.log_event(mid, "confluence", {"url": res.get("url"), "space": c["space"], "error": res.get("error")})
    else:
        db.log_event(mid, "confluence_dryrun",
                     {"space": c.get("space"), "status": c.get("status", "draft"), "title": f"Mission: {goal[:120]}"})

    # Trello — comment the deliverable summary on the mission card (the list move to
    # done/failed happens in run_mission/resume after finalize).
    if REPORTING.get("trello", {}).get("enabled"):
        trello_comment(db, mid, (db.get_mission(mid) or {}).get("jira_key"),
                       _report_body(goal, artifact, verdict, branches))
        targets.append("trello")

    db.log_event(mid, "reported", {"targets": targets, "jira": jira_key, "mr": mr_url})
    return targets


async def _safe_synthesize(db, mid, goal, subs, verdict, verified=True):
    """Synthesize the mission writeup, degrading to a plain summary if the judge flakes out
    (even after retries) rather than crashing before report() and stranding committed work.
    Shared by the pass path and the best-effort partial path. Returns the artifact string."""
    db.update_mission(mid, status="synthesizing")
    summaries = [{"subtask": s["subtask_key"], "summary": (s.get("result") or {}).get("summary", ""),
                  "artifacts": (s.get("result") or {}).get("artifacts", [])} for s in subs]
    tag = "verified (pass)" if verified else "did NOT pass verification (best-effort)"
    try:
        art = await synthesize(goal, {"subtasks": summaries}, verdict)
    except Exception as e:
        fallback = "; ".join(s["summary"] for s in summaries if s.get("summary")) or goal
        art = {"artifact": f"Mission {tag}; auto-summary unavailable "
                           f"({type(e).__name__}). Subtask summaries: {fallback}"[:4000]}
        db.log_event(mid, "synthesize_degraded", {"error": f"{type(e).__name__}: {e}"})
    db.log_event(mid, "synthesized", {"artifact": (art.get("artifact") or "")[:4000]})
    return art.get("artifact", "")


async def finalize(db, mid, goal, start_round=0):
    """Shared tail for run + resume: DAG/verify/re-plan loop → synthesize → report → finish."""
    verdict, ok = await run_and_verify(db, mid, goal, start_round=start_round)
    subs = db.list_subtasks(mid)
    if not ok:
        replans = (db.get_mission(mid) or {}).get("replan_count")
        # Best-effort triage on exhaustion (opt-in): don't dead-end. Preserve the attempt behind
        # a DRAFT MR + enumerate the residual reviewer findings as Claude-Queue tickets, finish
        # `partial`. Never emits a false "ready" signal — the draft MR + partial status carry the
        # honesty. Default `escalate` keeps the historical stop-and-strand behavior.
        if ON_EXHAUSTED == "partial":
            art = await _safe_synthesize(db, mid, goal, subs, verdict, verified=False)
            targets = await report(db, mid, goal, art, subs, verdict, draft=True, triage=True)
            db.finish_mission(mid, "partial")
            db.log_event(mid, "partial", {"verdict": verdict, "replans": replans, "targets": targets})
            print(f"[conductor] PARTIAL after {MAX_REPLANS} re-plans · mission {mid} · reported→{targets}")
            return mid, "partial"
        db.finish_mission(mid, "escalated")
        db.log_event(mid, "escalated", {"verdict": verdict, "replans": replans})
        print(f"[conductor] ESCALATED after {MAX_REPLANS} re-plans · mission {mid}")
        return mid, "escalated"
    art = await _safe_synthesize(db, mid, goal, subs, verdict, verified=True)
    targets = await report(db, mid, goal, art, subs, verdict)
    db.finish_mission(mid, "done")
    print(f"[conductor] DONE · mission {mid} · reported→{targets}")
    return mid, "done"


# ── the spine ────────────────────────────────────────────────────────────────
async def run_mission(goal: str, created_by: str = "cli") -> tuple:
    db = Db()
    mid = None
    card_id = None
    try:
        _set_sess("conductor")
        print("[conductor] classifying…")
        cr = await classify(goal)
        mid = db.create_mission(goal, type=cr.get("type", "building"),
                                route=cr.get("route", "conductor"), repos=cr.get("repos", []),
                                datasources=cr.get("datasources", []), created_by=created_by, device=HOST)
        _set_sess(f"conductor-{mid[:8]}")
        db.log_event(mid, "classified", cr)
        print(f"[conductor] mission {mid[:8]} · type={cr.get('type')} repos={cr.get('repos')}")
        card_id = trello_open(db, mid, goal)   # on-deck (no-op unless reporting.trello.enabled)

        design_brief = {}
        if PLAN_GATE_ON and PLAN_GATE_DESIGN:
            print("[conductor] design…")
            design_brief = await design(cr)
            db.log_event(mid, "designed", design_brief)
            _write_design_md(mid, goal, design_brief)

        print("[conductor] planning…")
        p = await plan({**cr, "design": design_brief} if design_brief else cr)

        if PLAN_GATE_ON:
            print("[conductor] plan-gate (adversarial pre-build review)…")
            p, gate = await run_plan_gate(db, mid, goal, cr, design_brief, p, card_id)
            if gate == "stop":
                trello_move(db, mid, card_id, "failed", goal)
                db.finish_mission(mid, "failed")
                db.log_event(mid, "escalated", {"reason": "plan-gate not passed"})
                _slack_relay(f"⛔ Conductor {mid[:8]} halted at plan-gate · {_title(goal)}")
                return mid, "failed"

        if design_brief:
            p["design"] = design_brief
        db.update_mission(mid, plan=p, status="dispatched")
        for st in p.get("subtasks", []):
            db.create_subtask(mid, st["id"], st["goal"], st.get("profile", "one-shot"),
                              repo=st.get("repo"), depends_on=st.get("depends_on", []), effort=WORKER_EFFORT)
        db.log_event(mid, "planned", {"strategy": p.get("strategy"), "subtasks": len(p.get("subtasks", []))})
        if PLAN_GATE_ON:
            label = "📋 Plan approved" if gate == "approved" else "📋 Plan (forced past gate)"
            trello_comment(db, mid, card_id, f"{label} · " + (p.get("strategy", "")[:400])
                           + f"\n{len(p.get('subtasks', []))} subtask(s).")
        subtasks = db.list_subtasks(mid)
        print(f"[conductor] plan: {p.get('strategy')} · {len(subtasks)} subtask(s)")
        if not subtasks:
            trello_move(db, mid, card_id, "failed", goal)
            db.finish_mission(mid, "failed")
            db.log_event(mid, "escalated", {"reason": "empty plan"})
            return mid, "failed"

        # Slices C–E: DAG → verify/re-plan loop → synthesize → report.
        trello_move(db, mid, card_id, "in_progress", goal)
        rid, status = await finalize(db, mid, goal)
        # partial = best-effort preserved behind a draft MR + queued findings; card goes to the
        # failed/needs-human list (no dedicated 'partial' Trello column), the mission status stays partial.
        trello_move(db, mid, card_id, "done" if status == "done" else "failed", goal)
        if status == "partial":
            _slack_relay(f"🟡 Conductor {mid[:8]} PARTIAL · {_title(goal)} · "
                         f"draft MR + findings queued (see mission {mid[:8]})")
        return rid, status
    except Exception as e:
        print(f"[conductor] ERROR: {type(e).__name__}: {e}")
        if mid:
            trello_move(db, mid, card_id, "failed", goal)
            db.finish_mission(mid, "failed")
            db.log_event(mid, "error", {"error": f"{type(e).__name__}: {e}"})
        return mid or "none", "failed"
    finally:
        db.close()


# ── resume (slice F) ─────────────────────────────────────────────────────────
async def resume_mission(mid_prefix: str) -> tuple:
    """Pick a non-terminal mission back up from DB state (state is fully persisted)."""
    db = Db()
    mid = mid_prefix
    try:
        m = db.find_mission(mid_prefix)
        if not m:
            print(f"[conductor] no mission matching {mid_prefix!r}")
            return mid_prefix, "unknown"
        mid, goal = m["id"], m["goal"]
        if m["status"] in ("done", "failed", "partial"):
            print(f"[conductor] mission {mid[:8]} already {m['status']} — nothing to resume")
            return mid, m["status"]
        if not db.list_subtasks(mid):
            print(f"[conductor] mission {mid[:8]} has no plan persisted — re-run it as a new mission")
            return mid, "unresumable"
        _set_sess(f"conductor-{mid[:8]}")
        card_id = m.get("jira_key")   # the mission's Trello card, if any
        db.log_event(mid, "resumed", {"from_status": m["status"], "round": m["replan_count"]})
        print(f"[conductor] resuming {mid[:8]} · status={m['status']} · round={m['replan_count']}")
        for s in db.list_subtasks(mid):   # workers that were mid-flight when the process died
            if s["status"] == "running":
                db.update_subtask(s["id"], status="pending")
        trello_move(db, mid, card_id, "in_progress", goal)
        rid, status = await finalize(db, mid, goal, start_round=int(m["replan_count"] or 0))
        trello_move(db, mid, card_id, "done" if status == "done" else "failed", goal)
        if status == "partial":
            _slack_relay(f"🟡 Conductor {mid[:8]} PARTIAL · {_title(goal)} · "
                         f"draft MR + findings queued (see mission {mid[:8]})")
        return rid, status
    except Exception as e:
        print(f"[conductor] ERROR: {type(e).__name__}: {e}")
        return mid, "failed"
    finally:
        db.close()


# ── sdlc pipeline driver (S1: read-only) ─────────────────────────────────────
def _scan_sdlc(ws_root=None):
    """Shell the sdlc plugin's scan.py → parsed JSON (the deterministic router).
    `ws_root` overrides the workspace root for this call. Returns the raw scan
    payload, or an {"error": …} dict (never raises)."""
    if not os.path.isfile(SDLC_SCAN):
        return {"error": "sdlc-plugin-missing", "scan": SDLC_SCAN}
    # Resolve the workspace root: explicit arg > a caller-set SDLC_WS_ENV override (only when
    # the plugin defines one) > first existing CUSTOM_WORKSPACE_ROOTS dir > configured
    # sdlc.workspace_root. When none resolves, leave the env unset + let scan.py fall through
    # to its OWN discovery (accurate `workspace-not-bootstrapped`, not `-override-invalid`).
    env = {**os.environ}
    root = None
    if ws_root:
        root = ws_root
    elif SDLC_WS_ENV and env.get(SDLC_WS_ENV):
        root = env[SDLC_WS_ENV]                                  # caller-set: respect verbatim
    else:
        for r in (os.environ.get("CUSTOM_WORKSPACE_ROOTS", "") or "").split(":"):
            r = os.path.expanduser(os.path.expandvars(r.strip()))
            if r and os.path.isdir(r):
                root = r; break
        if root is None and os.path.isdir(SDLC_WS_ROOT):
            root = SDLC_WS_ROOT
    # Hand the resolved root to scan.py via the plugin's OWN env contract if the config names
    # one (sdlc.workspace_env); otherwise scan.py self-discovers from cwd.
    if SDLC_WS_ENV:
        if root:
            env[SDLC_WS_ENV] = root
        else:
            env.pop(SDLC_WS_ENV, None)
    cwd = root if (root and os.path.isdir(root)) else HOME
    r = subprocess.run(["python3", SDLC_SCAN], capture_output=True, text=True, env=env, cwd=cwd)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"error": "scan-parse-failed", "rc": r.returncode,
                "stdout": (r.stdout or "")[:400], "stderr": (r.stderr or "")[:400]}


def _sdlc_resolve_project(goal, scan):
    """Map a goal/ticket to a project in the scan payload. Returns
    (project_dict|None, reason). None ⇒ a NEW project (bootstrap via create-requirements)."""
    projects = scan.get("projects") or []
    m = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", goal or "")
    ticket = m.group(1) if m else None
    if ticket:
        for p in projects:
            if (p.get("jira_issue") or "").upper() == ticket.upper():
                return p, f"matched ticket {ticket} → project '{p.get('project')}'"
    low = (goal or "").lower()
    for p in projects:                                   # else a project slug named in the goal
        slug = p.get("project") or ""
        if slug and slug.lower() in low:
            return p, f"matched project slug '{slug}'"
    return None, ("no existing project matches"
                  + (f" ticket {ticket}" if ticket else "")
                  + " → would bootstrap via create-requirements")


def _sdlc_projected_plan(project):
    """Projected remaining create-* stages up to the plan.md boundary. DRY-RUN ONLY —
    scan.py gives the authoritative immediate `next`; this projects the forward chain
    from artifact existence (self-skipped domain-model/tech-design count as satisfied)."""
    sib = project.get("siblings") or {}
    skipped = project.get("skipped") or {}
    have = {
        "requirements": True,                            # the project exists ⇒ requirements.md present
        "domain-model": bool(sib.get("domain_model")) or bool(skipped.get("domain_model")),
        "tech-design": bool(sib.get("tech_design")) or bool(skipped.get("tech_design")),
        "validation": bool(sib.get("validation")),
        "plan": bool(sib.get("plan")),
    }
    return [f"create-{s}" for s in SDLC_CHAIN if not have.get(s)] + ["(→ human: review + code phase)"]


def sdlc_dry_run(goal):
    """Preview an --sdlc mission: resolve the target project + print the stage plan.
    Purely read-only — shells scan.py, mutates nothing."""
    scan = _scan_sdlc()
    print("[conductor] --sdlc DRY RUN (read-only preview; nothing mutated)")
    print(f"  plugin:         {SDLC_PLUGIN_DIR}")
    print(f"  workspace_root: {scan.get('workspace_root', SDLC_WS_ROOT)}")
    if scan.get("error"):
        detail = next((b for b in (scan.get("reason"), scan.get("value"), scan.get("cwd"),
                                   scan.get("scan"), scan.get("stderr")) if b), "")
        print(f"  ⚠ scan: {scan['error']}" + (f" — {detail}" if detail else ""))
        if scan["error"] == "workspace-not-bootstrapped":
            print("  → setup: clone the context repos (project-context-*) under a workspace root, then "
                  "set CUSTOM_WORKSPACE_ROOTS\n           (colon-separated; or sdlc.workspace_root in conductor.yaml).")
        return
    proj, reason = _sdlc_resolve_project(goal, scan)
    print(f"  goal:           {_title(goal)}")
    print(f"  resolve:        {reason}")
    if proj:
        print(f"  project:        {proj.get('path')}  "
              f"[status={proj.get('status')} phase={proj.get('phase')}]")
        print(f"  scan.next:      {proj.get('next')}   (authoritative immediate step)")
        print("  projected stages → plan.md boundary:")
        for s in _sdlc_projected_plan(proj):
            print(f"    - {s}")
    else:
        print("  projected stages (new project) → plan.md boundary:")
        for stage in SDLC_CHAIN:
            print(f"    - create-{stage}")
        print("    - (→ human: review + code phase)")


# ── sdlc leaf executor (S2: headless single-stage run) ──────────────────────
SDLC_WORK_TOOLS = ["Bash", "Edit", "Write", "Read", "Grep", "Glob"]

# The whole headless lever is prompt wording: the sdlc leaves trigger their non-interactive
# escape hatches by what the INVOKING PROMPT says ("auto mode", "pre-confirmed: …"), and
# AskUserQuestion is disallowed so the worker self-answers every fork from mission context
# (= the locked "Conductor judgment answers" decision) instead of stalling for a human. The
# worker reads + follows the leaf's SKILL.md directly (the Skill tool can't invoke plugin
# skills headlessly).
SDLC_HEADLESS_CONVENTION = (
    "You are a headless Conductor SDLC worker. No human is available and the AskUserQuestion "
    "tool is unavailable. Read and follow the assigned SDLC skill's SKILL.md to completion, "
    "writing the artifact file(s) to disk. At every decision the skill would normally ASK "
    "about, choose the forward/recommended option yourself using the mission context (the "
    "ticket + goal below) and proceed — never pause. As soon as the artifact file is written, "
    "STOP immediately: do NOT run a grilling pass, a next-step picker, or the next stage. "
    "If — and ONLY if — you genuinely cannot proceed without a human decision (e.g. you cannot "
    "determine the area/team/project for a brand-new spec), output a single line beginning "
    "'ESCALATE:' that states exactly what is blocking, and stop without guessing."
)


def _sdlc_escape_hint(leaf: str) -> str:
    """Per-leaf hint that fires the skill's built-in non-interactive path (minimizes forks)."""
    base = leaf.split(":")[-1]
    if base == "create-validation":
        return ("Run in AUTO MODE (non-interactive): force validation_tier=checklist, no intake "
                "pickers, skip the grilling pass and the next-step picker.")
    if base == "advance-status":
        return "Pre-confirmed: flip the status one step forward — do not re-ask for confirmation."
    if base.startswith("review-"):
        return ("Triage each Open Question yourself — resolve it with the most reasonable answer "
                "from context, or waive it — apply edits inline, then stop. Do not walk a human "
                "through them one at a time.")
    return "Choose the forward/recommended option at every fork yourself; never pause for input."


async def run_sdlc_stage(mid: str, project_dir: str, leaf: str, ctx: str, effort: str = None) -> dict:
    """Run ONE sdlc leaf skill headlessly in `project_dir`. Returns
    {leaf, status, artifacts, escalate, summary}. AskUserQuestion is disallowed; the worker
    self-answers forks from `ctx` (Conductor judgment) and emits 'ESCALATE:' when truly stuck.

    Mechanism = point the worker at the leaf's SKILL.md (read + follow) + disallow
    AskUserQuestion + a headless convention. A faithful upgrade — intercept AskUserQuestion via
    a streaming client's can_use_tool/hooks and inject a chosen answer — is a clean drop-in."""
    leaf_fq = leaf if ":" in leaf else f"{SDLC_PLUGIN_NS}:{leaf}"
    skill_md = _skill_md(leaf_fq)
    if not skill_md:
        return {"leaf": leaf_fq, "status": "error", "artifacts": [], "escalate": None,
                "summary": f"SKILL.md not found for {leaf_fq}"}
    mcp = _load_mcp(["agent-memory", "atlassian"])
    allowed = SDLC_WORK_TOOLS + [f"mcp__{s}__*" for s in mcp]
    prompt = (f"Assigned SDLC stage. Read and follow the procedure at {skill_md} to completion "
              f"(its references/ are alongside it), writing the artifact file(s) under {project_dir}.\n"
              f"{_sdlc_escape_hint(leaf_fq)}\n\nMission context:\n{ctx}\n")
    opts = ClaudeAgentOptions(
        model=MODEL, effort=(effort or WORKER_EFFORT), cwd=project_dir,
        setting_sources=["user", "project"],
        mcp_servers=mcp, allowed_tools=allowed, disallowed_tools=["AskUserQuestion"],
        permission_mode="bypassPermissions", max_turns=80,
        system_prompt={"type": "preset", "preset": "claude_code", "append": SDLC_HEADLESS_CONVENTION},
    )
    text, artifacts, status = [], [], "error"
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    text.append(b.text.strip())
                elif isinstance(b, ToolUseBlock) and b.name in WRITE_TOOLS:
                    fp = b.input.get("file_path") or b.input.get("notebook_path")
                    if fp:
                        artifacts.append(fp if os.path.isabs(fp) else os.path.join(project_dir, fp))
        elif isinstance(msg, ResultMessage):
            status = "done" if msg.subtype == "success" else "error"
    joined = "\n".join(text)
    m = re.search(r"^ESCALATE:\s*(.+)$", joined, re.MULTILINE)
    esc = m.group(1).strip() if m else None
    if esc:
        status = "escalated"
    return {"leaf": leaf_fq, "status": status, "artifacts": sorted(set(artifacts)),
            "escalate": esc, "summary": joined[-1500:]}


# ── sdlc staged spine (S3: scan → leaf → advance loop to the plan.md boundary) ──
def _sdlc_next_kind(nxt: str):
    """Classify a scan.py `next` string → (kind, leaf). kind ∈
    {run, boundary, terminal, decision}. `leaf` is the sdlc:<leaf> to invoke (or None)."""
    if not nxt or nxt.strip() == "—":
        return "terminal", None
    m = re.search(r"/sdlc:([a-z-]+)", nxt)
    leaf = f"sdlc:{m.group(1)}" if m else None
    if leaf in ("sdlc:implement-plan", "sdlc:review-implementation", "sdlc:git-publish"):
        return "boundary", leaf                       # plan.md done (or publishing) → hand to human / S4
    if "mark ready" in nxt.lower():
        return "run", "sdlc:advance-status"            # draft requirements → ready-for-review (pre-confirmed)
    if leaf and (leaf.startswith("sdlc:create-") or leaf.startswith("sdlc:review-")
                 or leaf == "sdlc:advance-status"):
        return "run", leaf
    return "decision", leaf                            # staged-locally / resolve-MR / anything unexpected


def _sdlc_ctx(goal: str, ticket: str, proj: dict) -> str:
    lines = [f"Goal: {goal}"]
    if ticket:
        lines.append(f"Source ticket: {ticket} (you have the atlassian MCP — fetch it for detail).")
    if proj:
        lines.append(f"Project: {proj.get('project')} (team {proj.get('team')}, area {proj.get('area')}) "
                     f"at {proj.get('path')}; repos: {proj.get('repos')}.")
    else:
        lines.append("This is a NEW feature — no project exists yet. Determine area/team/project from the "
                     "ticket/goal and bootstrap requirements.md; if you cannot determine them, ESCALATE.")
    return "\n".join(lines)


async def _sdlc_escalate(db, mid, goal, ticket, why):
    """Pause point: a fork the Conductor can't answer. Log + ping the Slack bus (+ Jira comment)."""
    db.log_event(mid, "escalated", {"why": why, "ticket": ticket})
    msg = (f"⚠️ Conductor SDLC mission {mid[:8]} needs a human: {why}\n   goal: {_title(goal)}"
           + (f" · {ticket}" if ticket else ""))
    try:
        subprocess.run([os.path.expanduser("~/.tmux/agent-send.sh"), "--relay", msg],
                       timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    if ticket and REPORTING.get("jira", {}).get("enabled") and not DRY_RUN:
        try:
            await reporter_agent(
                f"On Jira issue {ticket}: add a comment that a Conductor SDLC mission paused and needs a "
                f"human decision: {why}. Return ONLY JSON: {{\"commented\":true}}.", ["atlassian"])
        except Exception:
            pass
    print(f"[conductor] ESCALATED · {why}")


def _sdlc_commit_artifacts(repo_dir, subpath, goal, mid):
    """Commit the SDLC artifact subtree onto a fresh branch in the project-context repo (the
    docs deliverable). Returns a branch dict (repo/branch/worktree/head/committed/changed) or
    None. Hooks bypassed (docs, not code) — the human review on the MR is the gate."""
    if not _is_git(repo_dir):
        return None

    def g(*a):
        return subprocess.run(["git", "-C", repo_dir, *a], capture_output=True, text=True)
    base = g("rev-parse", "HEAD").stdout.strip()
    branch = f"sdlc/{_slug((subpath.split('/')[-1] if subpath else '') or 'artifacts')}-{mid[:6]}"
    g("checkout", "-B", branch)
    g("add", "--", subpath or ".")
    if not g("status", "--porcelain", "--", subpath or ".").stdout.strip():
        return {"repo": os.path.basename(repo_dir), "branch": branch, "worktree": repo_dir,
                "committed": False, "changed": False}
    r = g("commit", "--no-verify", "-m", f"[Conductor SDLC] {_title(goal)} — spec artifacts through plan.md")
    head = g("rev-parse", "--short", "HEAD").stdout.strip()
    return {"repo": os.path.basename(repo_dir), "branch": branch, "worktree": repo_dir,
            "head": head, "committed": r.returncode == 0, "changed": r.returncode == 0}


async def sdlc_report(db, mid, goal, ticket, proj, ws_root):
    """Report an SDLC mission that reached the plan.md boundary: commit the artifact subtree,
    open a docs MR (dry-run logs it), file a Jira mission issue + comment the source ticket, and
    ping Slack. The code-phase MR is deliberately NOT opened (trust boundary = plan)."""
    targets = ["db"]
    path = (proj or {}).get("path")
    branch_info = None
    if path and ws_root and "/" in path:
        spec_repo, subpath = path.split("/", 1)
        branch_info = _sdlc_commit_artifacts(os.path.join(ws_root, spec_repo), subpath, goal, mid)
        if branch_info:
            db.log_event(mid, "committed", {k: branch_info.get(k)
                         for k in ("repo", "branch", "head", "committed", "changed")})
            targets.append("branch")

    title = f"[Conductor SDLC] {(ticket + ': ') if ticket else ''}{_title(goal)}"
    body = (f"Autonomous SDLC mission produced the spec artifacts **through plan.md** for `{path}`.\n\n"
            f"Trust boundary = plan: the code phase (implement-plan → review-implementation → MR) is "
            f"left to a human." + (f"\n\n**Source ticket:** {ticket}" if ticket else ""))
    mr_url = None
    if branch_info and branch_info.get("changed") and REPORTING.get("mr", {}).get("enabled") and not DRY_RUN:
        out = _open_mr(branch_info, title, body)
        mr_url = _first_url(out)
        db.log_event(mid, "mr", {"branch": branch_info["branch"], "url": mr_url, "out": out[:300]})
        targets.append("mr")
    elif branch_info:
        db.log_event(mid, "mr_dryrun", {"branch": branch_info.get("branch"), "title": title,
                                        "changed": branch_info.get("changed")})

    j = REPORTING.get("jira", {})
    if j.get("enabled") and not DRY_RUN and j.get("project"):
        summary = f"[Conductor SDLC] {ticket}" if ticket else f"[Conductor SDLC] {_title(goal)}"
        body_j = body + (f"\n\n**MR:** {mr_url}" if mr_url else "")
        res = await reporter_agent(
            f"Create a Jira issue: project key={j['project']!r}, issue type={j.get('issue_type', 'Task')!r}, "
            f"summary={summary!r}, {_epic_clause(ticket)}, assignee accountId={j.get('assignee')!r}. "
            f"Description:\n\n{body_j}\n\nReturn ONLY JSON: {{\"key\":\"<key>\"}}.", ["atlassian"])
        if res.get("key"):
            db.update_mission(mid, jira_key=res["key"]); targets.append("jira")
        db.log_event(mid, "jira", {"key": res.get("key"), "error": res.get("error")})
        if ticket and j.get("update_source"):
            await reporter_agent(
                f"On Jira issue {ticket}: add a comment that a Conductor SDLC mission produced the spec "
                f"artifacts through plan.md" + (f" (MR {mr_url})" if mr_url else "")
                + "; the code phase is left to a human — do NOT change the status. "
                'Return ONLY JSON: {"commented":true}.', ["atlassian"])
            targets.append("jira-source")
    else:
        db.log_event(mid, "jira_dryrun", {"project": j.get("project"), "ticket": ticket})

    db.log_event(mid, "sdlc_reported", {"project": path, "boundary": "plan", "targets": targets, "mr": mr_url})
    msg = (f"✅ Conductor SDLC mission {mid[:8]} → plan ready · {_title(goal)}"
           + (f" · {ticket}" if ticket else "") + (f"\n   project: {path}" if path else "")
           + (f"\n   MR: {mr_url}" if mr_url else "")
           + "\n   next: human reviews the artifacts + drives the code phase.")
    try:
        subprocess.run([os.path.expanduser("~/.tmux/agent-send.sh"), "--relay", msg],
                       timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        targets.append("slack")
    except Exception:
        pass
    return targets


async def run_sdlc_mission(goal: str, created_by: str = "cli") -> tuple:
    """Drive the sdlc pipeline autonomously: scan.py routes → run the next leaf headlessly →
    re-scan → repeat, until the plan.md boundary (implement-plan/review-implementation). Each
    stage is a mission_subtasks row; forks the Conductor can't answer escalate to Slack/Jira."""
    if not SDLC_ENABLED:
        print("[conductor] sdlc integration disabled (config sdlc.enabled=false)"); return None, "disabled"
    db = Db(); mid = None
    try:
        _set_sess("conductor-sdlc")
        m = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", goal or ""); ticket = m.group(1) if m else None
        scan = _scan_sdlc()
        if scan.get("error"):
            print(f"[conductor] sdlc: cannot start — {scan['error']} "
                  f"({scan.get('reason') or scan.get('cwd') or ''})")
            return None, "error"
        ws_root = scan.get("workspace_root")
        proj, reason = _sdlc_resolve_project(goal, scan)
        mid = db.create_mission(goal, type="sdlc", route="sdlc",
                                repos=(proj or {}).get("repos") or [], created_by=created_by, device=HOST)
        _set_sess(f"conductor-sdlc-{mid[:8]}")
        db.log_event(mid, "sdlc_resolved", {"reason": reason, "project": (proj or {}).get("path"),
                     "workspace_root": ws_root, "ticket": ticket,
                     "boundary": SDLC.get("trust_boundary", "plan")})
        print(f"[conductor] sdlc mission {mid[:8]} · {reason} · ws={ws_root}")

        last_next, repeat = None, 0
        for stage in range(SDLC_MAX_STAGES):
            scan = _scan_sdlc()
            proj, _ = _sdlc_resolve_project(goal, scan)
            nxt = (proj or {}).get("next") or ("/sdlc:create-requirements" if proj is None else "—")
            kind, leaf = _sdlc_next_kind(nxt)
            db.log_event(mid, "scan", {"stage": stage, "next": nxt, "kind": kind,
                                       "status": (proj or {}).get("status")})
            if kind == "terminal":
                print(f"[conductor] sdlc: nothing left (next={nxt!r})")
                db.finish_mission(mid, "done"); return mid, "done"
            if kind == "boundary":
                print(f"[conductor] sdlc: plan.md boundary (next={nxt!r}) — handing code phase to human")
                db.log_event(mid, "boundary", {"next": nxt, "project": (proj or {}).get("path")})
                targets = await sdlc_report(db, mid, goal, ticket, proj, ws_root)
                db.finish_mission(mid, "done")
                print(f"[conductor] sdlc DONE · mission {mid} · reported→{targets}")
                return mid, "done"
            if kind == "decision":
                await _sdlc_escalate(db, mid, goal, ticket, f"scan next needs a human decision: {nxt!r}")
                db.finish_mission(mid, "escalated"); return mid, "escalated"
            # progress guard — the same `next` twice running means the leaf isn't advancing state
            repeat = repeat + 1 if nxt == last_next else 0
            if repeat >= 2:
                await _sdlc_escalate(db, mid, goal, ticket, f"stuck: '{nxt}' repeated with no progress")
                db.finish_mission(mid, "escalated"); return mid, "escalated"
            last_next = nxt

            project_dir = os.path.join(ws_root, proj["path"]) if proj else ws_root
            base = leaf.split(":")[-1]
            sid = db.create_subtask(mid, f"s{stage}-{base}", nxt, "sdlc-stage")
            db.update_subtask(sid, status="running")
            db.log_event(mid, "stage_started", {"stage": stage, "leaf": leaf, "dir": project_dir},
                         subtask_id=sid)
            print(f"[conductor] sdlc stage {stage}: {leaf}  ({project_dir})")
            res = await run_sdlc_stage(mid, project_dir, leaf, _sdlc_ctx(goal, ticket, proj))
            db.update_subtask(sid, status=res["status"], result=res)
            db.log_event(mid, "stage_done",
                         {k: res[k] for k in ("leaf", "status", "artifacts", "escalate")}, subtask_id=sid)
            if res["status"] == "escalated":
                await _sdlc_escalate(db, mid, goal, ticket, res.get("escalate") or "worker escalated")
                db.finish_mission(mid, "escalated"); return mid, "escalated"
            if res["status"] != "done":
                await _sdlc_escalate(db, mid, goal, ticket,
                                     f"stage {leaf} errored: {(res.get('summary') or '')[:200]}")
                db.finish_mission(mid, "escalated"); return mid, "escalated"

        await _sdlc_escalate(db, mid, goal, ticket,
                             f"did not reach the plan boundary within {SDLC_MAX_STAGES} stages")
        db.finish_mission(mid, "escalated"); return mid, "escalated"
    except Exception as e:
        print(f"[conductor] sdlc ERROR: {type(e).__name__}: {e}")
        if mid:
            db.finish_mission(mid, "failed")
        return mid, "failed"
    finally:
        db.close()


if __name__ == "__main__":
    import anyio
    _load_dotenv()   # idempotent re-load (already ran at import); harmless belt-and-suspenders
    args = sys.argv[1:]
    # Run-mode flags override CONDUCTOR_RUN_MODE (the default; see _resolve_run_mode). Precedence:
    # explicit flag > env > default(dry). --dry-run wins if both flags are passed (fail safe).
    _mode_override = None
    if "--live" in args:
        _mode_override = "live"; args = [a for a in args if a != "--live"]
    if "--run-mode" in args:
        _i = args.index("--run-mode")
        if _i + 1 < len(args):
            _mode_override = args[_i + 1].strip().lower(); del args[_i:_i + 2]
    if "--dry-run" in args:
        _mode_override = "dry"; args = [a for a in args if a != "--dry-run"]
    if _mode_override in ("dry", "live"):
        RUN_MODE = _mode_override                 # rebinds the module globals; report() reads DRY_RUN at call time
        DRY_RUN = RUN_MODE != "live"
    os.environ["CONDUCTOR_RUN_MODE"] = RUN_MODE    # export so the detached --distribute-run mission inherits it
    if DRY_RUN:
        print("[conductor] DRY RUN — branch commit happens; Jira/Confluence/MR are logged, not sent "
              "(set --live or CONDUCTOR_RUN_MODE=live to send for real)")
    if args and args[0] == "--distribute":
        # Fire-and-forget "distribute": spawn a DETACHED conductor into its own mission/<slug>
        # bucket (orchestrator + workers tile together — the watchable mission view), then return.
        # Honors the caller's substrate (herdr → bucket; tmux → detached window). The detached
        # conductor self-loads .env for the DB; CONDUCTOR_MISSION_WS keeps its workers in-bucket,
        # CONDUCTOR_RUN_MODE carries the run mode across the seam (a dry distribute runs + LOGS, a
        # live one files for real — the old flag never crossed this seam, so a "dry" distributed
        # mission ran LIVE and filed real Jira tickets; that was the FC-1513..1519 leak).
        goal = " ".join(args[1:]).strip()
        if not goal:
            print('usage: conductor.py --distribute [--live] "<goal>"'); sys.exit(2)
        label = _mission_ws(goal, "adhoc")               # mission/<slug>, mid-independent
        name = "conductor-" + label.split("/", 1)[1]
        # base64 the goal so it survives the substrate seam's word-split (`-- $cmd`) intact —
        # goals routinely carry spaces/parens/quotes. The detached conductor decodes it.
        import base64
        g64 = base64.b64encode(goal.encode()).decode()
        inner = (f"env CONDUCTOR_MISSION_WS={label} CONDUCTOR_GOAL_B64={g64} CONDUCTOR_RUN_MODE={RUN_MODE} "
                 f"{PYEXE} {os.path.abspath(__file__)} --distribute-run")
        r = subprocess.run([SUBSTRATE, "spawn", name, _REPO_ROOT, inner, "--workspace", label],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[conductor] dispatch failed: {(r.stderr or r.stdout).strip()}"); sys.exit(1)
        _rep = "Jira/MR/Slack" if not DRY_RUN else "LOGGED only (dry-run — nothing sent)"
        print(f"[conductor] dispatched detached ({RUN_MODE}) → bucket {label} (agent {name}); "
              f"reports {_rep} on completion.")
        sys.exit(0)
    if args and args[0] == "--distribute-run":
        # Internal entry for the detached conductor: decode the goal from the env + run it.
        import base64
        _g = os.environ.get("CONDUCTOR_GOAL_B64", "")
        goal = (base64.b64decode(_g).decode() if _g else "").strip()
        if not goal:
            print("[conductor] --distribute-run: missing CONDUCTOR_GOAL_B64"); sys.exit(2)
        # A detached pane has a stripped env; route LLM traffic through the local proxy so the
        # mission is traced in Langfuse (fill-gap — a foreground conductor's setting is kept).
        os.environ.setdefault("ANTHROPIC_BASE_URL", "http://localhost:4000")
        # Register THIS detached pane in the fleet registry + tag it @orchestrator: it IS the
        # mission command post, so the reaper/peers/name-resolution must see it (register-always,
        # docs/herdr-workflow.md #8). Deregister in the finally — a headless pane has no pane-died
        # hook. Name matches the spawn-time herdr agent (conductor-<slug>).
        _mws = os.environ.get("CONDUCTOR_MISSION_WS", "")
        _oname = "conductor-" + _mws.split("/", 1)[1] if "/" in _mws else "conductor"
        _register_self(_oname, cwd=_REPO_ROOT, ws=_mws or None, orchestrator=True)
        try:
            rid, status = anyio.run(run_mission, goal)
        finally:
            _deregister_self()
        print(f"\nmission {rid}: {status}")
        # Completion notify (durable, rich): pull the outcome from the DB so a fire-and-forget
        # --distribute surfaces an ACTIONABLE summary on the Slack bus — verdict + MR + Jira links
        # — without anyone polling. Survives the session (PushNotification is the interactive job).
        try:
            _m = {}; _mr = None
            try:
                _cdb = Db()
                _m = _cdb.get_mission(rid) or {}
                _mr = next((e["payload"].get("url") for e in _cdb.list_events(rid)
                            if e["event_type"] == "mr" and (e["payload"] or {}).get("url")), None)
            finally:
                try: _cdb.close()
                except Exception: pass
            _v = _m.get("verdict") or {}
            _emoji = {"done": "✅", "escalated": "⚠️", "failed": "❌"}.get(status, "•")
            _repos = ", ".join(_m.get("repos") or []) or "—"
            _parts = [f"{_emoji} Conductor mission {rid[:8]} → {status} · {_title(goal)} · {_repos}"]
            if _v:
                _parts.append(f"verdict pass={_v.get('pass')} ({_v.get('recommendation', '')})")
            if _mr:
                _parts.append(f"MR: {_mr}")
            elif status == "done":
                _parts.append("no MR (no change needed)")
            if _m.get("jira_key"):
                _parts.append(f"Jira: {_m['jira_key']}")
            subprocess.run([os.path.expanduser("~/.tmux/agent-send.sh"), "--relay", "\n   ".join(_parts)],
                           timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        # Ephemeral mission bucket: on success, detach a helper that waits for THIS conductor
        # pane to exit, then closes the (now-idle) mission/<slug> workspace so it doesn't linger.
        # Non-done missions keep the bucket for post-mortem inspection.
        _ws = os.environ.get("CONDUCTOR_MISSION_WS")
        if _ws and status == "done":
            subprocess.Popen(["bash", "-c", f"sleep 4; '{SUBSTRATE}' workspace-close '{_ws}' >/dev/null 2>&1"],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(0)
    if args and args[0] == "--sdlc":
        # Drive the sdlc pipeline autonomously (scan.py-routed staged mission). S1 ships the
        # read-only --dry-run preview; staged execution lands in S3.
        goal = " ".join(args[1:]).strip()
        if not goal:
            print('usage: conductor.py --sdlc [--live|--dry-run] "<goal|ticket>"'); sys.exit(2)
        if DRY_RUN:
            sdlc_dry_run(goal); sys.exit(0)
        os.environ.setdefault("ANTHROPIC_BASE_URL", "http://localhost:4000")   # trace via proxy
        rid, status = anyio.run(run_sdlc_mission, goal)
        print(f"\nsdlc mission {rid}: {status}")
        sys.exit(0 if status in ("done",) else 1)
    if args and args[0] == "--resume":
        if len(args) < 2:
            print("usage: conductor.py --resume <mission_id>"); sys.exit(2)
        rid, status = anyio.run(resume_mission, args[1])
    else:
        goal = " ".join(args).strip()
        if not goal:
            print('usage: conductor.py "<goal>"  |  conductor.py --resume <mission_id>'); sys.exit(2)
        rid, status = anyio.run(run_mission, goal)
    print(f"\nmission {rid}: {status}")
