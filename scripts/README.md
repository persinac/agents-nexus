# Utilities

Standalone, dependency-free tools that any agent can invoke directly.

### `excalidraw-gen.py SPEC -o out.excalidraw`

Generate a valid `.excalidraw` flowchart from a boxes-and-arrows spec. The caller
describes nodes and edges (no coordinates); the script runs a layered auto-layout,
wires bound text and bound arrows correctly on **both** sides of each link, and
validates every load-critical invariant before writing. This eliminates the three
classic hand-authoring failures: empty boxes (missing bound text), arrows that look
connected but aren't (missing reciprocal bindings), and files that won't open
(malformed envelope).

Input is auto-detected as **JSON** or a compact **line DSL**:

```bash
python3 excalidraw-gen.py -o auth.excalidraw <<'EOF'
direction: LR
title: Auth Flow
node ok "Valid?" [diamond]
node db "Postgres" [rectangle:teal]
gw -> auth: forward
auth -> ok: check token
ok -> db: valid
ok -> deny: invalid
EOF
```

Flags: `-o/--output`, `--stdout`, `--direction TB|LR` (or `--lr`), `--seed N`
(byte-reproducible output), `--no-validate`. Stdlib-only; runs under the bare
system `python3`. Open the result at excalidraw.com or in the Excalidraw editor —
it's fully editable, not a flat image. Also exposed as the `excalidraw-diagram`
skill (`skills/excalidraw-diagram/SKILL.md`).

---

# GitHub Reconciliation & Repo Manifest

Scripts for discovering local coding projects, pushing them to GitHub, and
building a tagged manifest for Spark indexing.

## Pipeline

Run these in order. Each script is idempotent — re-running only adds new
discoveries, never duplicates.

```
find-repos.py          find local git repos
      |
extract-urls.py        extract GitHub URLs from found repos
      |
find-ungit-projects.py find local projects without .git
      |
init-github-repos.py   git init + create private GitHub repos
      |                 (also appends to clone-urls.txt)
      v
clone-urls.txt         single source of truth for all repo URLs
      |
build-manifest.py      generate repos-manifest.yaml with rule-based tags
      |
ai-tag-repos.py        enrich manifest with AI-generated tech stack tags
      |
      v
repos-manifest.yaml    final manifest for Spark indexing
```

## Scripts

### `find-repos.py [root]`

Recursively finds directories containing `.git`. Logs discoveries to
`found-repos.log`. Skips `.worktrees` and common junk dirs. Default root:
`C:/projects`.

### `extract-urls.py`

Reads `found-repos.log`, runs `git remote get-url origin` on each, and
appends GitHub URLs to `clone-urls.txt`. Dedupes on URL.

### `find-ungit-projects.py [root]`

Finds directories that look like coding projects (by marker files like
`package.json`, `pyproject.toml`, `Dockerfile`, etc.) but have no `.git`.
Excludes anything already in `found-repos.log`. Logs to
`found-ungit-projects.log`.

### `init-github-repos.py`

For each project in `found-ungit-projects.log`:

1. Checks for `.venv`, `node_modules`, `.idea`, etc. and offers to create a
   `.gitignore` before committing
2. Runs `git init` + initial commit
3. Creates a private GitHub repo via `gh repo create`
4. Appends the new URL to `clone-urls.txt`

Skips repos that already exist on GitHub. Requires the `gh` CLI.

### `build-manifest.py [--repos-dir PATH]`

Reads `clone-urls.txt` and generates `repos-manifest.yaml` at the repo root.
Each entry gets:

- **name** — derived from the URL
- **url** — clone URL
- **tags** — rule-based categories (personal projects, reference repos, etc.)
- **owner** — `personal` or `community`

Rule-based categories include: `example-org`, `example-org`, `destiny-2`,
`jcp-barbell-club`, `homelab`, `data-engineering`, `reference`, and more.

If `--repos-dir` is provided (or common locations like `C:/projects` exist),
the script also scans local repos for tech stack signals (language files,
framework configs, infra tools) and merges those into tags.

Re-running preserves manual tag edits in the YAML.

### `ai-tag-repos.py [--repos-dir PATH] [--dry-run] [--force]`

Enriches the manifest with AI-generated tags via Claude Haiku. For each repo:

- If a local clone exists, sends the file listing (2 levels deep) to Haiku
- If no local clone, sends the repo name + URL + existing tags
- Haiku returns 3-8 tags from a controlled vocabulary (~100 tags covering
  languages, frameworks, infra, security, IoT, AI, etc.)

Tags are stored in a separate `ai_tags` field so they don't overwrite
rule-based tags. Skips already-tagged repos unless `--force` is passed.

Requires `ANTHROPIC_API_KEY` in the environment or in the project `.env` file.

## Data Files

| File | Tracked | Description |
|------|---------|-------------|
| `found-repos.log` | No | Local paths of discovered git repos |
| `found-ungit-projects.log` | No | Local paths of projects without git |
| `clone-urls.txt` | No | GitHub URLs — single source of truth |
| `repos-manifest.yaml` | Yes | Tagged manifest for Spark |

## Cloning to the Mini PC

After running the pipeline, use `clone-urls.txt` on the mini PC:

```bash
# Personal repos
while read -r url; do
  name=$(basename "$url" .git)
  [ -d "$HOME/repos/$name" ] || git clone "$url" "$HOME/repos/$name"
done < clone-urls.txt

# Reference repos (community)
bash clone-reference-repos.sh ~/repos/reference
```
