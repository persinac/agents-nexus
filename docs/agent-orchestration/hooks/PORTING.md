# The hooks

Two `PreToolUse` hooks on `Bash`. They are the only thing here that is a **control** rather than a
convention — everything else in this package is a document an agent can talk itself out of.

Between them they stopped the coordinator **five times** in one session, and were right every time —
including once where a `python3 -c` would have printed a live credential into a durable transcript.

Copied verbatim rather than rewritten. They are tested; a rewrite would not be.

| file | lines | what it refuses |
|---|---|---|
| `block-credential-dump.sh` | 469 | reads that would print a secret into the transcript |
| `block-destructive.sh` | 181 | commands that delete production data or deployed infrastructure |

## Install

```bash
cp block-credential-dump.sh block-destructive.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/*.sh
```

Then merge `../settings.hooks.json` into `~/.claude/settings.json`.

Verify they are live by running something they should refuse — e.g. `cat` on a file named `*secret*`.
**If it is not blocked, the wiring did not take.** (A hook that is not installed and a hook that
approves look identical from inside the session — the same class of failure the rules file is about.)

## What to change for a different environment

Both are self-contained bash with no external dependencies beyond coreutils. Four things are
environment-specific:

1. **Secrets tooling.** `block-credential-dump.sh` has ~18 references to one particular secrets CLI —
   the rule being that its "delete" verb prints the whole remaining table in plaintext by default
   while its "set" verb prints only one key. **That asymmetry is the trap, and every secrets tool has
   its own version of it.** Find yours and substitute.
2. **Kubernetes config paths.** ~6 references to one distro's config files (the ones holding a
   bootstrap token and a cluster encryption secret). Replace with whatever holds yours —
   kubeconfigs, cloud credential files, `.env`, `*.pem`, `id_rsa*`, `.npmrc`, `.netrc`.
3. **The classifier path.** `block-destructive.sh` points at a helper for classifying destructive
   commands. Repoint or inline it.
4. **Your own denylist.** The value is in the *specific* paths and verbs that would hurt **you**.
   Add them; the generic patterns are the floor, not the ceiling.

## Two things worth knowing before you trust them

**They produce false positives, and the right response is to fix the classifier — not route
around it.** Observed: `drop index` inside a *grep pattern*, in a command that only read a file from
an API, was refused as DDL. Correct behaviour from a conservative denylist. The fix is narrowing the
pattern, not adding an exception.

**Never satisfy a hook by asking someone else to run the command.** A control one actor can satisfy
by asking a second actor is not a control. This came up: an agent blocked from posting was going to
ask a peer; the peer **refused**; the asker **withdrew**. That is the right outcome on both sides and
the norm worth propagating.

Legitimate responses, in order:

1. **Restructure so the command cannot disclose** — split the interpreter from the credentialed call,
   put the payload in a file and pass the path, write first and verify in a *separate* command.
2. **Fix the classifier**, if it is genuinely a false positive.
3. **Hand it to the human**, who runs it in their own shell so the output never enters the transcript.

## A known trigger, and one that is not established

- **Confirmed:** inlining `key=$VAR&token=$VAR` directly into a URL query string trips the reader.
  **One level of variable indirection does not** — assign the query string to a variable first, then
  interpolate that. Isolated by an agent who hit it with **no `echo` in the command at all**.
- **Unproven:** an `echo` in the same command as a secret-named variable *appears* to trip it. The
  first fix attributed it to the `echo`, but that same restructure also reverted to the indirection —
  **both changed at once and the echo was never isolated.** Treat as a hypothesis until someone varies
  one at a time.

The shape that works regardless: **body in a file, indirection for credentials, write first and
verify second.** Clean across ~20 calls by four agents.
