# doc-vault

A local index for the HTML docs agents write — review decks, investigation reports, design
briefs. It exists because those docs were being written into whatever directory an agent
happened to be in, and then lost.

Stdlib Python 3.9+ only. No pip install, no virtualenv, no external services. `from __future__
import annotations` is what keeps the floor there — the `X | Y` annotations never get evaluated.
Verified on 3.9.6 and 3.14.2.

## Code here, data outside

The code is a component of this repo, which is **public**. The vault is machine state and is
never committed.

| | where | holds |
|---|---|---|
| code | `doc-vault/` in this repo | `docvault.py`, `theme/`, `tools/`, `tests/` |
| data | `$DOCVAULT_HOME`, default `~/doc-vault` | `docs/`, `index.db`, `config.json`, `server.log` |

`DOCVAULT_HOME` selects the data dir. Unset, it defaults to `~/doc-vault`, so an existing install
keeps working untouched. Set it to a path that does not exist and doc-vault **refuses to start**
rather than creating an empty vault — a silent empty vault is indistinguishable from having lost
every doc.

Live config is `$DOCVAULT_HOME/config.json`, which is not in this repo because it carries machine
paths and the Access allowlist. Every value under `access` is per-host — `team`, `aud`, `allow` and
`ca_bundle` all differ between machines, so a second box needs its own config rather than a copy of
the first one's. The launchd job is macOS-only; a Linux host needs a systemd unit invoking the same
`docvault.py serve --watch 120`. Start from `config.example.json`:

```sh
cp config.example.json ~/doc-vault/config.json    # then edit
```

Install the service with `task launchd:install:doc-vault` from the repo root. That boots out the
pre-move `com.docvault.server` label first — both bind 8310, so leaving the old one loaded
crash-loops the new job.

## Use it

```sh
doc-vault serve --watch 120     # browse + search at http://localhost:8310
doc-vault put report.html       # deposit one doc (the deposit-point)
doc-vault crawl --dry-run -v    # see what a sweep would pick up, and why
doc-vault crawl                 # sweep the roots and deposit
doc-vault stats                 # what the vault holds
doc-vault refile 17 notes       # move a doc between collections
doc-vault tag 17 perf --rm foo  # add / remove tags
doc-vault forget 17             # drop a doc
doc-vault reindex               # re-derive text, collection and auto-tags
```

`put` takes `--collection`, `--tag` (repeatable), `--ticket` (shorthand for `--tag ticket:KEY`),
`--title`, and `--move` (delete the origin after depositing).

## Versions — a rewrite keeps its ID

Identity is the **origin path**, not the content. Re-depositing a file that is already known at
that path adds a version and moves the doc's pointer; the ID, tags and collection are untouched,
so `/doc/12` stays `/doc/12` forever.

| you do | status | result |
|---|---|---|
| deposit a new path | `added` | new doc, v1 |
| deposit the same path, same bytes | `duplicate` | nothing changes |
| deposit the same path, new bytes | `versioned` | v2, same ID |
| deposit content matching an earlier version of that doc | `reverted` | pointer moves back, no v4 |
| deposit identical bytes at a *different* path | `duplicate` | one doc, two origins |

Two files that merely share a title stay separate docs — title is not identity. That is deliberate:
grouping by title would fuse unrelated log-sift runs, which all sign the same title.

Every version's HTML is kept on disk (`docs/<slug>-<hash8>.html`), so history is not lost. There is
no UI for reading an old version yet; the doc page shows `v2 of 3` with dates on hover.

## Comments — general and anchored

The doc page has a comments pane (toggle in the viewer bar; its open/closed state is remembered
per-browser). Two kinds:

- **general** — about the doc as a whole.
- **highlight** — select text in the doc and the form anchors to it, storing the exact quote plus
  ~40 characters either side. Clicking a stored quote finds it again in the doc.

A comment records the version it was made on. After a rewrite, older comments stay visible and are
marked `on v1`, since the text they point at may no longer exist. Anchors are stored as quote plus
context rather than offsets, so re-anchoring into a later version stays possible later.

Author comes from the request: the gated port uses the verified Access email, and the ungated
loopback port uses `local_author` from the config (default `local`), because it has no identity to
read. POST is refused cross-origin, capped at 32KB per request and 8KB per comment body, and — this
is the part that matters — is gated on the Access port exactly as GET is. A `do_GET`-only gate would
have left an authenticated-looking write surface open on the public hostname.

## Two axes, different in kind

**Collections** are a *closed, curated vocabulary*. Every doc is in exactly one:

```
investigations   log-sift   notes   merge-requests   ideation   r&d
```

That's the browsable hierarchy, and the point of keeping it closed is that it stays short enough
to scan. `refile` and `put --collection` validate against the list and refuse anything else, so a
typo can't quietly invent a new bucket. Add a real one by editing `collections` in `config.json`.

Placement is by ordered rules in `collection_rules`, first match wins. Each rule keys on exactly
one of:

