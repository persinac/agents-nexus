# Spec: `best-effort` triage on mission exhaustion (lean version)

**Status:** proposal · **Author:** conductor session 2026-07-21 · **Repo:** agents-nexus (`agent-runner/conductor.py`)

## Problem

A `building` mission today has a **binary** terminal outcome:

- verify passes → `finalize` synthesizes → `report` opens the MR + files the Jira tracking Task → `done`.
- verify never passes within `MAX_REPLANS` → `run_and_verify` returns `(verdict, False)` → `finalize` marks the
  mission `escalated` and **stops** (`conductor.py:1368-1372`). The committed worktree branch is stranded: no MR,
  no ticket, no route for a human to pick it up. That is exactly what happened to FC-1395 (`b5e020a8`) last night —
  ~70% of the work sat in a worktree with the reviewer findings buried in a DB `verdict` row.

Meanwhile a passed mission ships everything at once; a human still reviews the MR. So the internal 5-reviewer panel
grinding all 5 replans is partly redundant with the review that happens anyway — over-investment for a P3.

## Goal (lean)

When a mission **exhausts its replans**, don't dead-end. Instead:

1. **Commit + open a DRAFT MR** for the best-attempt branch (never non-draft — the code failed verification).
2. **File the residual reviewer findings as Jira tickets** in the Claude Queue (epic FC-1239), so "fix it later" is
   *real* (an actionable queue item) instead of stranded (a DB row nobody reads).
3. Mark the mission a distinct terminal status (`partial`) so it's visibly different from a clean `done` and from a
   silent `escalated`.

This is the **annotate** version: the MR carries the triage; the human reviewing the draft MR *is* the router for the
iffy findings. We are NOT (yet) building an autonomous fix-fan-out lane — see "Explicitly out of scope."

The honesty guardrail is the whole point: escalation must flip from "nothing ships, work stranded" to "work preserved
+ gaps enumerated as a to-do list," and it must never emit a false *ready* signal (hence **draft** MR, and a `partial`
status, not `done`).

## Non-goals / explicitly out of scope

- **No autonomous G2G fix lane.** The ambitious version (classify findings → cluster coupled ones → parallel-fix the
  mechanical class → re-verify) is a separate, later proposal. Observed mechanical-finding ratio is only ~2/5 on real
  missions; not yet worth the extra opus judgment step + clustering + second verify pass. Revisit after ~a dozen
  missions of data.
- **No change to the pass path.** A mission that passes verification behaves exactly as today.
- **No new reviewer model calls.** We reuse the verdict findings already produced by `verify_mission`.

## Design

### D1. New config knob (mirrors the existing plan-gate `on_exhausted`)

`conductor.garner.yaml` `policy:` block:

```yaml
policy:
  on_exhausted: partial   # partial | escalate  (default: escalate — byte-for-byte current behavior)
```

Read near the other policy constants (`conductor.py:45` area):

```python
ON_EXHAUSTED = os.environ.get("CONDUCTOR_ON_EXHAUSTED", POLICY.get("on_exhausted", "escalate"))
```

Default `escalate` keeps today's behavior for anyone who doesn't opt in. Set `partial` in the Garner work config.

### D2. `finalize` branches on exhaustion (`conductor.py:1364-1372`)

Current:

```python
verdict, ok = await run_and_verify(db, mid, goal, start_round=start_round)
subs = db.list_subtasks(mid)
if not ok:
    db.finish_mission(mid, "escalated")
    db.log_event(mid, "escalated", {...})
    return mid, "escalated"
```

New — when `not ok` and `ON_EXHAUSTED == "partial"`, fall through to a best-effort report instead of dead-ending:

```python
verdict, ok = await run_and_verify(db, mid, goal, start_round=start_round)
subs = db.list_subtasks(mid)
if not ok:
    if ON_EXHAUSTED != "partial":
        db.finish_mission(mid, "escalated")
        db.log_event(mid, "escalated", {"verdict": verdict, "replans": ...})
        return mid, "escalated"
    # best-effort: preserve the work + enumerate the gaps
    db.update_mission(mid, status="synthesizing")
    art = await _safe_synthesize(goal, subs, verdict)   # reuse the finalize() try/except fallback
    targets = await report(db, mid, goal, art, subs, verdict, draft=True, triage=True)
    db.finish_mission(mid, "partial")
    db.log_event(mid, "partial", {"verdict": verdict, "targets": targets})
    return mid, "partial"
# ... unchanged pass path ...
```

(Factor the `synthesize()` try/except I already added in the crash-fix PR into `_safe_synthesize` so both the pass
path and this path share it.)

### D3. `report(..., draft=False, triage=False)` — two new flags

`report` already does the right sub-steps; we parameterize two:

- **`draft`**: thread into `_open_mr`. `_open_mr` already honors `REPORTING.mr.draft` (`conductor.py:1233`); add a
  per-call override so a partial mission forces `--draft` regardless of config:

  ```python
  def _open_mr(b, title, description, draft=False):
      ...
      if draft or REPORTING.get("mr", {}).get("draft"):
          args.append("--draft")
  ```

  Also prefix the MR title with `[DRAFT/partial]` and prepend a banner to the description:
  `> ⚠️ This MR did not pass Conductor verification (exhausted N replans). It is a preserved best-effort with the
  open findings tracked below. Do not merge as-is.`

- **`triage`**: after the tracking Task is filed, file the **residual findings** as Claude-Queue tickets (D4).

The existing tracking-Task creation stays — but when `triage=True`, its description links to the child finding tickets
and it should itself carry the `[partial]` marker.

### D4. Findings → Jira, via the `queue-techdebt` shape

