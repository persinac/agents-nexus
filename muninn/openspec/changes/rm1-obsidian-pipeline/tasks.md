## 1. Project Scaffolding

- [x] 1.1 Initialize Python project with `pyproject.toml` using `uv`, define `muninn` CLI entry point
- [x] 1.2 Add core dependencies via `uv add`: `rmrl`, `pdf2image`, `boto3`, `anthropic`, `requests`, `click`; add `rclone` as system dependency note in README
- [x] 1.3 Create `Taskfile.yml` with tasks: `lint`, `format`, `test`, `run`, `check`, `sync`
- [x] 1.4 Create `config.toml.example` with all required and optional fields documented, including `[[vaults]]` array and Tailscale SSH host example
- [x] 1.5 Implement `config.py` — load and validate `config.toml` at startup, validate `[[vaults]]` array (at least one vault, exactly one `default = true`), raise descriptive errors for missing required fields
- [x] 1.6 Initialize SQLite database schema (`muninn.db`) with tables: `notebooks` (uuid, title, rm_folder, file_hash, last_synced), `pages` (notebook_uuid, page_index, png_hash, ocr_text, vision_description)

## 2. reMarkable Sync (`rm-sync`)

- [x] 2.1 Implement SSH connectivity check (`muninn check --ssh`) using configured host and identity file
- [x] 2.2 Implement `rm_sync.pull()` — use rclone over SSH to mirror the rM1 notebook directory to a local staging path
- [x] 2.3 Implement change detection — hash each pulled notebook's files and compare against `notebooks` table; return lists of new/modified/unchanged UUIDs
- [x] 2.4 Implement `--dry-run` flag for `muninn sync` — print pending notebooks without writing files or updating state
- [x] 2.5 Handle SSH connection timeout and unreachable device: log error and exit cleanly without modifying state
- [ ] 2.6 Document Tailscale setup in README: install Tailscale on rM1 (via Toltec), confirm tablet MagicDNS hostname, set as SSH host in `config.toml`
- [x] 2.7 Implement `rm_sync.resolve_folder_path()` — walk parent UUIDs across `.metadata` files to build human-readable folder paths (e.g., `"Work/Garner/Chatbot"`); update `notebook_folder()` to return the resolved path
- [x] 2.8 Add `--folder PREFIX` CLI flag to `muninn sync` (case-insensitive, repeatable). Filter pending notebooks by folder-path prefix before processing. `--dry-run` output shows resolved folder paths for verification.

## 3. Notebook Conversion (`notebook-conversion`)

- [x] 3.1 Implement `conversion.to_pdf()` — call `rmrl` to convert a notebook directory to PDF, handle unsupported format version errors (best-effort; v6 notebooks fail PDF/PNG but still OCR via stroke parsing)
- [x] 3.2 Implement `conversion.to_pngs()` — rasterize PDF pages to PNG at 150 DPI using `pdf2image`, output `page_001.png` ... `page_NNN.png`
- [x] 3.3 Handle empty notebook (no `.rm` page files): log warning and skip conversion
- [x] 3.4 Store PDF and PNGs under `<staging_dir>/<uuid>/notebook.pdf` and `<staging_dir>/<uuid>/pages/page_NNN.png`

## 4. Handwriting OCR (`handwriting-ocr`)

