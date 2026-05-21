## Context

The reMarkable 1 stores notes in a proprietary vector format (`.rm` files) organized in a JSON-described tree on a Linux filesystem. The device has root SSH access and supports Toltec-installed utilities including `rclone`. Notes accumulate over time and are currently never reviewed because there is no path from device → searchable knowledge base.

The target end state is a locally-run daemon/cron process that detects new or modified notebooks, processes them through OCR and AI vision, and deposits structured Markdown notes into an Obsidian vault — fully automated, no manual steps after initial setup.

**Constraints:**
- Device is a reMarkable 1 (not Paper Pro) — root SSH access available, Toltec-compatible
- Must work without reMarkable's official cloud (self-hosted preference)
- Obsidian vault may be on local disk or synced folder (iCloud/Syncthing)
- Owner is a single user; no multi-tenancy needed

## Goals / Non-Goals

**Goals:**
- Automatically detect and pull new/modified notebooks from rM1 over SSH/rclone
- Convert `.rm` files to a processable image format (PNG per page)
- Transcribe handwritten text via MyScript API
- Describe drawings/diagrams via an AI vision model API
- Store original assets (PDF/PNG) in S3 with deterministic key paths
- Write Markdown notes into Obsidian vault, one file per notebook, with embedded transcription and drawing descriptions
- Idempotent processing — re-running on already-processed notebooks produces the same output without duplication

**Non-Goals:**
- Real-time sync (polling interval of minutes is acceptable)
- Multi-user or multi-device support beyond the owner's rM1
- Two-way sync (Obsidian → reMarkable)
- On-device processing (all heavy work runs on the host machine)
- GUI or web interface
- Replacing Obsidian's own sync mechanism (Muninn only writes files)

## Decisions

### 1. Pull-based sync over SSH rather than on-device rclone push
**Decision**: Run `rclone` on the host machine to pull from rM1 over SSH, rather than installing rclone on the tablet to push to S3 directly.

**Rationale**: Keeps the tablet lightweight; avoids the risk of a buggy cron job on the tablet draining battery or corrupting state. All processing logic lives in one place (host). SSH pull is reliable and rM1 is frequently on the same Wi-Fi network.

**Alternative considered**: On-device rclone push via systemd timer — rejected due to battery impact and harder debugging.

### 2. `rmrl` for `.rm` → PDF/PNG conversion (vision input)
**Decision**: Use `rmrl` (Python) to convert `.rm` notebook files to PDF, then `pdf2image` to rasterize pages to PNG. PNGs feed the drawing-interpretation step (vision), **not OCR**.

**Rationale**: `rmrl` handles RM1 `.rm` v3/v5 formats and produces faithful page-level output. PNG rasterization remains necessary for the vision model, which is raster-based.

**Alternative considered**: `rM2svg` — produces SVGs but doesn't handle all `.rm` features cleanly; PDF→PNG pipeline is more universal.

### 2a. `remarkable-layers` (`rmlines`) for direct `.rm` stroke extraction (OCR input)
**Decision**: Parse RM1 `.rm` v3/v5 files directly into vector strokes using the `remarkable-layers` library (`rmlines` module), and submit those strokes to MyScript. We do **not** rasterize for OCR.

**Rationale**: MyScript iink Cloud accepts only vector ink (strokes), never raster images — confirmed against MyScript docs. `rmscene` (the most active modern library) is v6-only and rejects RM1's v3/v5 headers, so it cannot be used here. `remarkable-layers` is unmaintained but the v3/v5 binary format is frozen (RM1 is end-of-life on firmware ~3.11), so an unmaintained parser for a frozen format is acceptable. The library exposes per-stroke point arrays with `x, y, pressure` in screen-pixel space (1404 × 1872, 226 DPI), which maps cleanly to MyScript's `strokeGroups[].strokes[].{x,y,p}` schema. RM1 does not record per-point timestamps; we omit `t[]` or synthesize incrementing values.

**Alternative considered**: Vendor the stroke parser from `rmrl` internals — works but creates a private API dependency on an upstream we don't control. `rmscene` — rejected, v6-only.

### 3. MyScript iink Cloud REST API for handwriting OCR (strokes only)
**Decision**: Submit vector strokes to MyScript iink Cloud (`POST https://cloud.myscript.com/api/v4.0/iink/batch`, `contentType: "Text"`). Authenticate with `applicationKey` header (literal) + `hmac` header (HMAC-SHA512 over request body, key = `application_key + hmac_key`). Parse the JIIX response and extract the top-level `label` field as the recognized text.

**Rationale**: reMarkable's own built-in conversion uses MyScript under the hood, so it is best calibrated for stroke data style. iink Cloud is the only currently-supported MyScript product; the legacy CDK that accepted images was discontinued. No official Python SDK exists — we make raw HTTP calls. Free tier allows 2,000 requests/month, sufficient for personal use with caching.

