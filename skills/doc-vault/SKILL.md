---
name: doc-vault
description: >
  Deposit an authored HTML document into the local doc-vault so it is findable later — filed into
  the correct collection and tagged. Invoke this IMMEDIATELY AFTER writing any standalone HTML
  doc: a review deck, investigation report, design brief, architecture write-up, seam analysis,
  case doc, or diagram page. Without it the doc is captured by a background poll within two
  minutes but lands in a guessed collection with no topic tags, which is how docs get lost.
  Also use BEFORE writing such a doc, to start from the shared garner-doc theme instead of
  inventing a stylesheet; and to re-file or re-tag a doc already in the vault, or check whether a
  doc is indexed.
  Triggers: "/doc-vault", "file this doc", "put this in the vault", "add this to doc-vault",
  "tag this doc", "is this doc indexed", "where did that HTML report go", "deposit this report",
  "write this up as an HTML doc", "make an HTML report", "build a deck for this".
---

# doc-vault deposit

## 0. BEFORE you write the HTML — start from the theme

Do not hand-roll a stylesheet. Every doc that did produced a different look: 45 docs, no two
sharing a token vocabulary, one with no dark mode at all. Start from the shared theme:

```sh
doc-vault theme -o <file>.html
```

That writes a self-contained skeleton (~23KB, CSS inlined) with a sticky TOC rail, masthead,
stat tiles, finding cards, tables, callouts, and a collapsed appendix. Fill in the sections and
delete the ones you don't use.

Rules that matter while filling it in:

- **Every `<section>` needs an `id`, and every `id` needs a `.toc` link.** The TOC is the whole
  reason this theme was chosen over the others. A TOC entry that doesn't jump is worse than none.
- **Use the existing class vocabulary.** A new class name is exactly how a doc stops matching
  every other doc. The full table is in `$AGENTS_NEXUS_DIR/doc-vault/theme/README.md`.
- **Use the semantic tokens** (`--petrol` good, `--brass` caution, `--clay` danger, `--ink`,
  `--surface`), never raw hex. That is what makes dark mode work.
- **Keep the `<meta name="doc-theme">` marker.** `doc-vault theme --check` uses it.
- **Answer first** in `.dek`; push everything true-but-not-decision-relevant into the
  `<details>` appendix at the bottom.

Read `$AGENTS_NEXUS_DIR/doc-vault/theme/README.md` before deviating. Existing docs are NOT retrofitted — their
markup is coupled to their own class names — so do not try to restyle an old doc as a side task.

The vault indexes agent-authored HTML docs and serves them at http://localhost:8310.
It keeps its own copy, so a doc survives its origin being deleted or a worktree being pruned.

CLI: `doc-vault` (wrapper at `~/.local/bin/doc-vault`, code at `$AGENTS_NEXUS_DIR/doc-vault/docvault.py`).
Vault data lives in `$DOCVAULT_HOME` (default `~/doc-vault`) and is separate from the code.

## 1. Decide whether it belongs

Deposit ONLY a document a human would read: a deck, report, brief, write-up, or analysis.

Do NOT deposit:
- Templates or files with unrendered placeholders (`__TITLE__`, `${var}`, `{{var}}`).
- App shells, `index.html` bundles, anything loading a local `.js`.
- Test fixtures, Playwright/pytest report dumps, eval report dumps.
- Machine-readable output nobody reads as prose.

`put` BYPASSES the classifier that the background crawl applies. Depositing junk with `put`
succeeds. Judgment is yours.

## 2. Write the file somewhere durable first

Prefer, in order:
1. The relevant repo, in an existing docs/notes dir (`notes/`, `docs/`, `.ai/plans/`).
2. `~/investigations/` for debugging and root-cause write-ups.
3. `$HOME` only as a last resort — it is the unfiled `inbox` of the filesystem.

Do not write it to `/tmp`. Do not write it only into a git worktree you are about to remove.

## 3. Deposit it

```sh
doc-vault put <file.html> --collection <collection> --tag <tag> --tag <tag>
```

