# garner-doc theme v1

One look for authored HTML docs. Extracted from the champion doc — *Amex Demo Entity Seam* —
which won on navigability, not decoration: it was the **only** doc in the set with a sticky TOC
rail whose links actually jump to anchored sections.

```sh
doc-vault theme -o mydoc.html     # self-contained skeleton, CSS inlined
doc-vault theme                   # same, to stdout
doc-vault theme --check           # which indexed docs are on theme
```

## Why the CSS is inlined, not linked

Docs get opened as bare files — from a repo, from a worktree, straight off disk — not only
through the vault server. An external `<link>` to this directory's `garner-doc.css` renders
unstyled everywhere except localhost:8310.

So `garner-doc.css` is the single source of truth and `theme -o` inlines it at emit time. Don't
hand-copy the CSS into a doc; you'd create a second copy that drifts.

## Tokens

Semantic names, never literal ones. `--clay` is "this is bad news", not "this is red" — which is
why the dark palette can flip the actual hue without a single rule changing.

| role | token | wash |
|---|---|---|
| page ground | `--paper` | |
| raised surface (cards, tables) | `--surface` | |
| inset (code, table head) | `--surface-2` | |
| borders | `--line`, `--line-soft` | |
| text | `--ink`, `--ink-2`, `--slate` | |
| accent / good | `--petrol` | `--petrol-dim` |
| caution | `--brass` | `--brass-dim` |
| danger | `--clay` | `--clay-dim` |

Three type roles: `--f-display` (Archivo, headings + TOC), `--f-body` (Newsreader serif, prose),
`--f-mono` (JetBrains Mono, labels, code, numbers). `--measure` (66ch) caps line length;
`--rail` sizes the TOC column.

**The theme is declared in three blocks.** Complete light palette on bare `:root`; dark under
`@media (prefers-color-scheme: dark)` guarded by `:root:not([data-theme="light"])`; dark again
under `:root[data-theme="dark"]`. That third block is what makes an explicit toggle win in both
directions. Never give a colour its only definition inside a media query — one doc in the set had
no dark mode at all and read as a bug.

## Component vocabulary

Use these names. A new class name is how a doc stops matching every other doc.

| component | use it for | don't |
|---|---|---|
| `.shell` + `.rail` + `main` | the whole page frame | — |
| `.toc` | one link per `<section id>` | leave a link that doesn't jump |
| `.eyebrow` | mono micro-label above a heading | body text |
| `.masthead` / `.dek` / `.stamp` | title block, standfirst, metadata row | put the verdict below the fold |
| `.tiles` / `.tile` | 3–6 numbers a reader needs first | more than 6, or prose |
| `.chip` `.done .moot .block .watch .svc` | status | decoration |
| `.cards` / `.card` `.is-key .is-moot` | one per finding or entity | wrap the whole doc in one |
| `.scroll` + `<table>` | any table — the wrapper is what stops page-level h-scroll | a bare `<table>` |
| `td.num` | numbers (tabular, right-aligned) | text |
| `.callout` `.soft .calm` | danger / caution / neutral aside | more than one per section |
| `.gates` / `.gate` | numbered preconditions or steps | type the numbers — it's a CSS counter |
| `.flow` / `.node` / `.hop` `.trap` | a left-to-right pipeline, `.trap` where it breaks | anything branching — use mermaid |
| `.blockers` / `.blocker` | things that stop the work | a generic container |
| `.two` / `.stack` / `ul.tight` | side-by-side, vertical group, tight list | nesting three deep |
| `<details>` / `.details-body` | the appendix | hide decision-relevant content |

## Structure rules

1. **The TOC is the feature.** Every `<section>` gets an `id`; every `id` gets a `.toc` link.
   `theme --check` won't catch a broken pair — check it yourself.
2. **Answer first.** `.dek` gives away the verdict. A reader who stops after the masthead should
   still have it.
3. **`.tiles` colour by good/bad news**, never by importance. `.ok` = good, `.warn` = bad, no
   class = neutral.
4. **Everything true but not decision-relevant goes in `<details>` at the bottom.** That section
   may be long. It's what keeps the top short. Never delete it, never promote it.
5. **Keep the `<meta name="doc-theme">` marker.** It's how `theme --check` distinguishes an
   on-theme doc from a bespoke one.

## Existing docs are not retrofitted

`theme --check` reports 45 bespoke, 0 on theme — expected. Each doc's CSS is coupled to its own
class names (58 classes in one, 8 in another, no two sharing a token vocabulary), so no shared
stylesheet can reach them retroactively. Retrofitting means rewriting each doc's markup.

The theme applies to docs written from here on. If a specific old doc is worth converting, that's
a manual rewrite of that doc, not a config change.