| key | matches against | example |
|---|---|---|
| `match` | path substring | `/.claude/mr-walkthrough/` |
| `name` | filename glob, case-insensitive | `dossier-sift-*` |
| `title` | document title glob, case-insensitive | `*log-sift` |

Anything unmatched falls to `default_collection`.

The `title` matcher exists because `log-sift` output cannot be identified any other way: it is
scattered across `~/investigations`, `~/Downloads` and the log-sift repo itself, under three
different filename conventions. The tool signs its own titles — `Chat <uuid> — log-sift` — so a
title suffix glob is the only rule that actually holds. It is deliberately first in the list,
ahead of the `/investigations/` and `dossier-sift-*` rules that would otherwise claim those docs.

**Tags** are an *open, namespaced set*. A doc has any number. These are derived automatically:

| namespace | from | example |
|---|---|---|
| `ticket:` | keys found in the document text | `ticket:FC-1838` |
| `repo:` | nearest `.git` ancestor, worktree name, or an MR title | `repo:svc-chatbot` |
| `branch:` | the worktree name after `--` | `branch:amex-demo` |
| `mr:` | a `<repo>!<number>` title | `mr:596` |
| `date:` | a date in the filename, else the file's mtime | `date:20260824` |

Anything else is free-form: `doc-vault tag 39 observability`.

Tag chips are clickable everywhere, `/tags` is the full index grouped by namespace, and typing a
tag straight into the search box (`repo:svc-chatbot`) jumps to its listing instead of running a
text search.

Ticket keys used to live in their own table. They're tags now — one uniform mechanism, one chip
renderer, one filter path.

## How it holds things

The vault owns a **copy** of every doc, in `docs/`. That is the whole point: a deck written
inside a git worktree survives the worktree being pruned. `index.db` is SQLite with an FTS5
full-text index over title, lede, and extracted body text.

Docs are deduped by **content hash**, so the same document found in three places is one entry
with three recorded origin paths, and each location can contribute tags the others couldn't.
Same title but different content stays separate, cross-linked as "other versions" on the doc
page.

Timestamps are **naive local time**, deliberately. UTC crosses the day boundary at 17:00–18:00
here, so a doc written at 23:06 on the 24th was being stamped and tagged `date:20260825` — the
wrong answer to "when did I write this". Every consumer of these dates is one person in one
timezone.

## Two ways in

**Crawl** is the manual backfill: sweep the configured roots, score every HTML file, deposit what
passes. **Watch** (`serve --watch N`) polls the same roots every N seconds and deposits anything
new. Both call the same `classify()` and `resolve_collection()`, deliberately — if they
disagreed, some docs would only ever appear after a manual sweep and nobody could tell which.

## Exposing it over a Cloudflare tunnel

Off by default. `serve` runs one unauthenticated loopback listener on 8310, as it always has.

Setting `access.enabled` starts a **second** listener on `access.port` (8311) where every request
must carry a Cloudflare Access JWT, signed by the team's published keys, for an email in
`access.allow`. Point the tunnel at 8311. Never at 8310.

Two ports rather than one gate on 8310, for two reasons. `curl localhost:8310/api/docs` appears in
the skill docs and in agent code, so gating it breaks existing callers. And cloudflared connects
*from* localhost, so the tunnel's traffic is indistinguishable from a local browser's by source
address — separate ports is what makes the distinction real.

```json
"access": {
  "enabled": true,
  "team": "<your-zero-trust-team>",
  "aud": "<AUD tag from the Access application>",
  "allow": ["you@example.com"],
  "port": 8311
}
```

`allow` entries are either an exact address or `@domain`, which admits any address at that domain
(exact-match only — `@example.com` does not admit `user@sub.example.com`). Use a domain entry when
the Cloudflare policy is already domain-scoped, so membership is decided in one place instead of
two; use exact addresses when the origin should be narrower than the policy.

`aud` is not optional. Without it, a token minted for *any* application in the team verifies here,
which is most of the protection gone. `serve` refuses to open the gated port when `team`, `aud`, or
`allow` is missing — it logs the gap and keeps serving 8310, rather than exiting (KeepAlive would
crash-loop it and take local browsing down with it) or opening the port half-configured.

Dashboard side, once:

1. Zero Trust → Networks → Tunnels → your tunnel → add a public hostname → service
   `http://127.0.0.1:8311`.
2. Zero Trust → Access → Applications → self-hosted app on that hostname → Allow policy for the
   same emails.
3. Copy the app's **AUD tag** into `access.aud`.
4. `launchctl kickstart -k gui/$(id -u)/com.agents-nexus.doc-vault`.

The Access policy and `access.allow` are belt and braces on purpose. Access decides who reaches
the origin; the allowlist decides who the origin answers. Either alone would do on a good day.

Verify the gate: `docvault.py access-selftest` stands it up against a throwaway key and a local
JWKS fixture, then asserts 13 cases — valid token, missing token, wrong aud, wrong issuer, expired,
not-allowlisted, `alg: none`, `alg: HS256`, unknown kid, post-signing payload swap, garbage.
`tests/test_access_guard.py` covers the misconfig refusals.

