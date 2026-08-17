#!/usr/bin/env python3
"""automode-watchdog — detect Claude Code's fail-closed auto-mode classifier
denials and self-heal by cycling the stuck pane out of the classifier's reach,
then reverting once the pane goes idle again.

## Why this exists (2026-08-17)

`nexus-proxy` already pins Claude Code's own per-tool-call permission-classifier
calls onto sonnet-5 (PR #52, `proxy/routing.py` CLASSIFIER_MARKERS) so a scarce
opus-5 capacity pool can't starve it. That measurably cut the failure rate but
did not eliminate it — sonnet-5 gets stressed too — and every denial is a dead
end: Claude Code's own message says "wait a moment and try again," but nothing
retries FOR the agent, and nothing alerts a human either (no hook fires for
this — see "No hook sees this" below). Real incident that prompted this file:
`svc-chatbot` hit 13 of these denials in 40 minutes on 2026-08-17, on both Bash
and Write, while nexus-proxy's own logs showed it correctly serving every
classifier call on sonnet-5 the entire time — the proxy fix was working and the
session still got stuck, silently, with no visibility anywhere.

## Detection: the transcript, not a hook

Every denial — a real "auto mode cannot determine the safety of X" fail-closed
AND a genuine classifier "no" alike — lands in the session's own JSONL
transcript (`~/.claude/projects/<cwd-slugged>/<session-id>.jsonl`) as a `user`
entry with a TOP-LEVEL `toolDenialKind` field. Confirmed directly against the
installed 2.1.233 binary's own string table (not guessed, not from docs):

    automode-unavailable    classifier unreachable — fail-closed, NOT a policy
                            decision. THIS is what we react to.
    automode-parsing-error  classifier responded but couldn't be parsed. Also
                            fail-closed for the same non-decision reason.
    automode-blocked        classifier ran fine and said no. A REAL safety
                            verdict from the model — never touch this one.
    user-rejected / permission-rule / interrupted / cancelled
                            human declined / static rule denied / turn
                            aborted. Not ours to react to.

**No hook fires for any of this.** Confirmed against the same binary's hook
schema dump: the full event list is PreToolUse / PostToolUse / PostToolBatch /
Notification / UserPromptSubmit / Stop / SubagentStop / PreCompact /
SessionStart / SessionEnd — and PreToolUse, the only one that runs before a
tool call, fires BEFORE the classifier does, so it can't see the outcome. (A
subagent asked to research this claimed a "PermissionDenied" hook exists —
that's fabricated; the string only appears in the binary as part of an
unrelated Rust OS-error-kind enum, "PermissionDeniedAddrNotAvailable".) So this
daemon tails each live pane's transcript file directly instead of hooking
anything.

## Self-heal target is Manual mode, not bypassPermissions

bypassPermissions was the original request, but it turns out to be unreachable
from an already-running session at all: empirically, `shift+tab` only cycles
Plan -> Auto -> Manual -> Accept Edits and back to Plan (verified by cycling a
disposable throwaway session through 7 presses — bypass never appeared).
bypassPermissions requires restarting the `claude` process with
`--permission-mode bypassPermissions` / `--dangerously-skip-permissions` at
LAUNCH, which kills in-flight subprocesses and is a much bigger, less
reversible action than this daemon should take unattended.

Manual mode is the next best thing, and is actually a good fit, not a
compromise: its whole design is "always ask, no classifier" — confirmed its
internal/CLI value is literally `"default"`, the original pre-auto-mode mode —
so cycling into it routes every subsequent tool call through a
`permission_prompt` Notification instead of the classifier. This repo's own
`notify-classify.py` already auto-approves the safe majority of those asks and
forwards only genuinely risky ones to Slack. So this trades "silently stuck"
for "asks like Claude Code always used to," at no new security cost — it
INCREASES scrutiny on the stuck pane, it doesn't loosen anything.

## Mechanics: raw escape bytes, not the named key

`herdr pane send-keys <pane> shift+tab` does NOT work — tested directly on a
disposable pane and confirmed dead (the footer never changes), most likely an
escape-sequence mismatch in herdr's own key synthesis. The raw terminal
sequence for Shift+Tab (classic "back-tab", CSI Z, `\x1b[Z`) sent as literal
text DOES work, confirmed on both a throwaway pane and this project's own live
pane. `substrate.sh send-keys` already falls through any key it doesn't
recognize (i.e. anything but `enter`/`escape`) to a literal write — `herdr pane
send-text` on the herdr backend, `tmux send-keys -l` on tmux — so no
substrate.sh changes were needed; this script just calls
`send-keys <pane> "\x1b[Z"` directly and it reaches the app on both backends.

## Which transcript belongs to which pane (fixed 2026-08-17)

The first cut derived it from the pane's cwd alone: slug the cwd, take the newest
`.jsonl` in that project dir. That silently conflates panes, because a cwd
identifies a PROJECT, not a session — several agents cwd'd to `$HOME` all slug to
one directory. Caught in live state: panes `w4J:p2` and `w4M:p2` held an
identical `transcript_path` AND `offset`, so one pane's denials were counted
against both and the watchdog could have cycled the mode of a pane that was
never stuck.

Fixed by not inferring at all. `record-transcript.sh` writes the real
`transcript_path` — which Claude Code hands every hook — to
`$NEXUS_TMUX_DIR/transcript-map/<pane>`, wired into SessionStart (authoritative,
lands before the session's first tool call and re-pins on `--resume`/clear),
PreToolUse (backfills panes that predate this, and guarantees an entry exists
before a denial can land, since a denial IS a tool call), and Stop (cheap
refresh). The cwd heuristic survives only as a fallback for a pane whose cwd no
other pane shares. Where neither applies, the pane is SKIPPED, not guessed at: a
missed self-heal leaves a pane exactly as stuck as it would be without this
daemon, while cycling an innocent pane's mode actively breaks it.

## Config (env, all optional — these are host-side tmux vars, not compose vars;
## set them in `~/.tmux/env.sh` or the plist's EnvironmentVariables, not .env.example)

  AUTOMODE_WATCHDOG_ENABLED          default 1     master kill switch
  AUTOMODE_WATCHDOG_AUTOFIX          default 1     0 = alert to Slack only,
                                                    never touch a pane's mode
  AUTOMODE_WATCHDOG_POLL_SECONDS     default 8
  AUTOMODE_WATCHDOG_THRESHOLD        default 2     denials within WINDOW
                                                    before acting on a pane
  AUTOMODE_WATCHDOG_WINDOW_SECONDS   default 90
  AUTOMODE_WATCHDOG_MAX_CYCLES       default 5     shift+tab presses before
                                                    giving up on one pane
  AUTOMODE_WATCHDOG_MAX_ESCALATION_SECONDS
                                     default 7200  hard revert backstop, in
                                                    case a pane never reports
                                                    idle again
  SLACK_BRIDGE_PORT                  default 8788  where /notify lives

State lives in `$NEXUS_TMUX_DIR/automode-watch-state.json` (offsets, rolling
denial timestamps, escalation bookkeeping) — safe to delete any time; the
daemon rebuilds it from a clean slate (never replays old transcript history on
a first sighting of a pane, only denials seen from that point forward).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime

NEXUS_TMUX_DIR = os.environ.get("NEXUS_TMUX_DIR", os.path.expanduser("~/.tmux"))
SUBSTRATE = os.path.join(NEXUS_TMUX_DIR, "substrate.sh")
STATE_PATH = os.path.join(NEXUS_TMUX_DIR, "automode-watch-state.json")
TRANSCRIPT_MAP_DIR = os.path.join(NEXUS_TMUX_DIR, "transcript-map")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

ENABLED = os.environ.get("AUTOMODE_WATCHDOG_ENABLED", "1") != "0"
AUTOFIX = os.environ.get("AUTOMODE_WATCHDOG_AUTOFIX", "1") != "0"
POLL_SECONDS = float(os.environ.get("AUTOMODE_WATCHDOG_POLL_SECONDS", "8"))
THRESHOLD = int(os.environ.get("AUTOMODE_WATCHDOG_THRESHOLD", "2"))
WINDOW_SECONDS = float(os.environ.get("AUTOMODE_WATCHDOG_WINDOW_SECONDS", "90"))
MAX_CYCLES = int(os.environ.get("AUTOMODE_WATCHDOG_MAX_CYCLES", "5"))
MAX_ESCALATION_SECONDS = float(os.environ.get("AUTOMODE_WATCHDOG_MAX_ESCALATION_SECONDS", "7200"))
SLACK_BRIDGE_PORT = os.environ.get("SLACK_BRIDGE_PORT", "8788")

FAIL_CLOSED_KINDS = {"automode-unavailable", "automode-parsing-error"}
BACKTAB = "\x1b[Z"  # CSI Z — classic terminal "Shift+Tab" / back-tab sequence

# Footer text -> normalized mode token (substring match, longest/most-specific first).
MODE_MARKERS = [
    ("accept edits", "acceptEdits"),
    ("bypass", "bypassPermissions"),  # not currently reachable live; matched defensively
    ("auto mode", "auto"),
    ("plan mode", "plan"),
    ("manual mode", "manual"),
]
TARGET_MODE = "manual"


def _run(*args: str, timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(
            [SUBSTRATE, *args], capture_output=True, text=True, timeout=timeout
        )
        return r.stdout
    except Exception:
        return ""


def _now() -> float:
    return time.time()


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def list_panes() -> list[dict]:
    """substrate.sh query -> [{pane, name, waiting, cwd, wait_type}, ...] for
    every live pane, on either backend. `name` empty means no registered agent."""
    out = []
    for line in _run("query").splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        pane, name, waiting, cwd = parts[0], parts[1], parts[2], parts[3]
        wait_type = parts[5] if len(parts) > 5 else ""
        if not pane or not cwd:
            continue
        out.append({"pane": pane, "name": name, "waiting": waiting, "cwd": cwd, "wait_type": wait_type})
    return out


def _map_key(pane: str) -> str:
    """Pane id -> map filename, matching record-transcript.sh's sanitization
    (and substrate.sh's sidecar convention): `:` and `/` become `_`."""
    return re.sub(r"[:/]", "_", pane)


def mapped_transcript(pane: str) -> str | None:
    """The transcript path a hook recorded for this pane, if any.

    Written by record-transcript.sh from the `transcript_path` Claude Code hands
    every hook, so it is exact — no inference. Ignored if the file it names has
    since disappeared."""
    try:
        with open(os.path.join(TRANSCRIPT_MAP_DIR, _map_key(pane))) as f:
            path = f.read().strip()
    except OSError:
        return None
    return path if path and os.path.exists(path) else None


def newest_transcript_in(cwd: str) -> str | None:
    """~/.claude/projects/<cwd slug>/, newest .jsonl inside.

    The slug replaces every non-alphanumeric character with its own `-`
    (verified against the installed binary's own slugging function —
    `e.replace(/[^a-zA-Z0-9]/g,"-")`, no run-collapsing), not just `/`. A naive
    `.replace('/', '-')` matches the repo-path segments but silently
    mis-resolves any cwd whose username/path has `.`/`@`/etc. — this box's
    actual home dir (`alex.persinger@getgarner.com`) is exactly that case.

    Fallback only — a cwd identifies a PROJECT, not a session, so this cannot
    tell two panes in one cwd apart. See resolve_transcripts.
    """
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    project_dir = os.path.join(PROJECTS_DIR, slug)
    try:
        candidates = [
            os.path.join(project_dir, f)
            for f in os.listdir(project_dir)
            if f.endswith(".jsonl")
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: os.path.getmtime(p))


_AMBIGUOUS_WARNED: set[str] = set()


def resolve_transcripts(panes: list[dict]) -> dict[str, str]:
    """pane id -> its transcript, mapping exactly where a hook recorded one and
    falling back to the cwd heuristic ONLY where that cannot be ambiguous.

    Two panes sharing a cwd share a project dir, so newest_transcript_in hands
    both the same file: denials from one get counted against both, and the
    watchdog can cycle the permission mode of a pane that was never stuck.
    Where no map entry exists AND the cwd is shared, this resolves to nothing
    and the pane is skipped for that tick. A missed self-heal is recoverable —
    the pane is merely as stuck as it would have been without this daemon —
    whereas yanking an innocent pane out of its mode is not. The gap is short:
    PreToolUse records the entry on that pane's very next tool call.
    """
    cwd_counts = Counter(p["cwd"] for p in panes)
    resolved: dict[str, str] = {}
    for p in panes:
        pane, cwd = p["pane"], p["cwd"]
        path = mapped_transcript(pane)
        if path is None and cwd_counts[cwd] == 1:
            path = newest_transcript_in(cwd)
        if path:
            resolved[pane] = path
        elif pane not in _AMBIGUOUS_WARNED:
            _AMBIGUOUS_WARNED.add(pane)
            print(
                f"[automode-watchdog] no transcript mapping for {pane} "
                f"({p['name'] or 'unnamed'}, cwd shared by {cwd_counts[cwd]} panes) — "
                f"skipping until a hook records one",
                file=sys.stderr,
            )
    # Defense in depth: a stale map entry (a pane id recycled before GC, or a hook
    # that never ran) could still point two panes at one file. Trust neither rather
    # than guess which one owns it.
    for path in [p for p, c in Counter(resolved.values()).items() if c > 1]:
        owners = [pane for pane, rp in resolved.items() if rp == path]
        print(
            f"[automode-watchdog] {len(owners)} panes resolve to the same transcript "
            f"({', '.join(owners)}) — skipping all of them this tick",
            file=sys.stderr,
        )
        for pane in owners:
            resolved.pop(pane, None)
    for pane in list(resolved):
        _AMBIGUOUS_WARNED.discard(pane)
    return resolved


def _parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return _now()


def scan_new_denials(path: str, offset: int) -> tuple[list[float], int]:
    """Read new lines since `offset`, return (denial timestamps found, new offset)."""
    denials = []
    try:
        size = os.path.getsize(path)
        if size < offset:
            offset = 0  # file rotated/truncated — restart from the top
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
    except OSError:
        return [], offset
    for raw in chunk.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if obj.get("type") != "user":
            continue
        if obj.get("toolDenialKind") in FAIL_CLOSED_KINDS:
            denials.append(_parse_ts(obj.get("timestamp", "")))
    return denials, new_offset


def read_mode(pane: str) -> str | None:
    text = _run("pane-visible", pane, "8")
    for line in reversed(text.splitlines()):
        low = line.lower()
        for marker, token in MODE_MARKERS:
            if marker in low:
                return token
    return None


def cycle_to(pane: str, target: str, max_presses: int) -> bool:
    """Press back-tab up to max_presses times, stopping as soon as the footer
    reports `target`. Returns whether it landed there."""
    for _ in range(max_presses):
        current = read_mode(pane)
        if current == target:
            return True
        _run("send-keys", pane, BACKTAB)
        time.sleep(0.4)
    return read_mode(pane) == target


def notify(name: str, pane: str, message: str, summary: str | None = None) -> None:
    body = json.dumps({
        "name": name or pane,
        "pane": pane,
        "kind": "automode_stuck",
        "message": message,
        "summary": summary or message,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{SLACK_BRIDGE_PORT}/notify",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # best-effort — the bridge being down shouldn't take the watchdog down


def handle_pane(pane_info: dict, path: str, state: dict) -> None:
    pane, name = pane_info["pane"], pane_info["name"]

    st = state.setdefault(pane, {})
    if st.get("transcript_path") != path:
        # New pane, or the session file changed underneath it (restart/resume) —
        # start from the END so we never replay old, already-resolved history.
        st["transcript_path"] = path
        st["offset"] = os.path.getsize(path)
        st["denials"] = []

    new_denials, st["offset"] = scan_new_denials(path, st.get("offset", 0))
    # Ignore denials while already escalated (a few can land right after the mode
    # switch, from calls that were in flight before it took effect) — but don't
    # `return` here, or the idle/revert check below would never run for this tick.
    if new_denials and not st.get("escalated"):
        cutoff = _now() - WINDOW_SECONDS
        st["denials"] = [t for t in st.get("denials", []) + new_denials if t >= cutoff]

    if not st.get("escalated") and len(st.get("denials", [])) >= THRESHOLD:
        count = len(st["denials"])
        label = name or pane
        if not AUTOFIX:
            notify(name, pane, f"{count}x automode-unavailable/parsing-error in {int(WINDOW_SECONDS)}s — watchdog is alert-only (AUTOMODE_WATCHDOG_AUTOFIX=0), not touching the pane.")
            st["denials"] = []  # avoid re-alerting every poll on the same burst
            return
        before = read_mode(pane)
        if before == TARGET_MODE:
            st["denials"] = []
            return
        if cycle_to(pane, TARGET_MODE, MAX_CYCLES):
            st["escalated"] = True
            st["escalated_at"] = _now()
            st["mode_before"] = before
            notify(
                name, pane,
                f"{count}x automode-unavailable/parsing-error in {int(WINDOW_SECONDS)}s on `{label}` — "
                f"cycled from `{before or 'unknown'}` to Manual mode so it can keep working "
                f"(asks-every-time now; notify-classify.py auto-approves the safe majority). "
                f"Will revert once idle.",
            )
        else:
            notify(
                name, pane,
                f"{count}x automode-unavailable/parsing-error on `{label}` but the watchdog couldn't "
                f"confirm landing on Manual mode after {MAX_CYCLES} shift+tab cycles — needs a manual look.",
            )
        st["denials"] = []

    if st.get("escalated"):
        idle = pane_info.get("waiting") == "2"
        overdue = _now() - st.get("escalated_at", _now()) > MAX_ESCALATION_SECONDS
        if idle or overdue:
            target_back = st.get("mode_before") or "acceptEdits"
            # mode_before is a normalized token; only revert to one we can target directly.
            reverted = cycle_to(pane, target_back, MAX_CYCLES) if target_back in dict(MODE_MARKERS).values() else False
            st["escalated"] = False
            st.pop("escalated_at", None)
            reason = "idle" if idle else f"{int(MAX_ESCALATION_SECONDS)}s escalation cap reached"
            if reverted:
                notify(name, pane, f"`{name or pane}` went {reason} — reverted from Manual back to `{target_back}`.")
            else:
                notify(name, pane, f"`{name or pane}` went {reason} — tried to revert to `{target_back}` but couldn't confirm; left in Manual mode, check it manually.")


def gc_transcript_map(live_panes: set[str]) -> None:
    """Drop map entries for panes that no longer exist.

    Pane ids get recycled — `w4J:p2` is reassigned to whatever opens there next.
    A leftover entry would point the new pane at the OLD session's transcript
    until a hook overwrote it, which is precisely the misattribution the map
    exists to prevent, so drop it as soon as the pane is gone."""
    keys = {_map_key(p) for p in live_panes}
    try:
        stale = [f for f in os.listdir(TRANSCRIPT_MAP_DIR) if f not in keys]
    except OSError:
        return
    for f in stale:
        try:
            os.remove(os.path.join(TRANSCRIPT_MAP_DIR, f))
        except OSError:
            pass


def tick(state: dict) -> None:
    panes = list_panes()
    if not panes:
        # substrate.sh returns empty BOTH when nothing is running and when the
        # query itself fails (backend down, daemon timeout — _run swallows it).
        # Indistinguishable from here, and there is nothing to do in the genuine
        # case, so treat empty as "unknown" and skip the tick entirely rather than
        # let the two GC passes below read it as "everything vanished".
        return
    live = {p["pane"] for p in panes}
    agents = [p for p in panes if p["name"]]  # registered fleet agents only
    seen = {p["pane"] for p in agents}
    transcripts = resolve_transcripts(agents)
    for pane_info in agents:
        path = transcripts.get(pane_info["pane"])
        if not path:
            continue  # unresolvable/ambiguous — resolve_transcripts logged why
        try:
            handle_pane(pane_info, path, state)
        except Exception as e:
            print(f"[automode-watchdog] error on {pane_info['pane']}: {e}", file=sys.stderr)
    # garbage-collect panes that vanished (closed) and weren't mid-escalation
    for pane in [p for p in state if p not in seen and not state[p].get("escalated")]:
        state.pop(pane, None)
    gc_transcript_map(live)


def main() -> None:
    if not ENABLED:
        print("[automode-watchdog] AUTOMODE_WATCHDOG_ENABLED=0 — exiting")
        return
    print(f"[automode-watchdog] watching (autofix={'on' if AUTOFIX else 'alert-only'}, "
          f"threshold={THRESHOLD}/{int(WINDOW_SECONDS)}s, poll={POLL_SECONDS}s)")
    state = load_state()
    while True:
        tick(state)
        save_state(state)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
