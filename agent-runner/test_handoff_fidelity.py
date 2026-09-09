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
