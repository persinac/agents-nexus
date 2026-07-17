# Maintaining agents-nexus

This repository is the **source of truth**. Clone it, develop in it, and commit here directly.

## History

This repo began as a de-identified export of a private monorepo. That migration was a
**one-time event** — there is no ongoing export or mirror. Do not look for an upstream to
sync from; this is upstream.

## Org-specific configuration

Anything organization-specific (live reporting targets, private catalogs, machine-specific
service definitions) does **not** belong in a commit here. It lives in a separate private
**overlay** repo and is layered in at install time:

```sh
git clone <this-repo>
./install.sh --overlay <your-overlay-repo-url>
```

Running `install.sh` without `--overlay` gives a generic, standalone setup.

## Contributing

- Keep commits free of organization identifiers, personal usernames, home paths, and
  secrets. There is no automated scrubber on this repo — that hygiene is on the committer.
- Put org/personal specifics in your overlay repo, never in a commit here.