**Alternative considered**: Tesseract OCR — free but significantly worse on handwriting. Cloud vision OCR (Google Vision Read, Azure Read) — accept raster but worse on cursive than MyScript; viable fallback if MyScript becomes cost-prohibitive.

### 4. AI vision model for drawing interpretation
**Decision**: Use a Claude API call (vision capability) to describe drawings/diagrams per page. Pages with no detected ink strokes beyond handwriting can be skipped.

**Rationale**: Claude's vision is strong at interpreting sketches, flowcharts, and rough diagrams with natural language context. The pipeline already has page PNGs ready for MyScript, so the same images can be routed to vision with no extra conversion.

**Alternative considered**: GPT-4o vision — equally capable; Claude preferred as owner is already an Anthropic user. OpenAI is a reasonable drop-in if needed.

### 5. One Markdown file per notebook, updated on re-process
**Decision**: Each rM1 notebook maps to a single `.md` file in Obsidian, named after the notebook title. Re-processing overwrites the file in place.

**Rationale**: Keeps vault structure clean and mirrors how the user organizes notes on the tablet. Overwrite-on-reprocess is safe because the source of truth is always the tablet.

**Alternative considered**: One file per page — creates too many small files and breaks the notebook-as-document mental model.

### 6. S3 for asset storage, not for Markdown
**Decision**: Push raw PDFs and per-page PNGs to S3 under a path like `muninn/<notebook-id>/<date>/`. Markdown notes go directly into the Obsidian vault directory on disk; S3 is not in the Obsidian sync path.

**Rationale**: Obsidian works best with local files. S3 serves as a durable backup/archive of originals. Decouples storage concerns from the knowledge-base concern.

### 7. State tracking via a local SQLite database
**Decision**: Maintain a small SQLite file (`muninn.db`) recording each notebook's last-processed hash. Used to detect changes and skip already-processed unchanged notebooks.

**Rationale**: Simpler than file-based timestamps or relying on filesystem mtimes across SSH. Hash-based detection is robust to clock skew between tablet and host.

**Alternative considered**: Store state in S3 — adds latency and a network dependency to what should be a fast "nothing new" check.

## Risks / Trade-offs

- **rM1 firmware update breaks SSH or `.rm` format** → Mitigation: pin firmware version; monitor community channels (r/RemarkableTablet) for breaking changes.
- **MyScript API cost at scale** → Mitigation: cache OCR results in SQLite per page hash; only re-OCR changed pages.
- **AI vision calls add latency and cost per drawing page** → Mitigation: heuristic skip (no drawing strokes detected in `.rm` metadata → skip vision call); user can also disable vision step via config flag.
- **rmrl doesn't perfectly render all rM1 templates** → Mitigation: store original `.rm` files in S3 as fallback; PNG rendering is best-effort for vision input, not archival display.
- **`remarkable-layers` is unmaintained** → Mitigation: the `.rm` v3/v5 format is frozen (RM1 is no longer receiving format-breaking updates). Vendor the parser into `src/muninn/` if upstream disappears from PyPI.
- **MyScript free tier (2,000 requests/month) exhausted on first full sync** → Mitigation: `--folder PREFIX` flag scopes the first sync to a subset of notebooks; SQLite cache (`pages.ocr_text` keyed by `png_hash`) ensures no page is OCR'd twice.
- **Obsidian vault on iCloud may have sync conflicts if pipeline writes while Obsidian is open** → Mitigation: write to a temp file, then atomic rename; document that vault should be closed during sync or use a dedicated `Muninn/` subfolder.

## Migration Plan

1. Clone repo and run `pip install -r requirements.txt` on host machine
   2. Please use uv pacakage manager. Not raw pip.
   3. Include taskfile that will run common commands (lint, format, test, run, etc)
2. Configure `config.toml`: rM1 SSH host/key, MyScript API key, S3 credentials, Obsidian vault path
3. Test connection: `muninn check` — verifies SSH, S3, and API reachability
4. Run first sync manually: `muninn sync --dry-run` to preview, then `muninn sync`
5. Install cron job or launchd plist for automated polling (e.g., every 15 minutes)

**Rollback**: No destructive operations on the tablet. Obsidian vault files can be deleted; S3 bucket can be cleared. SQLite state file can be deleted to force full re-process.

## Open Questions

- Should drawing interpretation be opt-in per notebook (via a tag/template on the tablet) or always-on with a global config toggle?
  - Global config toggle
- Is there a preferred Obsidian subfolder structure (flat `Muninn/` folder, or mirror the rM1 folder tree)?
  - Muninn/ will do. 
  - I would like to explore multiple vaults. For instance, I have a work vault and a personal vault. 
- Should the pipeline notify the user on completion (e.g., macOS notification, Slack message)?
  - Slack Message would be sick
- MyScript vs. feeding the raw stroke XML directly to a vision model for combined OCR+interpretation in one API call — worth exploring to reduce round trips?
  - MyScript is fine to start
- Ensure that device sync'ing / pulling can occur even when the notebook is not on the same network (traveling or w/e)
