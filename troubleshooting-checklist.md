# Mini PC Smoke Test Checklist

## MCP Servers
- [ ] Agent memory write: run `/checkpoint` — verify it writes to `~/vault/Checkpoints/`
- [ ] Agent memory read: ask Claude "what do you remember about X?"

## Dashboard + Arbiter
- [x] Dashboard loads at `http://100.75.154.84:8421`
- [x] Green connector icons (db + mcp)
- [ ] Spawn an agent in tmux — verify it appears in the dashboard
- [ ] Agent status updates live as it progresses

## Langfuse Observability
- [ ] Open `http://100.75.154.84:3000` — Langfuse UI loads
- [ ] Trigger a memory operation — verify a trace appears
- [ ] Check trace details

## tmux Layer
- [x] SSH into mini PC
- [x] `work` attaches to agents session
- [ ] Launch agent with hotkey — opens in new pane/window
- [ ] Notifications work (bell to SSH client)
- [ ] Status bar shows agent count, key profile, APM

## Vault Sync (bidirectional)
- [ ] Mini PC: create test file in vault, `task nightly:vault-commit`, push
- [ ] Windows: `git pull` — file appears in Obsidian
- [ ] Windows: edit a note, commit + push
- [ ] Mini PC: `git pull` — change arrives

## End-to-end
- [ ] Run a real agent task with memory + dashboard + checkpoint
