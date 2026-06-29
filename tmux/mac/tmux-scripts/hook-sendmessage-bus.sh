#!/usr/bin/env python3
"""PreToolUse hook (matcher: SendMessage) — bus-by-default for peer messaging.

The built-in SendMessage tool only reaches agents the current orchestrator spawned;
it cannot reach PEER agents in other tmux windows/hosts. This hook intercepts
SendMessage calls and, when `to` is a registered peer, delivers the message through
the fleet Slack bus (agent-send.sh --via-slack, local fallback) and DENIES the native
call so the model treats it as sent. For non-peer targets (main, spawned subagents,
agentIds, unknown names) it stays out of the way and lets the native tool run.

Contract: emit ONLY the decision JSON on stdout, exit 0. Any error / non-peer / parse
failure -> exit 0 with no stdout (allow native). Never break messaging.
"""
import json
import os
import subprocess
import sys
import glob

HOME = os.path.expanduser("~")
AGENT_SEND = os.path.join(HOME, ".tmux", "agent-send.sh")
REGISTRY_GLOB = os.path.join(HOME, ".tmux", "registry", "*")
LOG = os.path.join(HOME, ".tmux", "sendmessage-bus.log")


def allow():
    # No stdout -> native SendMessage proceeds unchanged.
    sys.exit(0)


def deny(reason: str):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def log(line: str):
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_peer(name: str) -> bool:
    """True if `name` is a live agent in this host's tmux registry."""
    if not name or name == "main":
        return False
    target = "NAME=" + name
    for path in glob.glob(REGISTRY_GLOB):
        try:
            with open(path) as f:
                for raw in f:
                    if raw.rstrip("\n") == target:
                        return True
        except Exception:
            continue
    return False


def send(mode_flag, to, msg):
    """Run agent-send.sh; return (rc, combined_output). Output never hits our stdout."""
    if os.environ.get("BUS_HOOK_DRYRUN") == "1":
        return 0, "dryrun %s -> %s" % (mode_flag, to)
    cmd = [AGENT_SEND]
    if mode_flag:
        cmd.append(mode_flag)
    cmd += [to, msg]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, "exec error: %s" % e


def main():
    raw = sys.stdin.read()
    data = json.loads(raw)              # parse error -> caught below -> allow
    if data.get("tool_name") != "SendMessage":
        allow()
    # Opt-in, off by default — mirrors SLACK_BUS_ENABLED. Unset/0 -> native runs unchanged.
    if os.environ.get("SENDMESSAGE_BUS_ENABLED") != "1":
        allow()
    ti = data.get("tool_input") or {}
    to = ti.get("to")
    msg = ti.get("message")
    if not isinstance(to, str) or not isinstance(msg, str) or not msg:
        allow()
    if not is_peer(to):
        allow()                        # main / subagent / unknown -> native

    # Peer target: route through the bus, fall back to local tmux send-keys.
    rc, out = send("--via-slack", to, msg)
    if rc == 0:
        log("routed bus -> %s" % to)
        deny("Routed to '%s' via the Slack agent bus (bus-by-default). The native "
             "SendMessage cannot reach peer agents in other windows/hosts, so it was "
             "delivered through the bus instead. Delivered OK — do not retry." % to)

    rc2, out2 = send("--local", to, msg)
    if rc2 == 0:
        log("bus down, local -> %s" % to)
        deny("Slack bus was unreachable; delivered to '%s' locally via tmux send-keys "
             "instead. Delivered OK — do not retry." % to)

    log("FAILED -> %s : %s | %s" % (to, out.strip()[-200:], out2.strip()[-200:]))
    deny("Failed to deliver to '%s' via the Slack bus and local tmux. The native "
         "SendMessage also cannot reach this peer agent. Surface this delivery failure; "
         "do not silently retry." % to)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Fail-safe: never break messaging — let the native tool run.
        sys.exit(0)
