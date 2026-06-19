---
description: Review nightly Bash allowlist candidates and add approved rules to settings.json
---

You are running the **manual approval step** for the nightly allowlist suggester
(`scripts/allowlist-suggest.py`). Nothing gets added without the user's explicit
say-so in this session.

## Steps

1. Read the latest candidates report:
   `~/.claude/allowlist-candidates/agents-nexus-latest.json`
   - If the file is missing or `candidates` is empty, tell the user there's
     nothing to review and stop.
   - Note its `date` and `window_hours` so the user knows how fresh it is.

2. Read the current project allowlist at `.claude/settings.json`
   (`permissions.allow`). Skip any candidate whose `rule` is already present
   (the script filters these, but double-check in case rules were added since
   the report ran).

3. Present the remaining candidates to the user for approval with
   **AskUserQuestion** (`multiSelect: true`). Show, for each candidate:
   `rule`, `count`, `sessions`, and the first entry from `samples` so they can
   see a real example of how it was used. If there are more than 4 candidates,
   ask in batches of 4 (AskUserQuestion allows at most 4 options per question),
   highest-count first.

4. Re-screen anything the user approves before writing it:
   - **Never** add destructive verbs even if approved by reflex —
     `git push`/`reset`/`clean`/`rebase`/`checkout`, `docker stop`/`rm`/`prune`,
     `rm`, `kill`/`pkill`, `chmod`/`chown`, `mv`, `terraform apply/destroy`.
     If the user explicitly insists on one, confirm a second time and explain
     the risk before adding.
   - Keep the rule string exactly as the report's `rule` field
     (`Bash(<prefix>:*)` form) for consistency with existing entries.

5. Write the approved rules into `.claude/settings.json`:
   - Append to `permissions.allow`, preserving existing entries and ordering
     (add new ones just before the trailing `mcp__*` entries to keep Bash rules
     grouped). Dedupe. Keep valid JSON and the file's 2-space indentation.

6. Update the report file so approved candidates aren't re-offered next time:
   remove approved entries from `candidates` in
   `agents-nexus-latest.json` (leave declined ones — they'll resurface and the
   user can keep declining, or you can note which were declined).

7. Summarize: list what was added, what was skipped/declined, and remind the
   user that the change is local to this repo's `.claude/settings.json` and can
   be reverted with `git checkout .claude/settings.json`.

## Notes
- This repo's committed `settings.json` is team-shared. If the user wants a rule
  to stay personal, offer to put it in `.claude/settings.local.json` instead.
- The analyzer already excludes already-granted and destructive commands, so the
  candidate list should be short and safe — your job is the human gate, not
  re-deriving the analysis.
