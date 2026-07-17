# Plugin-install contract (v3 — LOCKED against the seeded files)

> **Status: LOCKED.** The four shipped manifests are the canonical examples and the
> installer parses exactly this shape:
> `plugins/catalog.toml` + `plugins/{nexus-fleet,nexus-presence,nexus-observe,nexus-mission}/nexus.deps.toml`.
> Schema is owned here (the installer parses it); the *content* is owned by the plugin agent.
> **One source of truth per side:** this doc is the **schema** (field shapes + installer
> behavior); the four shipped `nexus.deps.toml` files are the authoritative **content**. The
> snippets below illustrate the schema only and may lag the files — read the files for real deps.
> If you change the shape, update this doc **and** the parser (`scripts/plugin-deps-resolve.sh`) together.
>
> **Note:** the nexus `catalog.toml` and any external Claude-plugin marketplace manifest are
> **separate** (different runtimes; see the `claude_marketplace` note below). Org-specific
> marketplace content stays in a private overlay, never in this public catalog.

## The flow it serves

```
base install (settings / hotkeys / substrate spawn chain)
        │
     plugins?  ──no──▶ FIN (the minimal trial)
        │ yes
   catalog multi-select   ← plugins/catalog.toml  (+ any private catalog.<org>.toml overlay, merged)
        │
   for each chosen plugin — route by which source key is present:
        ├─ install:  source.bundled              → scripts/herdr-plugin-install.sh <dir>   (link)
        │            source.remote               → herdr plugin install <owner/repo/subdir> (herdr marketplace)
        │            source.{marketplace,plugin} → IF that marketplace is already registered:
        │                                          claude plugin install <plugin>@<marketplace>  (auth-gated; never `marketplace add`)
        └─ read plugins/<dir>/nexus.deps.toml → for each [[requires]] + [[env]]:
              run CHECK ──exit 0?──▶ satisfied, continue
                        │ nonzero
              optional? ─true─▶ note the degrade (guide as FYI), continue
                        └false▶ show `guide`, re-check; still unsatisfied → BLOCK this plugin
        │
   [[env]]: probe → derive; else default; write to the profile .env (required+unresolved → block)
```

## Artifact 1 — `plugins/catalog.toml`

```toml
[[plugin]]
id          = "nexus.fleet"          # matches the plugin's herdr-plugin.toml id
name        = "Fleet"
description = "…one line for the multi-select…"
default     = true                   # pre-checked in the multi-select
source      = { bundled = "nexus-fleet" }          # exactly ONE of three forms:
#             { remote  = "owner/repo/subdir" }    #   → herdr plugin install <that>   (herdr plugin)
#             { marketplace = "<name>", plugin = "<name>" }   # (identified by the marketplace+plugin keys)
#                                                   #   → claude plugin install <plugin>@<marketplace>   (Claude Code plugin/skill)
#                                                   #     AUTH-GATED: only if <marketplace> is already registered locally; never `marketplace add`.
```

> **Three backends, one catalog, by runtime — not one merged file.** `bundled`/`remote` are herdr
> plugins (panes/actions/hooks); `claude_marketplace` installs Claude Code plugins/skills from a
> Claude plugin marketplace the user has registered. The nexus catalog *routes to* a marketplace,
> it never absorbs one — so a plugin's optional Claude-skill dep can become a self-serve one-liner.
>
> **Public repo — no org internals in the committed catalog.** This is a public repository. Do NOT
> hardcode an org's marketplace URL/name, internal repo names, or plugin/team names into the
> committed `catalog.toml` or orchestrator. Org-specific `claude_marketplace` entries belong in a
> **private/gitignored overlay catalog**, not here. Additionally, `claude_marketplace` installs are
> **auth-gated**: the orchestrator only attempts an install when that marketplace is *already
> registered* locally (`claude plugin marketplace list` contains it) — i.e. the user set it up and
> is entitled to it. A user without it registered sees nothing and no install is attempted.
>
> **Private overlay merge.** The org slice ships as a separate `plugins/catalog.<org>.toml` using
> the *same* `[[plugin]]` schema. The orchestrator, after reading `catalog.toml`, merges any
> sibling `catalog.*.toml` (excluding `*.example.toml`) it finds. `.gitignore` keeps the real
> overlay out of this public repo (`plugins/catalog.*.toml` minus `!*.example.toml`); a committed
> `catalog.<org>.example.toml` (placeholders only) documents the shape. Two independent gates mean
> zero leak: **file-presence** (the overlay only exists on org machines) and the per-entry
> **marketplace-registered** check. Outsider clone → no overlay file → sees only the bundled nexus
> plugins; org engineer → overlay + registered marketplace → sees and installs the org slice.

