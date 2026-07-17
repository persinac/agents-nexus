# Building a private overlay for agents-nexus

The public `agents-nexus` core runs standalone. Everything org- or person-specific is a
**seam** — an empty or generic default the core ships with. An **overlay** is a *private*
repo that fills those seams: your internal automation scripts, launchd/systemd units,
marketplace catalog, and a reporting config with real targets.

`scripts/overlay-apply.sh <git-url|path>` fetches an overlay and layers it into your core
checkout. This directory is a **worked example** of an overlay repo's shape — copy it into a
fresh **private** repo and fill it in.

> Your overlay repo is PRIVATE. It carries identifiers (internal service names, marketplace
> URLs, account IDs, home paths). It must never be public. The core, by contrast, stays
> clean: overlay files are dropped at untracked paths and recorded in `.git/info/exclude`,
> so they can never be re-committed or re-exported from the core.

## Layout

```
your-overlay-repo/
├── overlay.toml          # the manifest: what to copy, symlink, template, and set
└── files/                # mirrors paths under the CORE repo root
    ├── scripts/…             → copied to  <core>/scripts/…
    ├── launchd/…             → copied to  <core>/launchd/…      (templated, see below)
    ├── plugins/
    │   └── catalog.myorg.toml   → picked up by the plugin installer's catalog overlay merge
    └── config/
        └── conductor.personal.yaml  → your reporting config with real targets
```

Every file under `files/` lands at the same relative path inside the core checkout. That's
the whole model: `files/scripts/foo.sh` becomes `<core>/scripts/foo.sh`.

## `overlay.toml`

```toml
# Directory (relative to this overlay's root) whose tree mirrors core paths. Default: "files".
files_dir = "files"

# Templating: after copy, sed these placeholders in matching files.
#   __HOME__     → your $HOME
#   __NODE_BIN__ → the dir containing your `node`
# Ship launchd plists / systemd units with placeholders so the overlay is host-portable.
[[template]]
glob = "launchd/*.plist"

# Symlinks created after copy. `target` is relative to the CORE root (i.e. a file you copied).
# This is how you repoint Conductor at your real reporting config without touching the
# core's tracked (neutral) config/conductor.yaml.
[[symlink]]
link   = "~/.tmux/conductor.yaml"
target = "config/conductor.personal.yaml"

# Extra env keys merged into the active profile .env (an existing key is kept, never clobbered).
[[env]]
key   = "WORK_UPSTREAM"
value = "https://gateway.internal.example/v1"
```

`[[template]]`, `[[symlink]]`, and `[[env]]` are each optional and repeatable.

## Applying / inspecting / removing

```bash
# from your core checkout:
bash scripts/overlay-apply.sh git@github.com:you/agents-nexus-plugs.git   # fetch + layer in
bash scripts/overlay-apply.sh ../agents-nexus-plugs --dry-run             # preview, touch nothing
bash scripts/overlay-apply.sh --status                                    # what's applied
bash scripts/overlay-apply.sh --remove                                    # un-apply cleanly
```

Or during setup: `./install.sh --overlay <git-url|path>`.

## How the catalog overlay works

Drop `files/plugins/catalog.<org>.toml`. The plugin installer
(`scripts/plugin-install-flow.sh`) already merges any sibling `catalog.*.toml` (never the
`*.example.toml` templates) into the plugin multi-select. Entries whose `source` is a
`claude_marketplace` are **auth-gated**: they install only if that marketplace is already
registered locally (`claude plugin marketplace list`). So an outsider who somehow obtained
your catalog still triggers no install — nothing org-specific is ever attempted for someone
who isn't entitled to it. See `plugins/catalog.example.toml` for the entry shape.

## Why the core stays clean

`scripts/export-public.sh` (the tool that produces the public core) is `git archive HEAD` —
**tracked files only**. Overlay files live at untracked paths (recorded in
`.git/info/exclude`), so they are invisible to the export. There is no way for an applied
overlay to leak back into a public export of the core.