- [x] 4.1 Add `remarkable-layers` dependency for `.rm` v3/v5 stroke parsing on RM1 (implemented as a vendored v5 parser in `rm_format.py` plus `rmscene` for v6; upstream `remarkable-layers` was unavailable on PyPI)
- [x] 4.2 Implement `ocr.extract_strokes(rm_path)` — parse a `.rm` page file into MyScript-shaped stroke groups (`x[], y[], p[]` per stroke; omit `t[]` since RM1 doesn't record per-point timestamps)
- [x] 4.3 Implement `ocr._sign(application_key, hmac_key, body_bytes)` — HMAC-SHA512 over the exact body bytes with key = `application_key + hmac_key`; return hex digest
- [x] 4.4 Implement `ocr.transcribe_strokes(cfg, strokes)` — POST strokes to `https://cloud.myscript.com/api/v4.0/iink/batch` with `applicationKey` + `hmac` headers; parse JIIX `label` field
- [x] 4.5 Implement cache lookup before API call — skip when `pages.ocr_text IS NOT NULL` for the current `rm_hash` (rm_hash invalidates correctly when strokes change; switched from `png_hash` in commit dfa6027 since OCR doesn't need PNGs)
- [x] 4.6 Handle MyScript API non-200 responses: log error, store NULL transcription, continue to next page
- [x] 4.7 Handle empty/low-confidence MyScript response: store empty string, do not treat as error
- [x] 4.8 Implement `[ocr].enabled` config flag — skip all OCR calls when `false`
- [x] 4.9 Validate `[ocr].application_key` and `[ocr].hmac_key` present at startup when OCR is enabled (done in commit 9419bcb)
- [x] 4.10 Wire OCR into the `muninn sync` pipeline after PNG conversion; iterate `.rm` page files in stable order
- [x] 4.11 Implement `muninn check` MyScript verification — send a minimal test stroke and assert 200 + JIIX response shape

> **Phase 4 addendum (commit 3b9e6d6):** Added Claude vision as a second OCR provider and made it the default. MyScript transcriptions were repeatedly mangling content (emojis from underlines, dropped letters, flattened diagrams). Selection lives in `[ocr].provider = "claude" | "myscript"`. New `muninn reocr` command refreshes pages whose stored provider doesn't match config. Renderer in `rm_format.render_to_png` handles rM1 "scrolled" pages that extend past the default 1872-px viewport.

## 5. Drawing Interpretation (`drawing-interpretation`)

- [x] 5.1 Implement `vision.describe_page()` — send page PNG to Claude API (vision), return natural-language description
- [x] 5.2 Implement empty-page skip — inspect `.rm` metadata for zero stroke count; skip vision call and store empty description (parses strokes via `rm_format.parse_strokes`; empty list → store `""`)
- [x] 5.3 Implement cache lookup before API call — cached by `rm_hash` (same change as 4.5; `png_hash` doesn't cover v6 pages)
- [x] 5.4 Handle vision API errors: log error, store `null` description, continue to next page
- [x] 5.5 Implement `vision.enabled` config flag — skip all vision calls when `false`
- [x] 5.6 Support configurable vision prompt via `vision.drawing_prompt` in `config.toml`
- [x] 5.7 Validate vision API key present at startup when vision is enabled (or `ANTHROPIC_API_KEY` env var)

## 6. Asset Storage (`asset-storage`)

- [ ] 6.1 Implement `storage.upload_notebook()` — upload `notebook.pdf` and all page PNGs to S3 under `muninn/<uuid>/<ISO-date>/`
- [ ] 6.2 Implement idempotent upload — check if S3 key already exists before uploading; skip and log debug if present
- [ ] 6.3 Handle S3 upload errors: log error, mark notebook as `upload_failed` in database, continue to next notebook
- [ ] 6.4 Support non-AWS S3-compatible endpoints via `storage.endpoint` config field
- [ ] 6.5 Implement `storage.enabled` config flag — skip all S3 uploads when `false`
- [ ] 6.6 Validate bucket name present at startup when storage is enabled

## 7. Obsidian Sync (`obsidian-sync`)

- [x] 7.1 Implement `obsidian.build_markdown()` — generate `.md` content with YAML frontmatter and per-page sections (transcription + drawing description)
- [x] 7.2 Implement per-page section rendering: `### Transcription` and `### Drawing` sub-headings when content present; `*No content detected*` placeholder when both empty
- [x] 7.3 Implement filename sanitization — replace filesystem-unsafe characters with underscores
- [x] 7.4 Implement atomic file write — write to `.tmp` file then rename to final path
- [x] 7.5 Implement multi-vault config parsing — load `[[vaults]]` array, resolve folder prefix patterns, identify default vault (validated in `config.py`)
- [x] 7.6 Implement vault routing — given a notebook's `rm_folder`, match against vault `folders` prefixes (first match wins); fall back to default vault (`obsidian.pick_vault`)
- [x] 7.7 Implement per-vault `subfolder` support — create subdirectory if it doesn't exist; write notes there when configured (`write_notebook_atomic` mkdirs parents)
- [x] 7.8 Validate all vault paths exist at startup; exit with descriptive error naming any missing vault
- [x] 7.9 Add `rm_folder` to YAML frontmatter of generated Markdown files

> **Phase 7 addendum:** Added `muninn write-vault` command for backfilling notebooks into vaults (e.g. after toggling vision, switching OCR provider, or starting from an existing DB without on-disk notes). Supports `--folder`, `--vault`, `--dry-run` filters.

## 8. CLI and Orchestration

- [x] 8.1 Implement `muninn sync` command — orchestrate full pipeline: pull → convert → OCR → vision → write vault → daily merge (S3 upload deferred; Phase 6 skipped)
- [x] 8.2 Implement `muninn check` command — verifies SSH, OCR provider (Claude or MyScript), and vision API connectivity. S3 check is a stub.
- [x] 8.3 Implement per-notebook error isolation — `_write_to_vault` and `_maybe_merge_daily` both wrap exceptions, log, and continue
- [x] 8.4 Add structured logging (INFO for normal progress, DEBUG for cache hits, ERROR for failures)
- [x] 8.5 Write `README.md` with setup instructions: SSH key setup, Toltec/Tailscale on device, `config.toml` fields (including `[[vaults]]` and `[daily_merge]` examples), `uv` install, Taskfile usage, launchd plist example (`scripts/com.muninn.sync.plist`)
- [ ] 8.6 [DEFERRED] Implement Slack notification on sync completion — send summary (notebooks processed, errors) to configured webhook URL

## 9. Testing

- [ ] 9.1 Add unit tests for config validation — missing required fields, invalid paths
- [ ] 9.2 Add unit tests for filename sanitization and Markdown generation
- [ ] 9.3 Add unit tests for change detection logic (hash comparison against SQLite state)
- [ ] 9.4 Add integration test for conversion pipeline using a sample `.rm` fixture file
- [ ] 9.5 Add mocked API tests for OCR and vision steps (verify cache hit/miss behavior and error handling)
