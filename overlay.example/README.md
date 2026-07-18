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

## How secrets backends work

The public core ships an ordered secrets-resolver chain (`scripts/secrets/`) with three
backends: `env` (default), `doppler`, and `aws-sm`. An org adds an **exotic backend** (Vault,
1Password Connect, an internal secrets API) the same way it adds any file — drop
`files/scripts/secrets/backend-<name>.sh` in your overlay and it lands beside the core adapters
(no registration; `secret-get.sh` discovers any `backend-*.sh`). Implement the one-line contract
in the core's `scripts/secrets/README.md` (`get NAME` → value on stdout, empty + exit 0 on a
miss, fail-soft when the tool is absent).

Activate and configure it via `[[env]]` blocks in `overlay.toml`:
- put your backend in the chain: `NEXUS_SECRETS_BACKENDS = "env,vault"`
- set its **non-secret** coordinates (server address, KV path, prefix)

Keep the real credential (a Vault token, an API key) in `~/.tmux/env.sh` on each box, **never**
in the overlay — the overlay is a repo, and `[[env]]` only fills gaps in `.env` anyway. This
directory ships a worked example: `files/scripts/secrets/backend-vault.sh` plus the matching
`overlay.toml` `[[env]]` blocks.

Note the same auth-gate property as the catalog: a backend for a tool a machine doesn't have
simply fail-softs (returns empty, chain continues), so a leaked overlay triggers nothing — an
adapter is inert without both the tool and its credentials present locally.

## Why the core stays clean

`scripts/export-public.sh` (the tool that produces the public core) is `git archive HEAD` —
**tracked files only**. Overlay files live at untracked paths (recorded in
`.git/info/exclude`), so they are invisible to the export. There is no way for an applied
overlay to leak back into a public export of the core.