Verify the output. It prints `added: <title>` plus the URL, or `duplicate:` if identical content
is already indexed under another path.

Options:
- `--collection` — required in practice. Omit it only when you accept a path-derived guess.
- `--tag` — repeatable.
- `--ticket FC-1234` — shorthand for `--tag ticket:FC-1234`.
- `--title` — override the `<title>` tag. Use only if the `<title>` is wrong.
- `--move` — delete the origin after depositing. Use ONLY if you wrote the file to a scratch
  location. Never for a file that belongs in a repo.

## 4. Collections: a closed vocabulary

`$DOCVAULT_HOME/config.json` `"collections"` is authoritative. At time of writing:

| collection | what goes in it |
|---|---|
| `investigations` | debugging, root cause, incident analysis, risk/seam analysis |
| `log-sift` | log-sift and dossier-sift tool output only |
| `notes` | architecture write-ups, briefs, measurements, explanations. The default. |
| `merge-requests` | MR review decks and walkthroughs |
| `ideation` | proposals and explicitly exploratory idea docs |
| `r&d` | agent/platform capability exploration, prototypes, case-for-X docs |

A wrong value fails loudly and prints the valid list — you do not need to guess twice:

```
$ doc-vault refile 39 notez
error: 'notez' is not a collection.
  choose one of: investigations, log-sift, notes, merge-requests, ideation, r&d
```

Do NOT add a new collection to satisfy one doc. Pick the closest existing one. The vocabulary is
closed on purpose: it is the browsable hierarchy, and it only works while it is short enough to
scan. Adding a collection is the user's call.

## 5. Tags

These are DERIVED automatically. Do not pass them again:

| namespace | derived from |
|---|---|
| `ticket:` | ticket keys found in the document text |
| `repo:` | nearest `.git` ancestor, worktree name, or a `<repo>!<n>` title |
| `branch:` | worktree name after `--` |
| `mr:` | a `<repo>!<number>` title |
| `date:` | a date in the filename, else file mtime (local time) |

Add manually:
- **Topic tags**, free-form and lowercase: `observability`, `latency`, `cognito`, `demo`.
  Two or three. These are the ones a future search actually needs.
- `repo:<name>` ONLY when derivation cannot find it — a doc written outside any repo that is
  about one. Check first: `doc-vault put` output plus the doc page shows what was derived.
- `ticket:<KEY>` ONLY when the key is not in the document text.

Re-tag an existing doc:

```sh
doc-vault tag <id> latency cognito      # add
doc-vault tag <id> --rm stale           # remove ("-stale" does NOT work, argparse eats it)
doc-vault refile <id> notes             # change collection
```

## 6. Confirm

```sh
doc-vault stats                      # counts by collection and tag namespace
curl -s localhost:8310/api/docs      # JSON: id, title, collection, tags
```

If the server is not responding, it runs under launchd as `com.agents-nexus.doc-vault`:

```sh
launchctl kickstart -k gui/$(id -u)/com.agents-nexus.doc-vault
tail ~/doc-vault/server.log
```

## Notes

- Identity is the ORIGIN PATH. Re-depositing the same path with new content adds a version and
  keeps the SAME id — `put` prints `versioned:` instead of `added:`. Identical bytes at a new path
  is still one doc with two origins (`duplicate:`). Same title at a different path stays a
  SEPARATE doc; title is not identity.
- Rewriting a doc in place no longer creates a second entry. It becomes a new version of the
  same id, so there is nothing to warn the user about.
- A doc under a watched root is auto-deposited within 120s even if you skip `put`. That is a
  safety net, not the intended path — it cannot assign a collection or topic tags.
- Read `$AGENTS_NEXUS_DIR/doc-vault/README.md` before changing classifier or collection rules. Two rules there
  are deliberately counter-intuitive and documented with the reason.
- Theme details live in `$AGENTS_NEXUS_DIR/doc-vault/theme/README.md`. `garner-doc.css` is the single source of
  truth and is inlined at emit time — never hand-copy it into a doc, and never replace it with a
  `<link>` to the vault path (docs are opened as bare files, which would render unstyled).
