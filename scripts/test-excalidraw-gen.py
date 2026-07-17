#!/usr/bin/env python3
"""test-excalidraw-gen.py — self-test for excalidraw-gen.py.

Drives the real CLI end-to-end across many spec shapes (DSL + JSON, TB + LR,
branches, cycles, self-loops, error inputs) and re-parses each output to assert
the load-critical invariants independently of the generator's own validate():

  - envelope: type=="excalidraw", version==2, appState/files present
  - every shape has bound text, wired on BOTH sides (the "empty box" bug)
  - every non-title text back-references its container
  - every arrow is bound on BOTH ends with reciprocal refs (the "fake edge" bug)
  - no overlapping boxes (layout sanity)
  - required per-element fields present
  - --seed N is byte-reproducible
  - bad specs fail non-zero and write nothing

Stdlib only. Exits 0 if all pass, 1 otherwise. Safe to wire into CI.

Usage:
    test-excalidraw-gen.py [--gen PATH]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

GEN_DEFAULT = Path(__file__).with_name("excalidraw-gen.py")
SHAPES = {"rectangle", "diamond", "ellipse"}
_fail = 0


def run(gen, spec, *flags, expect_ok=True):
    """Invoke the CLI with spec on stdin; return parsed scene (or None on error)."""
    proc = subprocess.run(
        [sys.executable, str(gen), "--stdout", *flags],
        input=spec, capture_output=True, text=True,
    )
    if expect_ok:
        assert proc.returncode == 0, f"expected success, got rc={proc.returncode}: {proc.stderr}"
        return json.loads(proc.stdout)
    assert proc.returncode != 0, f"expected failure, but rc=0 for spec:\n{spec}"
    return None


def check_invariants(scene, label):
    els = scene["elements"]
    by_id = {e["id"]: e for e in els}

    assert scene["type"] == "excalidraw" and scene["version"] == 2, "bad envelope"
    assert isinstance(scene.get("appState"), dict) and "files" in scene, "no appState/files"

    shapes = [e for e in els if e["type"] in SHAPES]
    arrows = [e for e in els if e["type"] == "arrow"]

    for s in shapes:
        tids = [b["id"] for b in s.get("boundElements", []) if b["type"] == "text"]
        assert tids, f"[{label}] {s['id']} has no bound text (empty-box bug)"
        for tid in tids:
            assert by_id[tid]["containerId"] == s["id"], f"[{label}] {tid} containerId mismatch"

    for t in [e for e in els if e["type"] == "text"]:
        if t["id"] == "title":
            continue
        cid = t.get("containerId")
        assert cid in by_id, f"[{label}] {t['id']} dangling containerId"
        assert t["id"] in [b["id"] for b in by_id[cid].get("boundElements", [])], \
            f"[{label}] {cid} does not back-ref {t['id']}"

    for a in arrows:
        for side in ("startBinding", "endBinding"):
            b = a[side]
            assert b and "focus" in b and "gap" in b, f"[{label}] {a['id']} bad {side}"
            tgt = b["elementId"]
            assert tgt in by_id, f"[{label}] {a['id']} {side} dangling → {tgt}"
            assert a["id"] in [r["id"] for r in by_id[tgt].get("boundElements", [])], \
                f"[{label}] {tgt} does not back-ref arrow {a['id']} (fake-edge bug)"

    def overlap(a, b):
        return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                    or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])
    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            assert not overlap(shapes[i], shapes[j]), \
                f"[{label}] overlap {shapes[i]['id']} & {shapes[j]['id']}"

    for e in els:
        for f in ("id", "type", "x", "y", "width", "height", "seed", "versionNonce"):
            assert f in e, f"[{label}] {e.get('id')} missing {f}"

    return len(shapes), len(arrows)


def case(name, fn):
    global _fail
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as exc:
        _fail += 1
        print(f"  FAIL  {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        _fail += 1
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Self-test excalidraw-gen.py")
    ap.add_argument("--gen", default=str(GEN_DEFAULT), help="path to excalidraw-gen.py")
    args = ap.parse_args(argv)
    gen = Path(args.gen)
    assert gen.exists(), f"generator not found: {gen}"

    print(f"Testing {gen}\n")

    # 1. Line DSL, LR, with shapes/colors/branch
    def t_dsl():
        spec = (
            "direction: LR\ntitle: Auth Flow\n"
            'node ok "Valid?" [diamond]\nnode db "Postgres" [rectangle:teal]\n'
            "gw -> auth: forward\nauth -> ok: check\nok -> db: valid\nok -> deny: invalid\n"
        )
        n, a = check_invariants(run(gen, spec), "dsl")
        assert n == 5 and a == 4, f"expected 5 nodes/4 edges, got {n}/{a}"
    case("line DSL — LR, diamond branch, colors", t_dsl)

    # 2. JSON, TB default
    def t_json():
        spec = json.dumps({
            "title": "Pipeline",
            "nodes": {"a": "Ingest", "b": "Transform", "c": "Load"},
            "edges": [{"from": "a", "to": "b", "label": "raw"},
                      {"from": "b", "to": "c", "label": "clean"}],
        })
        n, a = check_invariants(run(gen, spec), "json")
        assert n == 3 and a == 2
    case("JSON — TB default, labeled edges", t_json)

    # 3. JSON with nodes-as-list
    def t_list():
        spec = json.dumps({
            "nodes": [{"id": "x", "label": "X", "shape": "ellipse", "color": "purple"},
                      {"id": "y", "label": "Y"}],
            "edges": [["x", "y", "go"]],
        })
        check_invariants(run(gen, spec), "list")
    case("JSON — nodes as list, edge as tuple", t_list)

    # 4. Cycle (layering must terminate, not hang)
    def t_cycle():
        check_invariants(run(gen, "a -> b\nb -> c\nc -> a\n"), "cycle")
    case("cycle — a->b->c->a terminates", t_cycle)

    # 5. Diamond / fan-out / fan-in (crossing reduction + no overlap)
    def t_fan():
        spec = "root -> l\nroot -> r\nl -> join\nr -> join\njoin -> end\n"
        check_invariants(run(gen, spec), "fan")
    case("fan-out/fan-in — no overlaps", t_fan)

    # 6. Self-loop is skipped with a warning, not crash
    def t_selfloop():
        proc = subprocess.run(
            [sys.executable, str(gen), "--stdout"],
            input="a -> a\na -> b\n", capture_output=True, text=True,
        )
        assert proc.returncode == 0, "self-loop should not crash"
        assert "self-loop" in proc.stderr.lower(), "expected self-loop warning on stderr"
        scene = json.loads(proc.stdout)
        assert not [e for e in scene["elements"] if e["type"] == "arrow"
                    and e.get("startBinding", {}).get("elementId") == "node:a"
                    and e.get("endBinding", {}).get("elementId") == "node:a"], "self-loop arrow leaked"
    case("self-loop — skipped with warning", t_selfloop)

    # 7. --seed byte-reproducible
    def t_seed():
        s = "a -> b\nb -> c\n"
        p1 = subprocess.run([sys.executable, str(gen), "--stdout", "--seed", "42"],
                            input=s, capture_output=True, text=True).stdout
        p2 = subprocess.run([sys.executable, str(gen), "--stdout", "--seed", "42"],
                            input=s, capture_output=True, text=True).stdout
        assert p1 == p2 and p1, "same --seed must produce byte-identical output"
    case("--seed — byte-reproducible", t_seed)

    # 8. Direction override flag beats spec
    def t_override():
        s = "direction: TB\na -> b\n"
        scene = run(gen, s, "--lr")
        xs = [e["x"] for e in scene["elements"] if e["type"] in SHAPES]
        ys = [e["y"] for e in scene["elements"] if e["type"] in SHAPES]
        assert (max(xs) - min(xs)) > (max(ys) - min(ys)), "--lr should widen, not stack"
    case("--lr overrides spec direction:", t_override)

    # 9. Error cases fail non-zero, write nothing
    def t_errors():
        run(gen, 'node x "Foo" [hexagon]\nx -> y\n', expect_ok=False)     # bad shape
        run(gen, "", expect_ok=False)                                      # empty
        run(gen, "this is not a valid line\n", expect_ok=False)            # garbage
        run(gen, '{"nodes": {"a": "A"}, "edges": [{"to": "a"}]}', expect_ok=False)  # edge no from
    case("bad specs — fail non-zero", t_errors)

    # 10. Writing to a file actually produces a parseable .excalidraw
    def t_file():
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x.excalidraw"
            proc = subprocess.run([sys.executable, str(gen), "-o", str(out)],
                                  input="a -> b\n", capture_output=True, text=True)
            assert proc.returncode == 0 and out.exists(), "file not written"
            check_invariants(json.loads(out.read_text()), "file")
            # suffix coercion: give a bad suffix, expect .excalidraw
            out2 = Path(d) / "y.json"
            subprocess.run([sys.executable, str(gen), "-o", str(out2)],
                           input="a -> b\n", capture_output=True, text=True)
            assert (Path(d) / "y.excalidraw").exists(), "suffix not coerced to .excalidraw"
    case("file output — written + suffix coerced", t_file)

    print()
    if _fail:
        print(f"FAILED — {_fail} case(s) failed")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
