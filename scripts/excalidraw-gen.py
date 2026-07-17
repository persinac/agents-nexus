#!/usr/bin/env python3
"""excalidraw-gen.py — turn a boxes-and-arrows spec into a valid .excalidraw file.

Why this exists: hand-emitting Excalidraw JSON one element at a time (the way the
old MCP worked) is slow and error-prone. The .excalidraw format has invariants
that are painful to satisfy incrementally — bound text is a two-sided link
(container.boundElements <-> text.containerId), a real connection is a binding
(arrow.startBinding/endBinding + reciprocal entries on both shapes), and one
malformed element fails the whole load. Emitting elements out of the loop, with
those invariants owned in ONE place, is what makes the output correct every time.

The caller never specifies coordinates. It emits intent (nodes + edges); this
script runs a layered (Sugiyama-style) auto-layout, wires bound text on both
sides, wires bound arrows on both sides, and validates every invariant before
writing. That kills the three classic failure modes: missing labels, arrows
that look connected but aren't, and files that won't open.

Stdlib only (json, argparse, pathlib, math, random, re, time) — runs under the
bare system python3 with no install step, like agent-ledger.py.

Routing note: arrows are drawn straight, center-to-center. This is clean for
linear/tree flows; a long back-edge (e.g. in a cycle) can cross intervening
boxes. Nudge such edges by hand after opening, or see the "elbow routing"
follow-up if that becomes common.

Input is auto-detected as JSON or a compact line DSL.

  Line DSL:
      # comments and blank lines are ignored
      direction: LR                 # optional; TB (default) or LR
      title: My Flow                # optional standalone title text
      node gw   "API Gateway"       # optional: give a node a nicer label...
      node ok   "Valid?" [diamond]  # ...and optionally a shape (rectangle|diamond|ellipse)
      gw -> auth: validates token   # edge; undeclared nodes are auto-created
      auth -> db: read user

  JSON (equivalent):
      {
        "direction": "LR",
        "title": "My Flow",
        "nodes": {"gw": "API Gateway", "auth": "Auth Service", "db": "Postgres"},
        "edges": [
          {"from": "gw", "to": "auth", "label": "validates token"},
          {"from": "auth", "to": "db", "label": "read user"}
        ]
      }
      # nodes may also be a list: [{"id":"ok","label":"Valid?","shape":"diamond","color":"amber"}]

Usage:
    excalidraw-gen.py SPEC_FILE -o out.excalidraw
    excalidraw-gen.py -o out.excalidraw < spec.txt        # spec on stdin
    cat spec.json | excalidraw-gen.py --stdout            # print, don't write
    excalidraw-gen.py spec.txt -o out.excalidraw --lr --seed 7

Flags:
    -o, --output PATH   Where to write the .excalidraw file.
    --stdout            Print the JSON to stdout instead of writing a file.
    --direction TB|LR   Flow direction (overrides a `direction:` in the spec).
    --lr                Shorthand for --direction LR.
    --seed N            Seed RNG so seed/versionNonce are reproducible.
    --no-validate       Skip the invariant checks (not recommended).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from pathlib import Path

# ── Layout constants ──────────────────────────────────────────────────────────
H_GAP = 60          # gap between sibling nodes within a layer (cross-axis)
V_GAP = 90          # gap between layers (flow-axis)
MARGIN = 60         # canvas padding around the whole diagram
MIN_W, MIN_H = 140, 64
PAD_X, PAD_Y = 26, 20
CHAR_W = 0.60       # width per char at fontSize 1 (Virgil overestimate → no overflow)
LINE_H = 1.25       # excalidraw text lineHeight
FONT_SIZE = 20
LABEL_FONT_SIZE = 16   # arrow labels
TITLE_FONT_SIZE = 28
BARY_SWEEPS = 4     # crossing-reduction passes

# ── Aesthetic constants (match the native Excalidraw palette) ─────────────────
STROKE = "#1e1e1e"
ARROW_STROKE = "#1e1e1e"
FONT_FAMILY = 1     # 1 = Virgil (hand-drawn), the look Excalidraw is loved for
# Named fills → (backgroundColor, strokeColor). Default is a soft blue.
PALETTE = {
    "blue":   ("#a5d8ff", "#1971c2"),
    "green":  ("#b2f2bb", "#2f9e44"),
    "amber":  ("#ffd8a8", "#e8590c"),
    "orange": ("#ffd8a8", "#e8590c"),
    "purple": ("#d0bfff", "#7048e8"),
    "red":    ("#ffc9c9", "#e03131"),
    "yellow": ("#fff3bf", "#f08c00"),
    "teal":   ("#c3fae8", "#0ca678"),
    "pink":   ("#eebefa", "#ae3ec9"),
    "gray":   ("#e9ecef", "#495057"),
    "grey":   ("#e9ecef", "#495057"),
    "none":   ("transparent", "#1e1e1e"),
}
DEFAULT_COLOR = "blue"
VALID_SHAPES = {"rectangle", "diamond", "ellipse"}

_RNG = random.Random()
_UPDATED = int(time.time() * 1000)   # pinned per-run; overridden when --seed is set


# ── Spec parsing ──────────────────────────────────────────────────────────────
class Spec:
    """Normalized diagram intent: ordered nodes, edges, direction, title."""

    def __init__(self):
        self.direction = "TB"
        self.title = None
        self.node_order = []                 # preserves declaration/first-seen order
        self.nodes = {}                      # id -> {"label", "shape", "color"}
        self.edges = []                      # [{"from","to","label"}]

    def _ensure(self, nid):
        if nid not in self.nodes:
            self.nodes[nid] = {"label": nid, "shape": "rectangle", "color": DEFAULT_COLOR}
            self.node_order.append(nid)
        return self.nodes[nid]

    def set_node(self, nid, label=None, shape=None, color=None):
        n = self._ensure(nid)
        if label is not None:
            n["label"] = label
        if shape:
            if shape not in VALID_SHAPES:
                raise ValueError(f"unknown shape {shape!r} for node {nid!r} "
                                 f"(use {', '.join(sorted(VALID_SHAPES))})")
            n["shape"] = shape
        if color:
            if color not in PALETTE:
                raise ValueError(f"unknown color {color!r} for node {nid!r} "
                                 f"(use {', '.join(sorted(PALETTE))})")
            n["color"] = color

    def add_edge(self, src, dst, label=None):
        self._ensure(src)
        self._ensure(dst)
        self.edges.append({"from": src, "to": dst, "label": label})


def parse_spec(text: str) -> Spec:
    text = text.strip()
    if not text:
        raise ValueError("empty spec")
    if text[0] in "{[":
        return _parse_json(text)
    return _parse_dsl(text)


def _parse_json(text: str) -> Spec:
    data = json.loads(text)
    spec = Spec()
    spec.direction = str(data.get("direction", "TB")).upper()
    spec.title = data.get("title")

    nodes = data.get("nodes", {})
    if isinstance(nodes, dict):
        for nid, label in nodes.items():
            if isinstance(label, dict):
                spec.set_node(nid, label.get("label", nid),
                              label.get("shape"), label.get("color"))
            else:
                spec.set_node(nid, str(label))
    elif isinstance(nodes, list):
        for n in nodes:
            if isinstance(n, str):
                spec.set_node(n)
            else:
                nid = n.get("id") or n.get("name")
                if not nid:
                    raise ValueError(f"node missing id: {n!r}")
                spec.set_node(nid, n.get("label", nid), n.get("shape"), n.get("color"))
    elif nodes:
        raise ValueError("`nodes` must be an object or a list")

    for e in data.get("edges", []):
        if isinstance(e, (list, tuple)):
            src, dst = e[0], e[1]
            label = e[2] if len(e) > 2 else None
        else:
            src = e.get("from") or e.get("source") or e.get("src")
            dst = e.get("to") or e.get("target") or e.get("dst")
            label = e.get("label")
        if not src or not dst:
            raise ValueError(f"edge missing from/to: {e!r}")
        spec.add_edge(src, dst, label)
    return spec


_NODE_RE = re.compile(r'^node\s+(\S+)\s+(.*)$')
_EDGE_RE = re.compile(r'^(\S+)\s*-+>\s*([^\s:]+)\s*(?::\s*(.*))?$')
_SHAPE_TAG_RE = re.compile(r'\[([a-z:]+)\]\s*$')


def _parse_dsl(text: str) -> Spec:
    spec = Spec()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        low = line.lower()
        if low.startswith("direction:"):
            spec.direction = line.split(":", 1)[1].strip().upper()
            continue
        if low.startswith("title:"):
            spec.title = line.split(":", 1)[1].strip()
            continue

        m = _NODE_RE.match(line)
        if m:
            nid, rest = m.group(1), m.group(2).strip()
            shape = color = None
            tag = _SHAPE_TAG_RE.search(rest)
            if tag:                                    # trailing [shape] or [shape:color]
                parts = tag.group(1).split(":")
                shape = parts[0] or None
                color = parts[1] if len(parts) > 1 else None
                rest = rest[:tag.start()].strip()
            label = _unquote(rest).replace("\\n", "\n")
            try:
                spec.set_node(nid, label or nid, shape, color)
            except ValueError as exc:
                raise ValueError(f"line {lineno}: {exc}") from None
            continue

        m = _EDGE_RE.match(line)
        if m:
            src, dst, label = m.group(1), m.group(2), m.group(3)
            if label is not None:
                label = _unquote(label.strip()).replace("\\n", "\n") or None
            spec.add_edge(src, dst, label)
            continue

        raise ValueError(f"line {lineno}: cannot parse {line!r} "
                         f"(expected `node ...`, `a -> b`, `direction:`, or `title:`)")
    if not spec.nodes:
        raise ValueError("spec declared no nodes or edges")
    return spec


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


# ── Layout (layered / Sugiyama-lite) ──────────────────────────────────────────
def _measure(text: str, font_size: int):
    lines = text.split("\n") or [""]
    w = max((len(l) for l in lines), default=1) * font_size * CHAR_W
    h = len(lines) * font_size * LINE_H
    return w, h


def _acyclic_edges(nodes, edges, node_order):
    """Return the edge set with cycle back-edges removed (they still draw; they
    just don't count toward layering). Back-edges are found by DFS: an edge into
    a node currently on the recursion stack is a back-edge. Without this, a cycle
    makes longest-path layering diverge into sparse, gap-ridden layer numbers."""
    succ = {n: [] for n in nodes}
    for u, v in edges:
        succ[u].append(v)
    WHITE, GREY, BLACK = 0, 1, 2
    state = {n: WHITE for n in nodes}
    back = set()

    def visit(root):
        stack = [(root, iter(succ[root]))]
        state[root] = GREY
        while stack:
            u, it = stack[-1]
            for v in it:
                if state[v] == GREY:              # edge into an ancestor → back-edge
                    back.add((u, v))
                elif state[v] == WHITE:
                    state[v] = GREY
                    stack.append((v, iter(succ[v])))
                    break
            else:
                state[u] = BLACK
                stack.pop()

    for n in node_order:                          # deterministic root order
        if state[n] == WHITE:
            visit(n)
    return [(u, v) for (u, v) in edges if (u, v) not in back]


def _assign_layers(nodes, edges, node_order):
    """Longest-path layering over the acyclic edge set. Layers are contiguous
    0..max (each layer k>0 has a predecessor at k-1), so ordering never gaps."""
    real = [(e["from"], e["to"]) for e in edges if e["from"] != e["to"]]
    dag = _acyclic_edges(nodes, real, node_order)
    layer = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for u, v in dag:
            if layer[v] < layer[u] + 1:
                layer[v] = layer[u] + 1
                changed = True
        if not changed:
            break
    return layer


def _order_layers(spec, layer):
    succ, pred = {n: [] for n in spec.nodes}, {n: [] for n in spec.nodes}
    for e in spec.edges:
        if e["from"] != e["to"]:
            succ[e["from"]].append(e["to"])
            pred[e["from"] if False else e["to"]].append(e["from"])

    layers = {}
    for n in spec.node_order:                    # stable initial order
        layers.setdefault(layer[n], []).append(n)
    max_l = max(layers) if layers else 0
    for l in range(max_l + 1):                    # guarantee contiguous keys
        layers.setdefault(l, [])
    pos = {n: i for l in layers for i, n in enumerate(layers[l])}

    def bary(n, neighbors, want_layer):
        idx = [pos[x] for x in neighbors[n] if layer[x] == want_layer]
        return sum(idx) / len(idx) if idx else pos[n]

    for _ in range(BARY_SWEEPS):
        for l in range(1, max_l + 1):            # down-sweep: order by predecessors
            layers[l].sort(key=lambda n: bary(n, pred, l - 1))
            for i, n in enumerate(layers[l]):
                pos[n] = i
        for l in range(max_l - 1, -1, -1):       # up-sweep: order by successors
            layers[l].sort(key=lambda n: bary(n, succ, l + 1))
            for i, n in enumerate(layers[l]):
                pos[n] = i
    return layers, max_l


def layout(spec):
    """Return {node_id: (x, y, w, h)} with no overlaps, flowing TB or LR."""
    tb = spec.direction != "LR"
    dims = {}
    for nid, n in spec.nodes.items():
        tw, th = _measure(n["label"], FONT_SIZE)
        w = max(MIN_W, math.ceil(tw + 2 * PAD_X))
        h = max(MIN_H, math.ceil(th + 2 * PAD_Y))
        dims[nid] = (w, h)

    layer = _assign_layers(spec.nodes, spec.edges, spec.node_order)
    layers, max_l = _order_layers(spec, layer)

    # flow-axis extent per node, cross-axis extent per node
    def flow_ext(nid):  return dims[nid][1] if tb else dims[nid][0]
    def cross_ext(nid): return dims[nid][0] if tb else dims[nid][1]

    # place along the flow axis, layer by layer
    layer_flow = {}
    acc = 0
    for l in range(max_l + 1):
        layer_flow[l] = acc
        band = max((flow_ext(n) for n in layers.get(l, [])), default=0)
        acc += band + V_GAP

    boxes = {}  # nid -> (major_flow_start, cross_start, w, h) in (flow, cross) space
    for l in range(max_l + 1):
        row = layers.get(l, [])
        band = max((flow_ext(n) for n in row), default=0)
        total = sum(cross_ext(n) for n in row) + H_GAP * max(0, len(row) - 1)
        cross = -total / 2.0
        for nid in row:
            w, h = dims[nid]
            f = layer_flow[l] + (band - flow_ext(nid)) / 2.0   # center in the band
            boxes[nid] = (f, cross, w, h)
            cross += cross_ext(nid) + H_GAP

    # map (flow, cross) → (x, y), then shift so the top-left sits at MARGIN
    placed = {}
    for nid, (f, c, w, h) in boxes.items():
        x, y = (c, f) if tb else (f, c)
        placed[nid] = [x, y, w, h]
    min_x = min(p[0] for p in placed.values())
    min_y = min(p[1] for p in placed.values())
    for p in placed.values():
        p[0] = round(p[0] - min_x + MARGIN)
        p[1] = round(p[1] - min_y + MARGIN)
    return {nid: tuple(p) for nid, p in placed.items()}


# ── Excalidraw element assembly ───────────────────────────────────────────────
def _nonce():
    return _RNG.randint(0, 2**31 - 1)


def _base(el_id, el_type, x, y, w, h, **extra):
    el = {
        "id": el_id,
        "type": el_type,
        "x": float(x),
        "y": float(y),
        "width": float(w),
        "height": float(h),
        "angle": 0,
        "strokeColor": STROKE,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": _nonce(),
        "version": 1,
        "versionNonce": _nonce(),
        "isDeleted": False,
        "boundElements": [],
        "updated": _UPDATED,
        "link": None,
        "locked": False,
    }
    el.update(extra)
    return el


def _text_el(el_id, text, cx, cy, container_id=None, font_size=FONT_SIZE,
             color=STROKE):
    tw, th = _measure(text, font_size)
    el = _base(
        el_id, "text", cx - tw / 2, cy - th / 2, math.ceil(tw), math.ceil(th),
        strokeColor=color,
        text=text,
        fontSize=font_size,
        fontFamily=FONT_FAMILY,
        textAlign="center",
        verticalAlign="middle",
        containerId=container_id,
        originalText=text,
        lineHeight=LINE_H,
        autoResize=True,
    )
    return el


def build_scene(spec, positions):
    tb = spec.direction != "LR"
    elements = []
    by_id = {}

    # shapes + bound text (both sides of the link wired here, once)
    for nid in spec.node_order:
        n = spec.nodes[nid]
        x, y, w, h = positions[nid]
        bg, stroke = PALETTE[n["color"]]
        shape_id = f"node:{nid}"
        text_id = f"text:{nid}"
        shape = _base(
            shape_id, n["shape"], x, y, w, h,
            backgroundColor=bg,
            strokeColor=stroke,
            roundness=({"type": 3} if n["shape"] == "rectangle" else None),
            boundElements=[{"type": "text", "id": text_id}],
        )
        text = _text_el(text_id, n["label"], x + w / 2, y + h / 2,
                        container_id=shape_id)
        elements.append(shape)
        elements.append(text)
        by_id[shape_id] = shape

    # arrows + bound labels; reciprocal boundElements on the shapes
    for i, e in enumerate(spec.edges):
        if e["from"] == e["to"]:
            print(f"warning: skipping self-loop {e['from']} -> {e['to']} "
                  f"(not supported)", file=sys.stderr)
            continue
        sx, sy, sw, sh = positions[e["from"]]
        tx, ty, tw, th = positions[e["to"]]
        gap = 6.0
        if tb:
            start = (sx + sw / 2, sy + sh)
            end = (tx + tw / 2, ty)
        else:
            start = (sx + sw, sy + sh / 2)
            end = (tx, ty + th / 2)
        ax, ay = start
        dx, dy = end[0] - start[0], end[1] - start[1]
        arrow_id = f"arrow:{i}"
        src_id, dst_id = f"node:{e['from']}", f"node:{e['to']}"
        arrow = _base(
            arrow_id, "arrow", ax, ay, abs(dx), abs(dy),
            strokeColor=ARROW_STROKE,
            points=[[0.0, 0.0], [float(dx), float(dy)]],
            lastCommittedPoint=None,
            startBinding={"elementId": src_id, "focus": 0.0, "gap": gap},
            endBinding={"elementId": dst_id, "focus": 0.0, "gap": gap},
            startArrowhead=None,
            endArrowhead="arrow",
            elbowed=False,
        )
        by_id[src_id]["boundElements"].append({"type": "arrow", "id": arrow_id})
        by_id[dst_id]["boundElements"].append({"type": "arrow", "id": arrow_id})
        elements.append(arrow)

        if e.get("label"):
            lbl_id = f"arrowlabel:{i}"
            mx, my = ax + dx / 2, ay + dy / 2
            label = _text_el(lbl_id, e["label"], mx, my, container_id=arrow_id,
                             font_size=LABEL_FONT_SIZE)
            arrow["boundElements"].append({"type": "text", "id": lbl_id})
            elements.append(label)

    # optional free-standing title above the diagram
    if spec.title:
        min_x = min(p[0] for p in positions.values())
        min_y = min(p[1] for p in positions.values())
        max_x = max(p[0] + p[2] for p in positions.values())
        cx = (min_x + max_x) / 2
        title = _text_el("title", spec.title, cx, min_y - 48,
                         font_size=TITLE_FONT_SIZE)
        elements.insert(0, title)

    return {
        "type": "excalidraw",
        "version": 2,
        "source": "agents-nexus/scripts/excalidraw-gen.py",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }


# ── Validation — the guardrail that makes bad output impossible ────────────────
def validate(scene):
    """Assert every load-critical invariant. Raises AssertionError on any break."""
    assert scene.get("type") == "excalidraw", "envelope: type != 'excalidraw'"
    assert scene.get("version") == 2, "envelope: version != 2"
    assert isinstance(scene.get("elements"), list), "envelope: elements not a list"
    assert isinstance(scene.get("appState"), dict), "envelope: appState missing"

    els = scene["elements"]
    by_id = {}
    for el in els:
        for f in ("id", "type", "x", "y", "width", "height"):
            assert f in el, f"element missing required field {f!r}: {el.get('id')}"
        assert el["id"] not in by_id, f"duplicate element id {el['id']!r}"
        by_id[el["id"]] = el

    for el in els:
        # bound text: both sides must agree
        if el["type"] == "text" and el.get("containerId"):
            cid = el["containerId"]
            assert cid in by_id, f"text {el['id']} → missing container {cid}"
            refs = [b.get("id") for b in by_id[cid].get("boundElements") or []]
            assert el["id"] in refs, \
                f"container {cid} does not back-reference text {el['id']}"
        # arrow bindings: endpoint must exist and back-reference the arrow
        if el["type"] == "arrow":
            for side in ("startBinding", "endBinding"):
                b = el.get(side)
                if not b:
                    continue
                tid = b["elementId"]
                assert tid in by_id, f"arrow {el['id']} {side} → missing {tid}"
                refs = [r.get("id") for r in by_id[tid].get("boundElements") or []]
                assert el["id"] in refs, \
                    f"shape {tid} does not back-reference arrow {el['id']}"
        # any boundElements entry must resolve
        for b in el.get("boundElements") or []:
            assert b.get("id") in by_id, \
                f"{el['id']} boundElements → missing {b.get('id')}"
    return True


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate a valid .excalidraw flowchart from a boxes+arrows spec.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("spec", nargs="?", help="spec file (JSON or line DSL); omit to read stdin")
    ap.add_argument("-o", "--output", help="write the .excalidraw file to this path")
    ap.add_argument("--stdout", action="store_true", help="print JSON to stdout instead of writing a file")
    ap.add_argument("--direction", choices=["TB", "LR"], help="flow direction (overrides the spec)")
    ap.add_argument("--lr", action="store_true", help="shorthand for --direction LR")
    ap.add_argument("--seed", type=int, help="seed RNG for reproducible seed/versionNonce")
    ap.add_argument("--no-validate", action="store_true", help="skip invariant checks")
    args = ap.parse_args(argv)

    if args.seed is not None:
        _RNG.seed(args.seed)
        global _UPDATED
        _UPDATED = args.seed        # pin timestamp too so --seed is byte-reproducible

    raw = Path(args.spec).read_text() if args.spec else sys.stdin.read()
    try:
        spec = parse_spec(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        ap.error(f"could not parse spec: {exc}")

    if args.lr:
        spec.direction = "LR"
    if args.direction:
        spec.direction = args.direction
    if spec.direction not in ("TB", "LR"):
        ap.error(f"direction must be TB or LR, got {spec.direction!r}")

    positions = layout(spec)
    scene = build_scene(spec, positions)

    if not args.no_validate:
        try:
            validate(scene)
        except AssertionError as exc:
            print(f"error: generated scene failed validation: {exc}", file=sys.stderr)
            return 1

    payload = json.dumps(scene, indent=2)
    if args.stdout or not args.output:
        print(payload)
    if args.output:
        out = Path(args.output)
        if out.suffix != ".excalidraw":
            out = out.with_suffix(".excalidraw")
        out.write_text(payload)
        n_nodes = len(spec.nodes)
        n_edges = sum(1 for e in spec.edges if e["from"] != e["to"])
        print(f"wrote {out}  ({n_nodes} nodes, {n_edges} edges, {spec.direction})",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
