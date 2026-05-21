## ADDED Requirements

### Requirement: Transcribe handwritten text per page via MyScript iink Cloud API
The system SHALL extract vector strokes from each notebook page's `.rm` file and submit them to the MyScript iink Cloud REST endpoint (`POST /api/v4.0/iink/batch`, `contentType: "Text"`) to retrieve recognized plain text. The system SHALL NOT send raster images for OCR — MyScript iink Cloud only accepts vector ink. Results SHALL be stored in the per-notebook SQLite record keyed by page index, with the page PNG hash retained as the cache key (PNGs are deterministically derived from strokes, so the existing `png_hash` correctly invalidates cached transcriptions when strokes change).

#### Scenario: Successful transcription of a handwritten page
- **WHEN** strokes extracted from a `.rm` page containing handwritten text are submitted to the MyScript API
- **THEN** the system SHALL store the JIIX `label` field (top-level recognized text) associated with that page index

#### Scenario: Page with no handwriting (blank or drawing-only)
- **WHEN** the MyScript API returns a JIIX response with an empty `label` or no recognized words
- **THEN** the system SHALL record an empty transcription for that page and not treat it as an error

#### Scenario: OCR result cache hit
- **WHEN** a page's `png_hash` matches a row in the `pages` table whose `ocr_text` is non-NULL
- **THEN** the system SHALL use the cached transcription and SHALL NOT make an API call

#### Scenario: MyScript API error (non-200 response)
- **WHEN** the MyScript API returns a non-200 HTTP status for a page submission
- **THEN** the system SHALL log the error, store a NULL transcription for that page, and continue processing remaining pages

### Requirement: MyScript API credentials from configuration
The system SHALL read `[ocr].application_key` and `[ocr].hmac_key` from `config.toml`. Requests SHALL be authenticated with two headers:
- `applicationKey`: the application key literal
- `hmac`: a hex HMAC-SHA512 digest computed over the exact request body bytes, using the concatenation `application_key + hmac_key` as the HMAC key

The system SHALL NOT embed credentials in source code or log files.

#### Scenario: Missing credentials at startup
- **WHEN** either `[ocr].application_key` or `[ocr].hmac_key` is absent from `config.toml` and `[ocr].enabled = true`
- **THEN** the system SHALL raise a configuration error on startup and exit before processing any notebooks

### Requirement: OCR step can be disabled via configuration
The system SHALL support a `[ocr].enabled = false` flag in `config.toml` that skips the handwriting recognition step entirely, leaving transcription fields empty in the output.

#### Scenario: OCR disabled
- **WHEN** `[ocr].enabled = false` is set in `config.toml`
- **THEN** the system SHALL skip all MyScript API calls and produce Markdown output with empty transcription sections
