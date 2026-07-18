# scripts/secrets/ — pluggable secrets-backend seam

Resolve secrets through an **ordered chain** of backends. The first backend that returns a
value wins; every backend fail-softs (missing CLI or absent key → empty, and the chain moves
on). This generalizes the repo's old copy-pasted `${VAR:-$(doppler …)}` pattern into one seam:
`env` is the universal default, and `doppler` / `aws-sm` (and org-specific backends shipped via
an overlay, e.g. `vault`) layer after it.

## Selecting the chain

```
NEXUS_SECRETS_BACKENDS=env              # default (fresh clone): env only
NEXUS_SECRETS_BACKENDS=env,doppler      # env, then Doppler CLI
NEXUS_SECRETS_BACKENDS=env,aws-sm       # env, then AWS Secrets Manager
NEXUS_SECRETS_BACKENDS=env,doppler,aws-sm
```

Multiple backends coexist with no routing map: each returns empty for names it doesn't hold,
so the chain finds each secret wherever it lives. Per-host is just a different chain value
(set in `~/.tmux/env.sh`, the profile `.env`, or an overlay `[[env]]` block).

## Tools

- **`secret-get.sh [--project P] [--config C] [--backends a,b,c] NAME`** — resolve ONE secret,
  print the value (empty if no backend has it). Point-fetch for scripts that need a single value
  (`database/dbmate.sh`) or for a Python caller shelling out (`agent-runner/conductor.py`).
- **`secret-run.sh [--project P] [--config C] [--backends a,b,c] NAME [NAME...] -- CMD...`** —
  resolve the named secrets, export them, then `exec CMD`. The generalization of
  `doppler run -- CMD`: pre-populates the process env so any downstream bare-`$VAR` consumer
  (e.g. the Slack bridge) works unchanged. Names are explicit (the chain has no "list all
  secrets" primitive); a name that resolves empty is left **unset**, not exported as `""`.

`--project` / `--config` set the Doppler scope **for that call only** (exported to the doppler
backend). This is how different call sites use different Doppler projects (`dbmate.sh` → `nexus`,
conductor Trello → `infrastructure`) without a single global.

## Adapter contract — `backend-<name>.sh get NAME`

To add a backend, drop a `backend-<name>.sh` in this directory (public core) or ship it via an
overlay at `files/scripts/secrets/backend-<name>.sh` (path-mirror, no registration needed —
`secret-get.sh` discovers any `backend-*.sh` here). It must implement `get NAME`:

- **stdout**: the value on a hit, **nothing** on a miss. Use `printf '%s'` (no trailing newline,
  no `echo -e/-n`).
- **exit code**: `0` for hit, miss, *and* tool-absent — the resolver decides hit/miss **solely on
  non-empty stdout** and ignores the exit code. Reserve non-zero only for a usage error (missing
  `NAME`).
- **fail-soft**: if the backing tool is not installed, or the key is absent, print nothing and
  exit 0 so the chain continues. Redirect the tool's stderr to `/dev/null` — error text must
  never end up in the returned value.
- **NAME** is a validated identifier (`^[A-Za-z_][A-Za-z0-9_]*$`); `secret-get.sh` enforces this
  before calling you, but guard it too if your adapter can be invoked directly.

Config for your backend comes from the environment (set in `.env` / `env.sh` / an overlay
`[[env]]`). Keep real secrets (tokens) out of anything committed — only the *selector* and
non-secret coordinates (project names, prefixes, addresses) belong in `env.defaults.sh` /
`.env.example` / an overlay manifest.

### Backends shipped in the public core
- `backend-env.sh` — read `$NAME` from the process env. Always present; the default.
- `backend-doppler.sh` — `doppler secrets get` (`DOPPLER_PROJECT`/`DOPPLER_CONFIG`/`DOPPLER_BIN`).
- `backend-aws-sm.sh` — `aws secretsmanager get-secret-value` (`AWS_SM_PREFIX`/`AWS_SM_BIN` + `AWS_*`).

Exotic backends (HashiCorp Vault, 1Password Connect, …) ship from a private overlay — see
`overlay.example/README.md`.

## Invocation note

Everything is invoked via `bash <path>` (the call sites do not rely on the executable bit), so
the `git archive` public export and the overlay `cp` don't depend on file-mode preservation.
