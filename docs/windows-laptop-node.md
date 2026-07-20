# Windows laptop → second nexus node (thin-first, grow-to-full)

> Draft runbook (uncommitted). Goal: stand up the Windows laptop as node #2,
> **thin first** (prove bus federation with minimal moving parts), then **grow to
> a full peer node** (own stack, redundant to a nexus crash).
>
> Verified against the live nexus box 2026-07-20:
> - Bus backbone is **Slack Socket-Mode fan-out** — bridges never connect to each
>   other, only to Slack. Federation works on-LAN, off-LAN, behind a firewall.
> - Nexus bridge already runs `SLACK_PRESENCE_ENABLED=1`,
>   `SLACK_PRESENCE_HOST=alex-nexus` (systemd --user `slack-bridge.service`). **No
>   nexus-side change needed.** It's "solo" only until a peer bridge appears.
> - Slack tokens are fetched at launch by
>   `scripts/secrets/secret-run.sh --project nexus --config prd …` → Doppler.
> - Nexus IPs: LAN `192.168.4.94`, Tailscale `100.75.154.84`. Shared services:
>   spark SSE `:8343`, agent-memory (mnemon) over SSH, proxy `:4000`, dashboard `:8421`.

Naming: nexus = `alex-nexus`. Use **`alex-laptop`** for the laptop
(`SLACK_PRESENCE_HOST`). Keep agent names host-unique, or address the specific one
as `alex-nexus/<name>` vs `alex-laptop/<name>` (bare names go to the presence-elected owner).

---

## What "thin" means here (important)

Thin is **NOT** the Phase-10 "SSH into nexus and run agents in its tmux" client — that's
one bridge remote-controlled, i.e. no federation. Thin here = the laptop runs **its own
bridge with presence on**, plus one registered agent, but **borrows nexus's MCP + model**
instead of replicating the docker stack. That's the smallest thing that actually proves
two bridges federating.

| Concern        | Thin phase                                   | Full phase                          |
|----------------|----------------------------------------------|-------------------------------------|
| Slack bridge   | **Local** (Node, presence on)                | Local (unchanged)                   |
| Agents/substrate | 1+ Claude Code session (sessionstart hook) | Full herdr/tmux + hook set          |
| Search (spark) | Point at nexus `:8343` over Tailscale        | Own local spark container           |
| Memory (mnemon)| Point at nexus over SSH                       | Own local mnemon                    |
| Model traffic  | Laptop's own Anthropic login, **direct**     | Own local proxy (`:4000`) + Langfuse|
| Docker stack   | none                                          | `task docker:up`                    |
| File handoff   | git branches (see below)                      | git branches (same)                 |

---

## Prereqs (laptop, one-time)

1. **Tailscale** — `tailscale up`; confirm it can reach nexus:
   `ping 100.75.154.84` and `curl http://100.75.154.84:8343/webhook/status`.
2. **Deps** — Node (fnm or winget), Git, Docker Desktop (WSL2 backend; needed only
   for the full phase), Claude Code (`npm i -g @anthropic-ai/claude-code`; run once to
   log in). `tmux/windows/install-winget.ps1` bootstraps most of this.
3. **msys64** — the bash tooling (`agent-send.sh`, `agent-registry.sh`, hooks) runs under
   msys64, as on the old gaming-PC node. The **bridge (Node) and Docker do not need it.**
   Repo path convention on Windows: `C:/projects` ↔ `/c/projects` under msys64.
4. **Doppler CLI** — `doppler login`, or drop a **service token** for project `nexus`
   config `prd` so `secret-run.sh` can fetch the same Slack bot/app token + channel the
   nexus bridge uses. This is what puts both bridges on the *same* bus.
5. **Clone** — `git clone <origin> /c/projects/agents-nexus` (or wherever `REPOS_PATH` points).

---

## Thin phase — prove federation

### 1. Start the laptop bridge with presence

From `slack-bridge/` on the laptop (msys64 or PowerShell with env set):

```bash
SLACK_PRESENCE_ENABLED=1 SLACK_PRESENCE_HOST=alex-laptop \
  ../scripts/secrets/secret-run.sh --project nexus --config prd \
  SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_NEXUS_CHANNEL SLACK_AGENTS_CHANNEL SLACK_BUS_ENABLED \
  -- node index.js
```

