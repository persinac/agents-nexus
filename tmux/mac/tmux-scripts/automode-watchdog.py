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

**CORRECTION (2026-08-18): a PermissionDenied hook exists — the claim below
that it was "fabricated" was wrong.** Confirmed against Anthropic's own current
docs (code.claude.com/docs/en/hooks#permissiondenied), not a binary string
scan: `PermissionDenied` fires "when auto mode denies a tool call, INCLUDING
denials without a classifier verdict" — i.e. it covers exactly the
automode-unavailable/automode-parsing-error case this daemon exists for, not
just automode-blocked. Its input payload carries `classifier_verdict: null`
for a no-verdict denial (our target) vs. a real verdict string otherwise — a
direct, structured marker, no transcript parsing needed. It also hands
`session_id`/`cwd`/`tool_name` directly, which would make the whole
transcript-path resolution problem this file spends two sections on
(cwd-collision, record-transcript.sh, the transcript-map) moot: no
cross-referencing needed when the hook fires already scoped to the denying
pane. The hook can't itself gate the decision (exit code 2 is ignored, "the
denial already occurred") and its only real lever — `retry: true` — is
explicitly ignored for a no-verdict denial, so it doesn't replace what this
daemon does (cycle the pane to Manual); but as a DETECTION mechanism it is
strictly better than polling a transcript file: synchronous, zero poll lag,
no pane-to-transcript ambiguity. This daemon still tails the transcript, not
the hook, because it was built before this was found — not because no
alternative exists. Rearchitecting onto the hook is a real, not-yet-done
follow-up; flagging it here rather than leaving the disproven claim in place.

Original (now-wrong) reasoning, kept for the record rather than silently
deleted: a binary hook-schema string scan turned up PreToolUse / PostToolUse /
PostToolBatch / Notification / UserPromptSubmit / Stop / SubagentStop /
PreCompact / SessionStart / SessionEnd and no PermissionDenied, and a subagent's
claim that PermissionDenied existed was dismissed as fabricated on the theory
that the string only appeared in an unrelated Rust OS-error enum. That scan was
either run against a binary/version where the hook didn't yet exist, or missed
it outright — either way, trust the vendor docs over a binary string grep next
time this kind of question comes up.

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

## No debounce, by request (2026-08-17)

The first cut required 2 denials within a 90s WINDOW before acting, to absorb a
single flaky classifier call rather than react to it. Measured against the real
2026-08-17 incident this bought little: once a burst starts, denials landed
15-40s apart (e.g. 17:20:13, 17:20:31, 17:20:34, 17:20:48), so the 2-of-90s gate
added only about one POLL_SECONDS' worth of extra latency over reacting to the
first one. Changed to THRESHOLD=1 / POLL_SECONDS=4 so the very first fail-closed
denial triggers a reaction, detected within one poll cycle. Traded away: a
genuine one-off blip (classifier down for a single call, then fine) now cycles
the pane's permission mode same as a sustained outage would — accepted because
the fallback (Manual mode) is itself low-cost and self-reverts on idle. WINDOW
and THRESHOLD are still both real knobs — set AUTOMODE_WATCHDOG_THRESHOLD above
1 to bring the debounce back.

## Detection is now dual: the PermissionDenied hook (primary) + this poll loop (backstop) (2026-08-18)

Once the "CORRECTION" note above established that `PermissionDenied` fires
synchronously for exactly our case (`denial_reason: "no_verdict"`), with no
transcript-path resolution needed at all (Claude Code hands the pane's
identity via env, same as every other hook), it became strictly better than
polling for the ESCALATE side: zero poll-interval lag, no cwd-collision
ambiguity. `hook-permissiondenied.sh` -> `python3 automode-watchdog.py --hook`
reads the hook's stdin payload once and calls `note_denial_and_maybe_escalate`.

The REVERT side moved the same way, onto hooks that already fire at the right
moments: `hook-stop.sh` calls `--revert-check --idle` the instant a turn ends
(replacing poll-discovered idleness), and `hook-pretooluse.sh` calls bare
`--revert-check` on every tool call, which gives the MAX_ESCALATION_SECONDS
hard cap a check on every single call an escalated pane makes — no timer
thread needed for that either.

This poll loop (`main()`/`tick()`) keeps running as the backstop: it still
does its own transcript scan and its own revert check every POLL_SECONDS, so
losing a single hook invocation (a hook script error, a Claude Code version
that changes the payload shape) degrades to "poll-loop speed," not "stuck."
Both paths write the same `automode-watch-state.json` under the same
`locked_state()` — whichever notices first (virtually always the hook) sets
`escalated`/reverts it, and the other sees that and no-ops. This is why
`locked_state()` exists at all: once more than one process can touch this
file, an unlocked load-modify-save can silently drop a concurrent writer's
update to a DIFFERENT pane's entry, not just race on the same one.

## A third, untagged failure surface: slash-command bash substitution (2026-08-18)

`/checkpoint` (and any slash-command using inline `!cmd` bash substitution in
its body) failed twice in the wild with the exact same "auto mode cannot
determine the safety of Bash" root cause — but as a plain
`<local-command-stderr>` user message, no `toolDenialKind`, and the
PermissionDenied hook never fired for it either (confirmed against
automode-hook.log). Slash-command bash substitution runs during command
PREPROCESSING, before a turn or tool-call object exists at all, so neither
detection path this file already had could see it. Added a second branch to
`scan_new_denials` matching `NO_VERDICT_TEXT_MARKER` directly against the
message text on any untagged `user` entry. Poll-path only — there is no hook
event to catch this on the fast path, so this failure class still detects at
poll speed (up to POLL_SECONDS), not hook speed. It also can't rescue the
`/checkpoint` invocation that already failed — by the time it's in the
transcript, control already returned to the human — but it can cycle the
pane to Manual before the NEXT attempt, which is what actually matters here:
a human retrying a slash command manually, twice, ~22s apart, is exactly the
"still stuck, still failing" pattern this whole daemon exists to interrupt.

## Config (env, all optional — these are host-side tmux vars, not compose vars;
## set them in `~/.tmux/env.sh` or the plist's EnvironmentVariables, not .env.example)

  AUTOMODE_WATCHDOG_ENABLED          default 1     master kill switch
  AUTOMODE_WATCHDOG_AUTOFIX          default 1     0 = alert to Slack only,
                                                    never touch a pane's mode
  AUTOMODE_WATCHDOG_POLL_SECONDS     default 4
  AUTOMODE_WATCHDOG_THRESHOLD        default 1     denials within WINDOW
                                                    before acting on a pane —
                                                    at the default of 1, the
                                                    very first fail-closed
                                                    denial qualifies and
                                                    WINDOW is moot; only
                                                    matters if raised above 1
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

import contextlib
import fcntl  # POSIX-only (flock) — fine: this daemon and its hooks are macOS/Linux only
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
STATE_LOCK_PATH = STATE_PATH + ".lock"
TRANSCRIPT_MAP_DIR = os.path.join(NEXUS_TMUX_DIR, "transcript-map")
REGISTRY_DIR = os.path.join(NEXUS_TMUX_DIR, "registry")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

ENABLED = os.environ.get("AUTOMODE_WATCHDOG_ENABLED", "1") != "0"
AUTOFIX = os.environ.get("AUTOMODE_WATCHDOG_AUTOFIX", "1") != "0"
POLL_SECONDS = float(os.environ.get("AUTOMODE_WATCHDOG_POLL_SECONDS", "4"))
THRESHOLD = int(os.environ.get("AUTOMODE_WATCHDOG_THRESHOLD", "1"))
WINDOW_SECONDS = float(os.environ.get("AUTOMODE_WATCHDOG_WINDOW_SECONDS", "90"))
MAX_CYCLES = int(os.environ.get("AUTOMODE_WATCHDOG_MAX_CYCLES", "5"))
MAX_ESCALATION_SECONDS = float(os.environ.get("AUTOMODE_WATCHDOG_MAX_ESCALATION_SECONDS", "7200"))
SLACK_BRIDGE_PORT = os.environ.get("SLACK_BRIDGE_PORT", "8788")

FAIL_CLOSED_KINDS = {"automode-unavailable", "automode-parsing-error"}

# Untagged fail-closed signal (2026-08-18): a slash-command's inline `!cmd`
# bash substitution goes through the same auto-mode classifier during
# command-PREPROCESSING, before any tool-call/turn object exists — so a
# failure there surfaces as a plain <local-command-stderr> USER message with
# NO toolDenialKind at all, invisible to both the branch above and the
# PermissionDenied hook (confirmed: automode-hook.log stayed empty across two
# real `/checkpoint` failures that hit this). Scoped to only the exact phrase
# actually observed twice in the wild for the "classifier model unavailable"
# case — deliberately NOT the broader "Auto mode could not evaluate this
# action and is blocking it for safety" prefix the docs also describe for the
# parsing-error and safety-filter cases, because that prefix is IDENTICAL for
# a real, intentional safety block (never touch) and distinguished only by an
# appended clause — matching it correctly needs an exclusion this repo has no
# confirmed real case to validate against. This phrase alone is unambiguous.
NO_VERDICT_TEXT_MARKER = "is temporarily unavailable, so auto mode cannot determine the safety of"
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


@contextlib.contextmanager
def locked_state():
    """Serialize load-modify-save against the shared state file.

    Needed since the PermissionDenied hook turned this from "one poll-loop
    process, exclusive owner of the state file" into "many short-lived hook
    processes plus one long-lived poll loop, all touching the same file,
    firing concurrently whenever the fleet has more than one pane." Without a
    lock, two overlapping load-modify-save cycles silently drop whichever
    wrote second's view of a THIRD pane's state (last-writer-wins on the whole
    file, not just the pane each was updating) — e.g. a hook escalating pane A
    could clobber a stop-hook's revert of pane B if they raced. Advisory
    flock on a sidecar file, not the state file itself, so a reader is never
    blocked mid-open on the file being replaced by os.replace() above."""
    fh = open(STATE_LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def registry_name(pane: str) -> str:
    """Same NAME= lookup every bash hook does against ~/.tmux/registry/<pane>,
    reimplemented here so the PermissionDenied/revert-check CLI modes below
    don't need to shell out just to label a Slack notification."""
    try:
        with open(os.path.join(REGISTRY_DIR, pane)) as f:
            for line in f:
                if line.startswith("NAME="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


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


def _message_text(obj: dict) -> str:
    """Normalize a transcript entry's message content to plain text. The
    tagged tool-result case (toolDenialKind present) carries content as a
    list of blocks; the untagged local-command-stderr case below carries it
    as a plain string directly."""
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(c.get("text") or c.get("content") or "")
            for c in content if isinstance(c, dict)
        )
    return ""


def scan_new_denials(path: str, offset: int) -> tuple[list[float], int]:
    """Read new lines since `offset`, return (denial timestamps found, new offset).

    Two shapes count: the normal toolDenialKind-tagged tool-call denial, and
    the untagged slash-command case (see NO_VERDICT_TEXT_MARKER) — the elif
    below can't double-count a tagged entry against the marker text, since a
    tagged automode-unavailable entry's own message ALSO contains that exact
    phrase but never reaches the elif at all."""
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
        elif NO_VERDICT_TEXT_MARKER in _message_text(obj):
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


def _escalate(pane: str, name: str, st: dict) -> None:
    """Cycle `pane` to Manual mode. Shared by both detection paths (the
    PermissionDenied hook's single real-time event, and this poll loop's
    transcript-scan batch) — whichever fires first does the work; the other
    sees `st["escalated"]` already set by its caller and never reaches here."""
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


def _maybe_revert(pane: str, name: str, st: dict, idle: bool) -> None:
    """Revert `pane` out of an escalation if it's gone idle or blown the hard
    cap. Shared by: hook-stop.sh (idle=True, fires the instant a turn ends —
    the common case, now event-driven instead of poll-discovered),
    hook-pretooluse.sh (idle=False, just the MAX_ESCALATION_SECONDS backstop —
    every tool call on an escalated pane gets a free cap check), and this poll
    loop (idle from list_panes(), same as always — the redundant safety net in
    case a hook invocation is ever lost)."""
    if not st.get("escalated"):
        return
    overdue = _now() - st.get("escalated_at", _now()) > MAX_ESCALATION_SECONDS
    if not (idle or overdue):
        return
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


def note_denial_and_maybe_escalate(pane: str, name: str, state: dict) -> None:
    """Record one fail-closed denial for `pane` at the moment it's called, and
    escalate if the rolling count has hit THRESHOLD. This is the
    PermissionDenied-hook path: one call per real-time denial, timestamped by
    when the hook actually fired — more precise than the poll path's
    transcript-scan timestamps, since there's no poll-interval discovery lag
    to begin with."""
    st = state.setdefault(pane, {})
    if st.get("escalated"):
        return  # a few can still land in flight right after the mode switch
    cutoff = _now() - WINDOW_SECONDS
    st["denials"] = [t for t in st.get("denials", []) + [_now()] if t >= cutoff]
    if len(st["denials"]) >= THRESHOLD:
        _escalate(pane, name, st)


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
        _escalate(pane, name, st)

    _maybe_revert(pane, name, st, pane_info.get("waiting") == "2")


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
          f"threshold={THRESHOLD}/{int(WINDOW_SECONDS)}s, poll={POLL_SECONDS}s) — "
          f"transcript-scan + revert backstop; the PermissionDenied hook is the primary detector")
    while True:
        # Reload under lock each tick, rather than holding one in-memory copy for
        # the daemon's whole lifetime: the PermissionDenied hook and the
        # Stop/PreToolUse revert-check hooks are now separate short-lived
        # processes writing this same file between ticks. Without reloading, a
        # stale in-memory copy would overwrite their escalate/revert with
        # whatever this loop last saw — silently undoing a hook's work.
        with locked_state():
            state = load_state()
            tick(state)
            save_state(state)
        time.sleep(POLL_SECONDS)


def hook_main() -> None:
    """One-shot: read a PermissionDenied hook payload from stdin, escalate
    this pane immediately if it's a fail-closed (no-verdict) denial. Always
    exits 0 and never raises past this function — a hook must never fail a
    turn, and per Claude Code's own docs PermissionDenied's exit code is
    ignored anyway ("the denial already occurred")."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    # denial_reason == "no_verdict" is the precise signal, not classifier_verdict
    # alone: per Anthropic's docs, classifier_verdict is ALSO null for a
    # custom-allow-rule or built-in-check denial (a real, intentional deny) —
    # reacting to those would mean cycling a pane's mode over a legitimate
    # block, exactly what this daemon must never do. Require both, matching the
    # docs' own description of the no-verdict case.
    if payload.get("denial_reason") != "no_verdict" or payload.get("classifier_verdict") is not None:
        return
    pane = os.environ.get("TMUX_PANE") or os.environ.get("HERDR_PANE_ID")
    if not pane:
        return
    name = registry_name(pane)
    with locked_state():
        state = load_state()
        note_denial_and_maybe_escalate(pane, name, state)
        save_state(state)


def revert_check_main(idle: bool) -> None:
    """One-shot: check whether this pane should revert out of an escalation.
    Called from hook-stop.sh (idle=True — the pane just went idle, the normal
    revert path) and hook-pretooluse.sh (idle=False — just the
    MAX_ESCALATION_SECONDS hard-cap backstop, piggybacked on the hook that
    fires on every tool call so the cap needs no poll loop of its own)."""
    pane = os.environ.get("TMUX_PANE") or os.environ.get("HERDR_PANE_ID")
    if not pane:
        return
    name = registry_name(pane)
    with locked_state():
        state = load_state()
        st = state.get(pane)
        if st:
            _maybe_revert(pane, name, st, idle)
        save_state(state)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--hook":
            hook_main()
        elif len(sys.argv) > 1 and sys.argv[1] == "--revert-check":
            revert_check_main(idle="--idle" in sys.argv[2:])
        else:
            main()
    except Exception as e:
        # The two one-shot modes run as Claude Code hooks — a hook must never
        # surface an error to the turn in progress. main() has its own
        # long-running loop and isn't expected to hit this, but the same
        # backstop costs nothing to keep here too.
        if len(sys.argv) > 1 and sys.argv[1] in ("--hook", "--revert-check"):
            print(f"[automode-watchdog] {sys.argv[1]} error: {e}", file=sys.stderr)
        else:
            raise