The verdict's `findings` list is the work-breakdown, already structured:
`{severity, lens, what, where, (fix_hint)}` (`conductor.py:861`, `verify_mission`).

New helper `_file_triage_tickets(db, mid, goal, verdict, src, tracking_key)`:

1. **Filter + dedupe.** Keep `severity in {blocker, major}` (drop `minor`/advisory noise — a queue full of nits is
   worse than no queue, per the queue-techdebt skill's core principle). Dedupe by normalized `where` (file:line or
   function) so coupled findings from multiple lenses collapse to one ticket — e.g. this FC-1395 run's
   "add APITimeoutError handler" and "chat() over the complexity baseline" are the **same function**, one ticket.
   Cap at N (e.g. 6) tickets; if more survive, roll the remainder into a single "N more findings" ticket with the list
   in the body (never silently drop — `log_event("triage_capped", {...})`).

2. **File each via the queue-techdebt contract** (see `~/.claude/skills/queue-techdebt/references/jira-fields.md`).
   Reuse `reporter_agent(..., ["atlassian"])` — do NOT re-implement Atlassian calls. Per-ticket:
   - `project = FC`, `issueTypeName = Task`, `parent = FC-1239` (the Claude Queue epic — this is the key routing
     decision: triage findings go to the **tech-debt queue**, not under the mission's own source epic),
     `assignee = REPORTING.jira.assignee`.
   - **Two-call gotcha:** the Team field (`customfield_10001 = "bee517c7-f0ef-499f-83ff-5a0ff5446959"`) is *silently
     dropped on create* — the reporter must create first, then `editJiraIssue` to set Team. Encode this in the
     reporter prompt exactly as the skill documents (verified on FC-1244).
   - Summary: `[<lens>] <one-line what>` (≤110 chars). Body: the skill's standard sections —
     `### Problem` (the finding `what`), `### Where` (`where`), `### Fix` (`fix_hint` if present), `### References`
     (link the tracking Task, the draft MR URL, and the source ticket `src`).
   - **False-positive gate:** carry over the skill's hard-coded rejects (the py3.14 `except A, B:` SyntaxError class,
     and "any finding whose fix the repo's own formatter/linter would revert"). Skip filing those; `log_event`
     ("triage_rejected", {...}) with the reason. Cheap substring/heuristic match is fine here — this is a filter, not
     a full verify.

3. `log_event(mid, "triaged", {"tickets": [keys], "rejected": n, "capped": n})` and return the keys so the tracking
   Task + MR banner can link them.

### D5. Terminal status + reporting

- New mission status `partial` (terminal). Update any status-category mapping / dashboard enum that special-cases
  `done|failed|escalated` (grep for those literals — e.g. Trello move in `run_mission`/`resume_mission:1422,1461`
  maps non-`done` → "failed"; add `partial` → a distinct column or reuse "in review").
- `_slack_relay` a one-liner on partial, consistent with the plan-gate stop relay (`conductor.py:1397`):
  `🟡 Conductor <mid8> PARTIAL · <title> · draft MR <url> · N findings queued (FC-…, FC-…)`.

## Dry-run behavior

`DRY_RUN` must still short-circuit all live calls: draft MR → `mr_dryrun`, triage tickets → a `triage_dryrun` event
logging the *would-file* payloads (summary + parent + severity) instead of creating them. Mirrors the existing
`jira_dryrun`/`confluence_dryrun` pattern so a first partial run on a new box is safe to inspect.

## Testing (same harness as the crash-fix PR — stub the SDK/subprocess boundary)

1. **Exhaust → partial, not escalated.** `run_and_verify` returns `(verdict, False)` with 3 blocker findings +
   `ON_EXHAUSTED="partial"` → `report(draft=True, triage=True)` called, mission finishes `partial`, draft MR opened
   (`_open_mr` received `draft=True`), tracking Task + N finding tickets filed. Assert `triaged` event has the keys.
2. **Default stays escalate.** Same input with `ON_EXHAUSTED` unset → old behavior exactly (`escalated`, no report).
3. **Dedupe + cap.** 8 findings across 5 lenses, 3 sharing one `where` → collapses to the coupled ticket + others,
   capped at 6, remainder rolled into one "N more" ticket; nothing silently dropped (assert `triage_capped`).
4. **False-positive gate.** A `except A, B:` SyntaxError finding is skipped with `triage_rejected`; a real finding is
   filed.
5. **Pass path untouched.** A passing verdict still → non-draft MR, `done`, no triage tickets.
6. **Dry-run.** `DRY_RUN=1` → `triage_dryrun`/`mr_dryrun` events, zero live Atlassian/glab calls.

## Rollout

- Land behind `on_exhausted: escalate` default (no behavior change until Garner config opts into `partial`).
- Flip `conductor.garner.yaml` → `partial` after tests green.
- One live shakedown on a deliberately-hard ticket; confirm the draft MR + queue tickets look right, then leave on.

## Resolved decisions (confirmed 2026-07-21)

1. **Draft MR on partial** — ✅ confirmed. Push the branch and open a **draft** MR (reviewable artifact, no false
   "ready" signal). Not push-branch-only.
2. **Severity floor `blocker`+`major`** — ✅ confirmed as the starting point. Drop `minor`/advisory from triage
   filing. Revisit the floor once we have a few partial missions of data (may tighten to blockers-only if `major`
   proves noisy).
3. **Triage → Claude Queue (FC-1239)** — ✅ confirmed. Route all triage findings to the tech-debt queue epic
   regardless of the mission's source epic (these are follow-up/offshoot work, matching `/queue-techdebt`). The
   mission's own tracking Task keeps its normal epic behavior; only the child finding tickets go to FC-1239.
