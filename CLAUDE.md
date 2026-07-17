# agents-nexus

## Project memory lives in the agent-memory MCP stack

This project's durable knowledge is stored in the **agent-memory MCP stack** (project `agents-nexus`), not in files. Use it liberally:

- **Before** starting any non-trivial task (debugging, infra changes, "where/why" questions), query the stack for prior context:
  - `mcp__agent-memory__search_similar` (project `agents-nexus`) with a natural-language description of the task — best for open-ended recall.
  - `mcp__agent-memory__query_entity` when you have a concrete name (a file path, service, unit, or flag) and want everything that references it.
- **After** reaching a durable, reusable finding — a decision, a non-obvious constraint, a fix whose root cause is worth remembering — write it with `mcp__agent-memory__create_note` (project `agents-nexus`). Include `links` to the files/services involved. Don't record what the code or git history already shows.
- **Do not** create file-based memory under `~/.claude/.../memory/` — that path is intentionally retired in favor of the stack.

Notes are point-in-time observations: if a recalled note cites a `file:line` or a flag, verify it against current code before asserting it as fact.
