#!/usr/bin/env python3
"""Handoff/verify fidelity guards. pytest-free — run: python3 test_handoff_fidelity.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conductor

_fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fails.append(name)


SPEC = "COLUMNS: captured_at, endpoint_id, model_slug, ttft_p50_ms, n_requests, raw_stats"
EPILOGUE = "Two notes on execution: the assigned skill did not fit, and Write was disabled."
transcript = f"{SPEC} " + ("filler. " * 400) + EPILOGUE

r = conductor._worker_result("s1", "done", transcript, [])
check("short transcript: handoff keeps the spec", SPEC in r["handoff"])
check("short transcript: nothing is dropped", r["handoff"] == transcript)
check("short transcript: full_output is untruncated", r["full_output"] == transcript)

overflow = f"{SPEC} " + ("filler. " * (conductor.HANDOFF_MAX // 4)) + EPILOGUE
ro = conductor._worker_result("s1", "done", overflow, [])
check("overflow: handoff keeps the HEAD (the spec survives)", SPEC in ro["handoff"])
check("overflow: the tail epilogue is what gets dropped", not ro["handoff"].endswith(EPILOGUE))
check("overflow: handoff respects HANDOFF_MAX", len(ro["handoff"]) == conductor.HANDOFF_MAX)
check("overflow: full_output still keeps everything", ro["full_output"] == overflow)

structured = (
    'Prose the model emitted first. {"status":"done","summary":"ported the scraper",'
    '"artifacts":["/tmp/a.py"],"handoff":"' + SPEC + '"}'
)
r2 = conductor._worker_result("s1", "error", structured, ["/tmp/b.py"])
check("structured: handoff comes from the JSON contract", r2["handoff"] == SPEC)
check("structured: summary comes from the contract", r2["summary"] == "ported the scraper")
check("structured: declared + observed artifacts are unioned",
      r2["artifacts"] == ["/tmp/a.py", "/tmp/b.py"])
check("structured: contract status overrides the transcript status", r2["status"] == "done")

r3 = conductor._worker_result("s1", "done", '{"status":"done","summary":"x","handoff":"   "}', [])
check("blank handoff in contract falls back to the transcript", r3["handoff"].strip() != "")

r4 = conductor._worker_result("s1", "done", "no json here at all", [])
check("unparseable output still yields a handoff", r4["handoff"] == "no json here at all")

noise = (
    'I edited the config: {"retries": 3, "timeout": 30}. Then I ran the tests. '
    '{"status":"done","summary":"real result","handoff":"' + SPEC + '"}'
)
r5 = conductor._worker_result("s1", "done", noise, [])
check("a printed config snippet is not mistaken for the result", r5["summary"] == "real result")
check("the LAST contract object wins over an earlier one", r5["handoff"] == SPEC)

only_noise = 'I edited {"retries": 3, "timeout": 30} and stopped.'
r6 = conductor._worker_result("s1", "done", only_noise, [])
check("non-contract JSON is ignored entirely", r6["summary"] == only_noise)
check("_last_result_json returns {} when nothing is contract-shaped",
      conductor._last_result_json(only_noise) == {})

r7 = conductor._worker_result("s1", "done", 'trailing {"status":"blocked","summary":"cannot"}', [])
check("blocked status from the contract is honoured", r7["status"] == "blocked")

rd = conductor._worker_result("s1", "error", "partial work", ["/tmp/a.py"],
                              degraded="max_turns; reconciled from git — 12 file(s) changed")
check("a reconciled turn-limit round counts as done", rd["status"] == "done")
check("the reconciled round records why it was degraded", "max_turns" in rd["degraded"])
check("the degraded reason is visible in the summary", rd["summary"].startswith("[degraded]"))
check("a reconciled round keeps its artifacts", rd["artifacts"] == ["/tmp/a.py"])
rd2 = conductor._worker_result("s1", "error", '{"status":"error","summary":"gave up"}', ["/tmp/a.py"],
                               degraded="max_turns; reconciled")
check("a contract 'error' cannot override a git-reconciled done", rd2["status"] == "done")


long_handoff = "H" * 5000
check("dep_context passes the full handoff (not [:300])",
      len(conductor._dep_context({"handoff": long_handoff})) == 5000)
check("dep_context prefers handoff over full_output",
      conductor._dep_context({"handoff": "A", "full_output": "B"}) == "A")
check("dep_context falls back to full_output",
      conductor._dep_context({"full_output": "B", "summary": "C"}) == "B")
check("dep_context falls back to summary",
      conductor._dep_context({"summary": "C"}) == "C")
check("dep_context ignores empty strings",
      conductor._dep_context({"handoff": "  ", "summary": "C"}) == "C")
check("dep_context on an empty result is explicit",
      conductor._dep_context({}) == "(no handoff recorded)")
check("dep_context is bounded by HANDOFF_MAX",
      len(conductor._dep_context({"handoff": "H" * (conductor.HANDOFF_MAX + 500)}))
      == conductor.HANDOFF_MAX)


d = tempfile.mkdtemp(prefix="refprobe-")
src = os.path.join(d, "004_openrouter_endpoint_perf.sql")
with open(src, "w") as f:
    f.write("CREATE TABLE x (captured_at TIMESTAMPTZ, endpoint_id UUID, n_requests BIGINT);")
missing = os.path.join(d, "nope.sql")

goal = f"Port the scraper. Source: {src}. Also {missing} which does not exist."
probes = conductor._reference_probes(goal)
check("reference probe found the real file", [p["path"] for p in probes] == [src])
check("reference probe carries the content", "endpoint_id" in probes[0]["content"])
check("reference probe marks non-truncated content", probes[0]["truncated"] is False)
check("nonexistent path is skipped", all(p["path"] != missing for p in probes))
check("a goal naming no files yields no probes", conductor._reference_probes("do a thing") == [])
check("probe count is capped", len(conductor._reference_probes(" ".join([src] * 20), cap=3)) <= 3)
check("duplicate paths are de-duplicated",
      len(conductor._reference_probes(f"{src} and again {src}")) == 1)

big = os.path.join(d, "big.py")
with open(big, "w") as f:
    f.write("z" * (conductor.REF_PROBE_BYTES + 100))
bp = conductor._reference_probes(f"see {big}")
check("oversized reference is truncated and flagged",
      len(bp[0]["content"]) == conductor.REF_PROBE_BYTES and bp[0]["truncated"] is True)


brace_prose = (
    'The goal fixes the bucket as garner-health-app-data-{env}, but the code hyphenates it. '
    '{"pass": false, "findings": [{"severity":"blocker","where":"a.py:1","what":"renamed"}]}'
)
v = conductor._verdict_json(brace_prose)
check("a quoted {env} placeholder no longer eats the verdict", v["pass"] is False)
check("the real verdict's findings survive the brace in prose",
      v["findings"][0]["severity"] == "blocker")

v_pass = conductor._verdict_json('Looks fine to me. {"pass": true, "findings": []}')
check("a passing verdict parses", v_pass["pass"] is True and v_pass["findings"] == [])

for _bad in ("no json at all", "{env}", 'prose {env} more prose', '{"unrelated": 1}'):
    _v = conductor._verdict_json(_bad)
    check(f"unparseable reviewer output fails CLOSED: {_bad[:22]!r}", _v["pass"] is False)
check("fail-closed verdict explains itself",
      "no parseable verdict" in conductor._verdict_json("{env}")["findings"][0]["what"])
check("a verdict missing findings gets an empty list",
      conductor._verdict_json('{"pass": true}')["findings"] == [])

MISSION = ("EXACT NAMES: table chatbot_analytics.openrouter_endpoint_perf.\n"
           "Columns: captured_at, endpoint_id, ttft_p99_ms, n_requests, raw_stats.\n"
           "ADD the CronJob to the existing kubernetes/cronjob.yml. Do NOT create a new manifest.")
PLANNED = "Port the scraper, write parquet to S3, create the Athena table."

sg = conductor._subtask_goal(PLANNED, MISSION)
check("subtask goal keeps the planner framing", PLANNED in sg)
check("subtask goal carries the exact column names", "ttft_p99_ms" in sg)
check("subtask goal carries the file-target prohibition", "Do NOT create a new manifest" in sg)
check("subtask goal marks the mission goal authoritative", "AUTHORITATIVE" in sg)
check("mission goal is bounded",
      len(conductor._subtask_goal(PLANNED, "x" * 99999))
      <= len(PLANNED) + len(conductor._MISSION_GOAL_HDR) + conductor.MISSION_GOAL_MAX)
check("no mission goal is a no-op", conductor._subtask_goal(PLANNED, "") == PLANNED)
check("a one-shot plan that already IS the goal is not duplicated",
      conductor._subtask_goal(MISSION, MISSION) == MISSION)

_fb = f"{sg}\n\n[Verification feedback]\nstale noise"
check("appending verification feedback preserves the mission goal",
      "ttft_p99_ms" in _fb.split("\n\n[Verification feedback]")[0])
_uc = f"{sg}\n\n[Upstream context]\nfrom s1"
check("appending upstream context preserves the mission goal",
      "ttft_p99_ms" in _uc.split("\n\n[Upstream context]")[0])

g = (
    "1. New module pipelines/openrouter_perf.py.\n"
    "2. EXTEND the existing scripts/setup_athena.py. Do NOT create a new setup script.\n"
    "3. ADD the CronJob to the existing kubernetes/cronjob.yml. Do NOT create a new manifest file.\n"
)
t = conductor._extend_targets(g)
check("extend cue picks up setup_athena.py", "scripts/setup_athena.py" in t)
check("extend cue picks up cronjob.yml", "kubernetes/cronjob.yml" in t)
check("a brand-new module on its own line is NOT an extend target",
      "pipelines/openrouter_perf.py" not in t)
check("a cue does not leak across lines",
      conductor._extend_targets("Extend the existing a/b.py\nAlso write c/d.py\n") == ["a/b.py"])
check("no cue means no targets",
      conductor._extend_targets("Write pipelines/foo.py and tests/test_foo.py.") == [])
check("absolute paths are not extend targets",
      conductor._extend_targets("Extend the existing /etc/thing/conf.yml here.") == [])
check("a dotted filename survives the matcher",
      conductor._extend_targets("Extend the existing scripts/setup_athena.py now.")
      == ["scripts/setup_athena.py"])
check("a dependency to USE is not an extend target",
      conductor._extend_targets(
          "Write parquet through the existing S3Output helper in pipelines/common.py.") == [])
check("'existing' alone does not make a target",
      conductor._extend_targets("Follow the existing pattern in pipelines/glue.py.") == [])

import subprocess as _sp

_wt = tempfile.mkdtemp(prefix="extend-wt-")
_sp.run(["git", "init", "-q", _wt], check=True)
os.makedirs(os.path.join(_wt, "kubernetes"))
os.makedirs(os.path.join(_wt, "scripts"))
for _f in ("kubernetes/cronjob.yml", "scripts/setup_athena.py"):
    open(os.path.join(_wt, _f), "w").write("original\n")
_sp.run(["git", "-C", _wt, "add", "-A"], check=True)
_sp.run(["git", "-C", _wt, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "base"], check=True)
_sp.run(["git", "-C", _wt, "branch", "-M", "main"], check=True)
_sp.run(["git", "-C", _wt, "checkout", "-q", "-b", "mission"], check=True)

# the round-0 violation: sibling manifest created, cronjob.yml untouched
open(os.path.join(_wt, "kubernetes/cronjob-openrouter.yml"), "w").write("new\n")
open(os.path.join(_wt, "scripts/setup_athena.py"), "a").write("extended\n")
pr = {p["path"]: p for p in conductor._target_file_probes(g, _wt)}
check("probe flags the untouched extend target", pr["kubernetes/cronjob.yml"]["ok"] is False)
check("probe names the sibling that was created instead",
      "kubernetes/cronjob-openrouter.yml" in pr["kubernetes/cronjob.yml"]["new_files_in_same_dir"])
check("probe passes the target that WAS modified", pr["scripts/setup_athena.py"]["ok"] is True)

prompt_unmet = conductor._reviewer_prompt(g, [], list(pr.values()), "completeness", _wt)
check("reviewer is told the unmet target is a blocker", "TARGET FILES" in prompt_unmet)
check("reviewer prompt names the unmet file",
      "kubernetes/cronjob.yml" in prompt_unmet.split("TARGET FILES")[1][:200])

# the round-1 fix: cronjob.yml actually extended
open(os.path.join(_wt, "kubernetes/cronjob.yml"), "a").write("---\nsecond doc\n")
pr2 = {p["path"]: p for p in conductor._target_file_probes(g, _wt)}
check("probe clears once the target is really modified", pr2["kubernetes/cronjob.yml"]["ok"] is True)

# a worker that COMMITS its work must still count as having modified the target
_sp.run(["git", "-C", _wt, "add", "-A"], check=True)
_sp.run(["git", "-C", _wt, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "mission work"], check=True)
check("worktree is clean after the commit",
      _sp.run(["git", "-C", _wt, "status", "--porcelain"],
              capture_output=True, text=True).stdout.strip() == "")
pr3 = {p["path"]: p for p in conductor._target_file_probes(g, _wt)}
check("COMMITTED edits still count as modified (git status alone would say no)",
      pr3["kubernetes/cronjob.yml"]["modified"] is True)
check("committed extend target passes", pr3["kubernetes/cronjob.yml"]["ok"] is True)
check("committed setup_athena target passes", pr3["scripts/setup_athena.py"]["ok"] is True)

cf = conductor._changed_files(_wt)
check("lint attribution sees COMMITTED files (else every failure reads as pre-existing)",
      "kubernetes/cronjob.yml" in cf)
check("lint attribution still carries basenames for output matching", "cronjob.yml" in cf)

# verbatim `uv run ruff check .`; the first parser only knew --concise and found nothing here
RUFF_DEFAULT = (
    "warning: The following rules have been removed and ignoring them has no effect:\n"
    "    - UP038\n\n"
    "SIM105 Use `contextlib.suppress(Exception)` instead of `try`-`except`-`pass`\n"
    "  --> pipelines/common.py:90:13\n"
    "   |\n"
    "88 |           except BaseException:\n"
    "90 | /             try:\n"
    "   | |____________________^\n"
    "help: Replace `try`-`except`-`pass` with `with contextlib.suppress(Exception): ...`\n\n"
    "T201 `print` found\n"
    "  --> scripts/create_snowflake_db.py:235:9\n"
    "help: Remove `print`\n"
    "Found 2 errors.\n"
)
rf = conductor._check_findings(RUFF_DEFAULT)
check("ruff's DEFAULT format is parsed at all", len(rf) == 2)
check("ruff default: the file is attributed",
      ("pipelines/common.py", ) == tuple(f for f, m in rf if "SIM105" in m))
check("ruff default: source-context lines are not mistaken for findings",
      not any(f.isdigit() for f, _m in rf))
check("mypy-style one-line findings still parse",
      conductor._check_findings("pipelines/x.py:12: error: bad type [arg-type]")
      == {("pipelines/x.py", "error: bad type [arg-type]")})

# same two findings, line numbers shifted by an edit above them
RUFF_SHIFTED = RUFF_DEFAULT.replace("common.py:90:13", "common.py:96:13")
check("ruff default: a line-number shift is not a new finding",
      conductor._check_findings(RUFF_SHIFTED) == rf)

BASE_LINT = (
    "pipelines/common.py:86:13: SIM105 Use `contextlib.suppress(Exception)` instead\n"
    "scripts/create_snowflake_db.py:235:9: T201 `print` found\n"
    "Found 2 errors.\n"
)
BRANCH_SAME = (
    "pipelines/common.py:92:13: SIM105 Use `contextlib.suppress(Exception)` instead\n"
    "scripts/create_snowflake_db.py:235:9: T201 `print` found\n"
    "Found 2 errors.\n"
)
touched = {"pipelines/common.py", "common.py", "pipelines/openrouter_perf.py", "openrouter_perf.py"}

check("pre-existing debt in a file the mission merely edited is NOT the mission's fault",
      conductor._new_check_findings(BRANCH_SAME, BASE_LINT, touched) == [])
check("line-number drift alone is not a new finding",
      conductor._check_findings(BASE_LINT) == conductor._check_findings(BRANCH_SAME))

BRANCH_NEW = BRANCH_SAME + "pipelines/openrouter_perf.py:12:1: F401 unused import\n"
nf = conductor._new_check_findings(BRANCH_NEW, BASE_LINT, touched)
check("a genuinely NEW finding in a mission file hard-fails", len(nf) == 1)
check("the new finding is named", "openrouter_perf.py" in nf[0] and "F401" in nf[0])

check("a new finding in a file the mission never touched is not attributed",
      conductor._new_check_findings(
          BRANCH_SAME + "other/untouched.py:3:1: F401 unused import\n", BASE_LINT, touched) == [])
check("no changed-file info falls back to reporting all new findings",
      len(conductor._new_check_findings(BRANCH_NEW, BASE_LINT, set())) == 1)
check("no TARGET FILES block when every target is met",
      "TARGET FILES" not in conductor._reviewer_prompt(g, [], list(pr2.values()), "x", _wt))
check("a goal with no extend cues yields no probes",
      conductor._target_file_probes("just build pipelines/x.py", _wt) == [])

p_with = conductor._reviewer_prompt("goal", [], probes, "correctness", d)
p_without = conductor._reviewer_prompt("goal", [], [{"probe": "artifact"}], "correctness", d)
check("fidelity instruction present when references exist", "FIDELITY CHECK" in p_with)
check("fidelity instruction names the source path", src in p_with)
check("fidelity instruction calls dropped fields a blocker", "BLOCKER" in p_with)
check("no fidelity instruction without references", "FIDELITY CHECK" not in p_without)


for _v, _want in (("", False), ("0", False), ("no", False), ("off", False),
                  ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True)):
    if _v:
        os.environ["CONDUCTOR_GATE_BEFORE_REPORT"] = _v
    else:
        os.environ.pop("CONDUCTOR_GATE_BEFORE_REPORT", None)
    check(f"gate env {_v!r} -> {_want}", conductor._resolve_gate_before_report() is _want)
os.environ.pop("CONDUCTOR_GATE_BEFORE_REPORT", None)
check("gate is OFF by default", conductor._resolve_gate_before_report() is False)


prof = conductor.PROFILES
check("research profile exists", "research" in prof)
check("research profile is skill-less", not prof.get("research", {}).get("skill"))
check("research profile is read-only", prof.get("research", {}).get("permission") == "read-only")
check("investigation still owns incident-postmortem",
      prof.get("investigation", {}).get("skill") == "incident-postmortem")

if _fails:
    print("\n%d FAILED: %s" % (len(_fails), _fails))
    sys.exit(1)
print("\nall handoff/fidelity tests passed")