### Why the JWT verify is hand-written

doc-vault has no third-party dependencies, this interpreter is PEP-668 externally-managed, and
PyJWT would mean a venv plus a launchd plist edit. RSA PKCS#1 v1.5 *verification* handles no secret
material — one modular exponentiation against a published public key, then a comparison — so the
usual reasons to avoid writing crypto yourself (key custody, timing leaks on secrets, nonce reuse)
do not apply. The real pitfall is lenient padding parsing, which forgery attacks exploit;
`_rsa_sha256_verify` builds the entire expected padded block and compares it whole, so there is
nothing to parse leniently. `alg` is pinned to RS256 rather than read from the token, which is the
other classic hole.

## Tests

`task docvault:test` runs all four suites in order and stops on the first non-zero exit.

| Suite | Needs | Covers |
| --- | --- | --- |
| `tests/test_access_guard.py` | stdlib | Access misconfig refusals, the `email_allowed` matcher |
| `tests/test_versioning.sh` | stdlib | deposit / duplicate / version / revert |
| `tests/test_comments.sh` | stdlib | the comment, position, resolve and delete endpoints |
| `tests/test_anchor_browser.py` | playwright + Chromium | whether a quote actually anchors |

Only the last one has a DOM, so it is the only place anchoring can be asserted. It drives real
Chromium and checks every swipe against the browser's own `find()` rather than against
`findRange`, so a bug in the lookup cannot make its own test pass. Run it with
`uv run --with playwright python tests/test_anchor_browser.py`; if no browser is installed it
prints `SKIP` and exits 0, so the other suites stay runnable on a box without one. Install one
with `uv run --with playwright playwright install chromium`, or point `DOCVAULT_CHROMIUM` at an
existing binary. `DOCVAULT_TEST_HEADED=1` watches it run.

## What counts as a doc

The hard part. This machine has roughly two non-documents for every document: committed Jinja
and Go templates, Playwright reports, eval report dumps, app-shell `index.html` files, fifteen
worktree copies of the same `chatgpt-rest-client.html`.

Rejection happens in three layers, cheapest first:

1. **`prune_dirs`** — directory names never walked at all (`node_modules`, `templates`,
   `llm_eval`, `examples`, …).
2. **`deny_path` / `deny_name`** — path substrings and filename globs that reject outright.
   This catches every real template on the machine by name (`*_template.html`, `*_base.html`).
3. **Content score** — needs to clear `score_threshold` (default 5). Points for a real
   non-generic `<title>`, size in range, an inline stylesheet, substantial prose, and section
   headings. Penalties for almost no prose, a local script bundle (the signature of an app shell
   rather than an authored document), and an unrendered placeholder in the title.

Everything is in `config.json`. Two rules there earned their comments the hard way:

- **Template detection looks at the `<title>` only.** Scanning the body for `${...}` or `{{...}}`
  rejected four real review decks, because a deck *quotes source code*. The decks also emit code
  as `<div class="ln"><span class="si">` token soup rather than `<pre>`/`<code>`, so there is no
  reliable region to exclude. Every actual template on this machine names itself with its own
  placeholder — `__TITLE__`, `${npi} ${specialty} Diag` — which is both precise and cheap.
- **`ng-app` and friends are gone as body markers.** `ng-app` matched inside the word
  "wro**ng-app**roval" in a walkthrough deck. Substring matching on framework attributes is not
  worth the false positives.

`always_allow` forces a path in regardless of score. `crawl --dry-run --verbose` prints every
reject with the reason that sank it — use it before tuning anything.

## Known gaps

- **Edit-after-deposit.** A doc edited in place gets a new content hash and is deposited as a
  separate entry; the older copy stays. Good enough at this scale, wrong at ten times it.
- **Partial writes.** A poll can catch a half-flushed file and hash a state that never really
  existed. No settle delay yet.
- **Directory vanishing mid-walk.** Routine when a worktree is pruned. The walk swallows the
  error; it does not retry.
- **No tag rename or merge.** `repo:Garner` (the Obsidian vault's git root) is accurate but ugly,
  and there is no way to alias it to something better short of `tag --rm` per doc.
- **Collection rules are path-shaped, not content-shaped.** A doc's bucket comes from where it
  was written, so a misfiled doc needs `refile`. That is a deliberate trade — path rules are
  auditable in one glance, content classification is not.
- **Google Fonts.** Docs reference `fonts.googleapis.com`, so they render slightly differently
  offline. Nothing breaks.

## Containerizing later

Deliberately deferred. When it happens it is a `python:3.14-slim` image with no build step:
copy `docvault.py`, mount `docs/` and `index.db` as a volume, mount the crawl roots read-only,
expose 8310. It belongs as a service in `agents-nexus-public/docker-compose.yml` alongside
`nexus-proxy`, not as its own stack.

Note before touching that compose file: `docker-compose.work.yml` in that repo is uncommitted,
and Docker on this box holds ~55GB of images and ~100GB of volumes. Never blanket-prune.
