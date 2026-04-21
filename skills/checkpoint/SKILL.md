---
name: checkpoint
description: Save the current conversation context as a checkpoint note to ~/garner/notes/. Invoke when the user wants to save progress, bookmark a session, or preserve context before switching tasks.
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash, Write, Read, Glob, mcp__agent-memory__create_note, mcp__agent-memory__log_event
argument-hint: "[optional label]"
---

# Save Conversation Checkpoint

Save a structured summary of the current conversation to `~/garner/notes/`.

## Variables

- **Date:** !`date -u +"%Y-%m-%d"`
- **Project:** !`basename "$(git rev-parse --show-toplevel 2>/dev/null || basename "$PWD")"`
- **Branch:** !`git branch --show-current 2>/dev/null || echo "n/a"`
- **Git status (staged + unstaged):** !`git diff --stat HEAD 2>/dev/null | tail -1`
- **Label:** $ARGUMENTS

## Instructions

1. Derive the filename as `~/garner/notes/<YYYY-MM-DD>-<project>-checkpoint.md` using the date and project name above.

2. Check if the file already exists using the Read tool.

3. Write a checkpoint entry with this structure:

```markdown
---

## Checkpoint — <HH:MM UTC> <label if provided>

**Branch:** <branch>
**Changes:** <git diff stat summary>

### Context
<2-4 sentence summary of what this conversation covered — decisions made, problems investigated, and current state>

### Key Changes
<bulleted list of files modified and what changed in each, based on what you did in this conversation>

### Open Items
<bulleted list of anything unfinished, blocked, or flagged for follow-up>
```

4. If the file does NOT exist, create it with a header first:

```markdown
# Checkpoint Notes — <YYYY-MM-DD> — <project>

<then the checkpoint entry>
```

5. If the file DOES exist, append the new checkpoint entry to the bottom of the existing file.

6. Write the same checkpoint content to the agent-memory system:

   a. Call `mcp__agent-memory__create_note` with:
      - `title`: "Checkpoint — <YYYY-MM-DD> <HH:MM UTC> <label if provided>"
      - `content`: the full checkpoint entry markdown (same text written to the flat file)
      - `project`: the project name
      - `tags`: `["checkpoint"]` plus any domain tags that fit (e.g. `"infra"`, `"backend"`, `"deployment"`) based on what the session covered
      - `links`: `[<project name>, <branch name>]`

   b. Call `mcp__agent-memory__log_event` with:
      - `event_type`: `"checkpoint"`
      - `project`: the project name
      - `repo`: the project name
      - `branch`: the current branch
      - `details`: `{ "label": "<label or empty string>", "file": "<flat file path>", "changes": "<git diff stat summary>" }`

7. After writing, confirm with the file path and a one-line summary of what was captured.