## Artifact 2 — `plugins/<dir>/nexus.deps.toml`

```toml
setup_guide = "optional plugin-level overview string"
# ORDERING RULE (TOML): any top-level key (setup_guide) MUST appear BEFORE the first
# [[requires]]/[[env]] header — otherwise TOML binds it INTO that table. The parser lints this.

[[requires]]                         # a thing that must (or optionally should) exist
type     = "bin"                     # bin | mcp | service | venv | file | plugin
id       = "fzf"
check    = "command -v fzf"          # shell probe; NON-ZERO exit ⇒ unsatisfied
optional = false                     # true ⇒ degrade (never block); false/omitted ⇒ block
guide    = "brew install fzf (mac) · sudo apt install fzf (linux)"   # the fix for THIS dep

[[env]]                              # an env var the plugin reads
name     = "DATABASE_URL"
required = false                     # true + unresolved ⇒ block
default  = ""                        # prefill
probe    = "grep -m1 '^DATABASE_URL=' \"$AGENTS_NEXUS_DIR/.env\" | cut -d= -f2-"  # auto-derive (optional)
describe = "…prompt/help text…"
```

### Semantics
- **`requires[].check`** — a shell command; **exit 0 = satisfied**, nonzero = unsatisfied. `type` classifies the probe (`bin`=`command -v`, `mcp`=`claude mcp get`, `service`=health probe, `venv`/`file`=`test -x`/`-f`, `plugin`=`herdr/claude plugin` present).
- **`requires[].optional`** — `true` degrades (surface `guide` as FYI, keep going — the plugin graceful-degrades); `false`/omitted **blocks** that plugin. A plugin with *only* optional requires (e.g. presence) must never block.
- **`env[].probe`** — a command to auto-derive the value; used before falling back to `default`. `required=true` + still-empty ⇒ block. `secret=true` (optional) hides input.

### Parser constraints (locked, so parsing stays trial-safe / dependency-free)
Values are **single-line scalars** — a `"quoted string"` (with `\"` escapes) or a bare `true|false`. No multiline strings, no value-arrays inside `[[requires]]`/`[[env]]`. `#` starts a comment. This lets the resolver parse in pure bash/awk (no python), so the fleet-only **trial parses with nothing but a shell**.

## Installer behavior contract

1. **Gate on the check, not prose** — run each `requires[].check`; satisfied → skip. Unsatisfied + `optional` → note the degrade + `guide`, continue. Unsatisfied + required → print `guide`, re-check; still unsatisfied → **block this plugin** (others proceed).
2. **All-optional never blocks** — matches the plugins' graceful-degrade (presence → terminal bell; observe → "DATABASE_URL not set" view).
3. **`check`→`guide` is the self-heal UX** — a service dep pairs a health probe with the fix, e.g. `check="mygw status | grep -qi healthy"`, `guide="mygw connect"`. That pair *is* the reconnect prompt the user sees when the service is down (an optional dep so a transient outage never blocks the install).
4. **Idempotent** — relink replaces the plugin's key block; env prompts prefill from the existing `.env`; `probe` skips already-resolved vars.
5. **`--trial` / non-interactive** — install only `default=true` plugins, prompt nothing (probe/default/blank). No-plugins = FIN.
6. **Lint** — warn if a top-level key (e.g. `setup_guide`) was authored *after* a `[[…]]` header (TOML would have swallowed it into that table).

## Changelog v2 → v3 (what the seed changed, and why I adopted it)
- **`source = { bundled | remote }`** tagged union (was `install=` + flat `path`/`repo`). Cleaner, one field.
- **Dropped the central `[[thing]]` table; `requires` is inline per plugin.** My v2 pushed DRY (define `memory-stack` once, reference by id). **Reversed** — a marketplace's *remote* plugin ships a self-contained `nexus.deps.toml` and can't reference *my* catalog's things. Portability > DRY. (Two plugins repeating a small `check` is fine, and their checks can legitimately differ — observe read-access vs mission write.)
- **`optional` polarity** (was `required`) on `requires` — omitted ⇒ block (safe default for a declared dep).
- **`type = "file"`** added — a required path that isn't a PATH binary (e.g. a script in the repo, or a cloned sibling repo).
- **per-`requires` `guide`** — the fix for *that* dep, not one plugin-level blob (`setup_guide` stays as the optional overview).

## Ownership
- **Plugin agent leads:** `catalog.toml` + each `nexus.deps.toml` content, the real `check`/`guide` probes, the publish/marketplace roadmap.
- **Installer (here) owns:** this schema, the parser (`scripts/plugin-deps-resolve.sh`), and the orchestration (catalog select → install → check-gated resolve → env → profile `.env`).