Expect in its log: `presence registry ENABLED (host=alex-laptop …)`. The bridge only
needs **outbound to Slack** — it does not connect to nexus at all.

### 2. Register one agent

Open a Claude Code session on the laptop under the substrate so `hook-sessionstart.sh`
writes a registry entry (`~/.tmux/registry/…`) and presence advertises it. (A bare
`herdr agent start` or a hooked tmux pane both work.)

### 3. Validate cross-host delivery (the actual test)

- Laptop bridge: `curl http://127.0.0.1:8788/agents` → should now list **both** hosts
  (`alex-nexus` + `alex-laptop`), not just self.
- **From nexus:** `agent-send.sh alex-laptop/<name> "ping from nexus"` → lands in the
  laptop agent.
- **From laptop:** `agent-send.sh alex-nexus/general "pong from laptop"` → lands on nexus.
- Negative check: a bare duplicate name delivers to the presence-elected owner only —
  use `host/name` to hit a specific instance.

### 4. Share nexus's brain (so laptop agents aren't blind)

Laptop `~/.claude.json` (from mini-pc-setup Phase 10):

```json
{ "mcpServers": {
  "spark": { "type": "sse", "url": "http://100.75.154.84:8343/sse" },
  "agent-memory": { "type": "stdio", "command": "ssh",
    "args": ["nexus", "/home/persinac/repos/agents-nexus/mnemon/.venv/bin/python3",
             "-m", "agent_memory.server.mcp_server"] }
}}
```

Model traffic in thin phase: **leave it direct to Anthropic** on the laptop's own login.
Don't route through nexus's `:4000` yet — that just adds a nexus dependency you remove
again in the full phase.

**Exit criteria for thin:** messages flow both directions by `host/name`, and laptop
agents can `search_similar` / `spark` against the shared fleet. Federation proven.

---

## Grow to full — independent peer node

Do these once thin is proven; each is independent.

1. **Local stack:** `task docker:up` (Docker Desktop) → own ollama + spark + mnemon +
   dashboard. Repoint the laptop's MCP config from nexus IPs to `localhost`. Consider
   skipping the heavy Langfuse profile at first — the laptop chassis is smaller.
2. **Own proxy + tracing:** bring up the `nexus-proxy` container locally so model traffic
   is traced to the laptop's Langfuse and the laptop keeps working when nexus is down.
   (The proxy is a transparent pass-through — it forwards the client's own Anthropic auth,
   so no token-sharing; personal sessions go straight to Anthropic.)
3. **Full substrate + hooks:** `tmux/windows/install.sh` (msys64) installs the hook set,
   `agent-send.sh`, registry, bashrc functions.
4. **Autostart (the real Windows chore):** there's no systemd. Supervise the bridge +
   stack with **Task Scheduler** or **NSSM** (mac uses launchd, linux systemd — Windows
   needs its own). This is the roughest edge on Windows; budget time here.
5. **Cross-host file handoff — switch off direct FS writes.** Same-host agents hand off by
   writing into each other's `~/repos` tree; that's impossible across machines. Use **git
   remotes**: each host owns distinct repos/worktrees, agents hand off by pushing a
   work-branch and pinging the peer over the bus to fetch it. Do **not** Syncthing/NFS the
   raw `.git`. (minio/S3 at `:9000` is the fallback for non-git artifacts.)

---

## Windows-specific gotchas

- **No systemd** → autostart/supervision is bespoke (Task Scheduler / NSSM). Biggest chore.
- **bash tooling needs msys64**; Node bridge + Docker don't. Keep the two straight.
- **Path translation** `C:/…` ↔ `/c/…` — the repo's `tmux/windows` env already handles it;
  set `REPOS_PATH` / `HOST_TMUX_DIR` per the mini-pc-setup Phase-3 table.
- **Docker Desktop resource caps** (WSL2) — the full stack is heavy for a laptop; start
  with the thin service set (ollama+spark+mnemon), add Langfuse only if you want traces.
- **Name collisions** across hosts — rely on `alex-laptop/…` vs `alex-nexus/…`.

---

## What I can prep from nexus vs what only you can do on the laptop

- **Nexus side (me):** nothing required — presence already on. I can optionally set up the
  git-remote handoff scaffolding and confirm the tailnet reaches the shared MCP ports.
- **Laptop side (you):** everything above — I don't have shell on the laptop. Ping me and
  I'll walk the validation step live over the bus once your laptop bridge is up.
