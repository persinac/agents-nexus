---
name: excalidraw-diagram
description: Generate a valid .excalidraw flowchart file from a boxes-and-arrows spec. The caller describes nodes and edges (no coordinates); the tool auto-lays-out the diagram, wires bound text and bound arrows correctly, validates every invariant, writes a .excalidraw file, and can then upload it for a shareable excalidraw.com link. Invoke when the user wants a diagram, flowchart, architecture sketch, or boxes-and-arrows visual as an editable Excalidraw file.
user-invocable: true
allowed-tools: Bash, Write, Read, mcp__excalidraw__export_to_excalidraw
argument-hint: "[description of the diagram] or [path to a spec file]"
---

# Generate an Excalidraw Diagram

Turn a high-level boxes-and-arrows description into a valid `.excalidraw` file using
`scripts/excalidraw-gen.py`. The generator owns all the fiddly Excalidraw
invariants (bound text on both sides, real arrow bindings, strict envelope) so the
output always opens and never has empty boxes or detached arrows.

**Generator:** `!`echo "$(git rev-parse --show-toplevel 2>/dev/null || echo .)/scripts/excalidraw-gen.py"``

## How it works

You emit *intent* — nodes and edges. You never write coordinates; the script runs a
layered auto-layout (top-to-bottom by default, or left-to-right). Two input formats,
auto-detected:

**Line DSL** (fewest tokens — prefer this):
```
direction: LR                 # optional: TB (default) or LR
title: Auth Flow              # optional standalone title
node gw   "API Gateway"       # optional: nicer label for a node id
node ok   "Valid?" [diamond]  # optional shape: rectangle (default), diamond, ellipse
node db   "Postgres" [rectangle:teal]   # optional [shape:color]
gw -> auth: forward           # edge with a label
auth -> ok: check token       # undeclared nodes (auth) are auto-created
ok -> db: valid
ok -> deny: invalid
```

**JSON** (for programmatic callers):
```json
{
  "direction": "TB",
  "title": "Pipeline",
  "nodes": {"a": "Ingest", "b": "Transform", "c": "Load"},
  "edges": [
    {"from": "a", "to": "b", "label": "raw"},
    {"from": "b", "to": "c", "label": "clean"}
  ]
}
```
`nodes` may also be a list of `{"id","label","shape","color"}`. Colors: blue
(default), green, amber, purple, red, yellow, teal, pink, gray, none.

## Instructions

1. Translate the user's request into a spec. Keep node ids short (`gw`, `db`); put
   readable text in the label. Use `[diamond]` for decisions, `LR` for wide/linear
   flows and `TB` for tall hierarchies. Keep arrow labels short — long ones crowd.

2. Write the spec to a temp file (e.g. `/tmp/diagram.spec`), then run the generator.
   Pick an output path the user will find (default to the cwd unless they say where):

   ```bash
   GEN="$(git rev-parse --show-toplevel 2>/dev/null || echo .)/scripts/excalidraw-gen.py"
   python3 "$GEN" /tmp/diagram.spec -o ./diagram.excalidraw
   ```

   Or pipe the spec on stdin:
   ```bash
   python3 "$GEN" -o ./diagram.excalidraw <<'EOF'
   a -> b: step one
   b -> c: step two
   EOF
   ```

3. The script prints a one-line summary to stderr (`wrote ... (N nodes, N edges, DIR)`)
   and validates the file before writing. If validation fails it exits non-zero and
   writes nothing — surface the error rather than claiming success.

4. **Upload for a shareable link (preferred way to share).** Hand the generated
   scene JSON to the `mcp__excalidraw__export_to_excalidraw` tool — it uploads to
   excalidraw.com and returns a shareable URL. Minify the file so the tool call stays
   compact, then pass its full JSON as the `json` argument:

   ```bash
   python3 -c "import json; print(json.dumps(json.load(open('./diagram.excalidraw')),separators=(',',':')))"
   ```

   Then call `export_to_excalidraw(json=<that JSON>)` and give the user the returned URL.
   Caveats: the upload is **public** (anyone with the link can view) — skip it for
   sensitive content; and the JSON goes inline in the tool call, so it's heavy for very
   large diagrams.

   > Use the Excalidraw MCP **only** for this upload step. Do NOT author diagrams
   > element-by-element via the MCP — that path is unreliable; the generator above owns
   > correctness (bindings, envelope, validation).

5. Also give the user the local file path. It's a real editable `.excalidraw` — they
   can open it at excalidraw.com (File → Open) or the Excalidraw VS Code / desktop app.
   (This tool does not render PNG/SVG; export from Excalidraw if a raster is needed.)

## Notes

- **Stdlib only** — runs under the bare system `python3`, no install step.
- **Deterministic with `--seed N`** — same spec + same seed = byte-identical file.
- **Self-loops** (`a -> a`) are skipped with a warning (not supported by the layout).
- Direction can be overridden without touching the spec: `--direction LR` / `--lr`.
- Sequence diagrams are out of scope; this is boxes-and-arrows flowcharts.
