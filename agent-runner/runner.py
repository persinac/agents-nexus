#!/usr/bin/env python3
"""agent-runner (cut 1) — hybrid Claude Agent SDK agent.

Unifies the three spike validations into the real loop:
  - V1: routes through the litellm/nexus proxy (per-agent /sess/<name>) + Langfuse.
  - V2: idle-gated delivery — a per-agent INBOX file (the bus/bridge writes it) is
        merged with your keyboard input; both are consumed only at a turn boundary.
  - V3: can_use_tool permission gate — read-only auto-allowed, mutating ops paused
        for a y/n in the pane (fail-safe deny on timeout).

Hybrid: this runs INSIDE a tmux pane and renders its stream, so `tmux attach` still
works and it self-registers in ~/.tmux/registry (so the dashboard/peers still see it).
Memory (mnemon) MCP is passed explicitly — hermetic (setting_sources=[]),
so no settings.json env clobber and no legacy shell hooks.

Usage:
  runner.py [--name NAME] [--model MODEL] [--cwd DIR] [--all-mcp] [--exit-on-idle]
Deliver into it from anywhere:
  echo 'your message' >> ~/.tmux/sdk-inbox/<name>.inbox
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

REPO = os.environ.get("AGENTS_NEXUS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
REGISTRY_DIR = Path(HOME, ".tmux", "registry")
INBOX_DIR = Path(HOME, ".tmux", "sdk-inbox")
PROXY = "http://localhost:4000"

READONLY_BASH = ("echo", "ls", "cat", "pwd", "git status", "git log", "git diff",
                 "git branch", "grep", "rg", "head", "tail", "wc", "date", "which",
                 "env", "find", "tree", "cd")


# ── permission gate — delegates to the real litellm notify-classify ─────────────
# Reuses the fleet's classifier (deterministic read/deny allowlists + a Haiku call
# for ambiguous Bash / writes) via its own venv, so the SDK gate agrees with what
# the tmux CLI agents do. Runs out-of-process (litellm stays out of this venv).
CLASSIFY_PY = os.path.join(HOME, ".tmux", ".classify-venv", "bin", "python")
CLASSIFY_SCRIPT = os.path.join(HOME, ".tmux", "notify-classify.py")
# Trusted local MCP servers + internal tools auto-allow, mirroring the fleet's
# settings.json permissions.allow (the CLI never prompts for these).
AUTO_ALLOW_MCP = ("mcp__agent-memory__",)
AUTO_ALLOW_EXACT = frozenset({"TodoWrite"})


def _heuristic_classify(name: str, inp: dict):
    """Fallback (decision, category, summary) if the classifier venv/script is absent."""
    inp = inp or {}
    if name in ("Read", "Grep", "Glob") or name.startswith("mcp__"):
        return "read", "read-only", ""
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        if any(cmd == p or cmd.startswith(p + " ") for p in READONLY_BASH):
            return "read", "read-only", cmd
        return "modify", "shell command", cmd
    return "modify", "change", (inp.get("file_path") or "")


async def classify_tool(name: str, inp: dict):
    """Ask the real litellm notify-classify whether this tool is read-only
    (auto-allow) or needs a human. Returns (decision, category, summary). Fails safe
    to the heuristic on any error/timeout."""
    if name in AUTO_ALLOW_EXACT or name.startswith(AUTO_ALLOW_MCP):
        return "read", "trusted", ""
    if not (os.path.exists(CLASSIFY_PY) and os.path.exists(CLASSIFY_SCRIPT)):
        return _heuristic_classify(name, inp)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            CLASSIFY_PY, CLASSIFY_SCRIPT, "--tool",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        payload = json.dumps({"name": name, "input": inp or {}}).encode()
        out, _ = await asyncio.wait_for(proc.communicate(payload), timeout=20)
        obj = json.loads(out.decode().strip())
        return (obj.get("decision") or "modify"), (obj.get("category") or "change"), (obj.get("summary") or "")
    except Exception:
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        return _heuristic_classify(name, inp)


def load_mcp_servers(all_mcp: bool) -> dict:
    """agent-memory (+ optionally atlassian/datadog/…) from ~/.claude.json; project servers from .mcp.json."""
    keep = None if all_mcp else {"agent-memory"}
    servers = {}
    try:
        user = json.load(open(os.path.join(HOME, ".claude.json"))).get("mcpServers", {}) or {}
        for n, c in user.items():
            if keep is None or n in keep:
                servers[n] = c
    except Exception as e:
        print(f"[runner] warn: could not read ~/.claude.json mcpServers: {e}")
    try:
        proj = json.load(open(os.path.join(REPO, ".mcp.json"))).get("mcpServers", {}) or {}
        servers.update(proj)              # any project servers
    except Exception:
        pass
    return servers


class InputHub:
    """Merges keyboard (stdin) + inbox file into one queue, with a side-channel for
    y/n approval answers so a mid-turn approve prompt doesn't get eaten as a message."""
    def __init__(self, loop):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()
        self.stdin_closed = False
        self._approval = None
        self._stop = False

    def _submit(self, source: str, line: str):
        line = line.rstrip("\n")
        if not line.strip():
            return
        if source == "stdin" and self._approval is not None and not self._approval.done():
            self.loop.call_soon_threadsafe(self._approval.set_result, line)
            return
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (source, line))

    def _submit_inbox(self, raw: str):
        """Inbox lines are framed JSON records ({"from","text","ts"}) from the bus,
        but a bare `echo 'hi' >> inbox` (plain text) is accepted too. Never answers
        an approval prompt — only the keyboard (stdin) does that."""
        raw = raw.strip()
        if not raw:
            return
        sender, text = None, raw
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "text" in obj:
                text, sender = str(obj.get("text") or ""), obj.get("from")
        except Exception:
            pass
        if not text.strip():
            return
        prompt = f"[bus message from {sender}]\n{text}" if sender else text
        print(f"  📨 inbox: buffered message from {sender or '?'} (delivers at next turn boundary)", flush=True)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (f"inbox·{sender}" if sender else "inbox", prompt))

    def start_stdin(self):
        def run():
            for line in sys.stdin:
                self._submit("stdin", line)
            self.stdin_closed = True
            self.loop.call_soon_threadsafe(self.queue.put_nowait, ("__eof__", ""))
        threading.Thread(target=run, daemon=True).start()

    def start_inbox(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

        def run():
            pos = path.stat().st_size          # only NEW lines after launch
            while not self._stop:
                try:
                    size = path.stat().st_size
                    if size > pos:
                        with open(path) as f:
                            f.seek(pos)
                            chunk = f.read()
                            pos = f.tell()
                        for line in chunk.splitlines():
                            self._submit_inbox(line)
                except FileNotFoundError:
                    pass
                time.sleep(0.3)
        threading.Thread(target=run, daemon=True).start()

    async def ask_human(self, prompt: str, timeout: float):
        fut = self.loop.create_future()
        self._approval = fut
        print(prompt, flush=True)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._approval = None


SUBSTRATE = os.path.expanduser("~/.tmux/substrate.sh")   # tmux<->herdr seam (NEXUS_SUBSTRATE)


def register(name: str, cwd: str, inbox: Path):
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return None
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    f = REGISTRY_DIR / pane
    slot = os.popen(f"{SUBSTRATE} pane-field '{pane}' '#{{window_index}}' 2>/dev/null").read().strip()
    # INBOX= is the load-bearing field: agent-send.sh routes bus delivery here
    # (append a framed record) instead of tmux send-keys when it's present.
    workspace = os.environ.get("NEXUS_WORKSPACE", "")
    substrate = os.environ.get("NEXUS_SUBSTRATE", "herdr")  # default herdr; NEXUS_SUBSTRATE=tmux for the legacy fallback
    f.write_text(f"SLOT={slot}\nNAME={name}\nCWD={cwd}\nAT={int(time.time())}\n"
                 f"PANE_ID={pane}\nRUNTIME=sdk\nINBOX={inbox}\nWORKSPACE={workspace}\nSUBSTRATE={substrate}\n")
    # rename verb = rename-window + automatic-rename off, through the substrate seam.
    os.system(f"{SUBSTRATE} rename '{pane}' '{name}' 2>/dev/null")
    return f


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=os.environ.get("PROJECT_SLUG") or Path(os.getcwd()).name)
    ap.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "claude-opus-4-8"))
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--all-mcp", action="store_true", help="load all user MCP servers, not just memory")
    ap.add_argument("--approval-timeout", type=float, default=120.0)
    ap.add_argument("--exit-on-idle", action="store_true", help="exit when stdin closes and nothing is queued (for tests)")
    args = ap.parse_args()

    slug = "".join(c if c.isalnum() or c in "._-" else "-" for c in args.name)

    # V1: route through the proxy when it's up (mirrors env.sh), else go direct.
    if os.popen(f"curl -sf -m 0.4 {PROXY}/health/liveliness >/dev/null 2>&1 && echo up").read().strip() == "up":
        os.environ["ANTHROPIC_BASE_URL"] = f"{PROXY}/sess/{slug}"
    proxied = os.environ.get("ANTHROPIC_BASE_URL", "(direct)")

    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions,
        PermissionResultAllow, PermissionResultDeny,
        AssistantMessage, UserMessage, ResultMessage,
        TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
    )

    loop = asyncio.get_running_loop()
    hub = InputHub(loop)
    inbox_path = INBOX_DIR / f"{slug}.inbox"

    async def can_use_tool(name, inp, ctx):
        decision, category, summary = await classify_tool(name, inp)
        if decision == "read":
            return PermissionResultAllow()
        detail = summary or (inp.get("command") or inp.get("file_path") or "")
        prompt = (f"\n  ⚠️  APPROVE  [{category}]  {name}\n"
                  f"     {detail}\n"
                  f"     [y/N]  (Enter or {int(args.approval_timeout)}s timeout = deny) → ")
        ans = await hub.ask_human(prompt, args.approval_timeout)
        if (ans or "").strip().lower() in ("y", "yes"):
            print("  ✓ approved")
            return PermissionResultAllow()
        print("  ✗ denied" + (" (timeout fail-safe)" if ans is None else ""))
        return PermissionResultDeny(message="denied by operator")

    # Preserve CLAUDE.md + identity by appending to the default Claude Code prompt.
    claude_md = ""
    try:
        claude_md = open(os.path.join(REPO, "CLAUDE.md")).read()
    except Exception:
        pass
    append = (f"You are '{args.name}', an agent in the Nexus fleet, running under the "
              f"Claude Agent SDK runner (hybrid). Working dir: {args.cwd}.\n\n"
              f"Project instructions (CLAUDE.md):\n{claude_md}")

    mcp = load_mcp_servers(args.all_mcp)
    options = ClaudeAgentOptions(
        model=args.model,
        cwd=args.cwd,
        setting_sources=[],                         # hermetic: no clobber, no legacy hooks
        mcp_servers=mcp,
        permission_mode="default",                  # route decisions to can_use_tool
        can_use_tool=can_use_tool,
        system_prompt={"type": "preset", "preset": "claude_code", "append": append},
    )

    reg = register(args.name, args.cwd, inbox_path)
    hub.start_stdin()
    hub.start_inbox(inbox_path)

    print(f"┌─ agent-runner  name={args.name}  model={args.model}")
    print(f"│  proxy={proxied}")
    print(f"│  mcp={list(mcp)}")
    print(f"│  inbox={inbox_path}")
    print(f"└─ type a message and Enter, or:  echo 'hi' >> {inbox_path}\n")

    async def drain(client):
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ThinkingBlock):
                        print("  · (thinking)")
                    elif isinstance(b, TextBlock) and b.text.strip():
                        print(f"\n{args.name}: {b.text.rstrip()}\n")
                    elif isinstance(b, ToolUseBlock):
                        d = b.input.get("command") if b.name == "Bash" else b.input
                        print(f"  → {b.name}({d})")
            elif isinstance(msg, UserMessage):
                for b in getattr(msg, "content", []) or []:
                    if isinstance(b, ToolResultBlock):
                        t = b.content if isinstance(b.content, str) else str(b.content)
                        flag = " [error]" if getattr(b, "is_error", False) else ""
                        print(f"  ← {t.strip()[:100]}{flag}")
            elif isinstance(msg, ResultMessage):
                print(f"  [turn done · {msg.subtype} · ${getattr(msg,'total_cost_usd',0):.4f}]")
                return

    rc = 0
    try:
        async with ClaudeSDKClient(options=options) as client:
            while True:
                source, line = await hub.queue.get()
                if source == "__eof__":
                    if args.exit_on_idle and hub.queue.empty():
                        break
                    continue
                print(f"[{source}] ▷ {line}")
                await client.query(line)
                await drain(client)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as e:
        print(f"[runner] fatal: {type(e).__name__}: {e}")
        rc = 1
    finally:
        hub._stop = True
        if reg and reg.exists():
            reg.unlink()
    return rc


if __name__ == "__main__":
    import anyio
    sys.exit(anyio.run(main))
