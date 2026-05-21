## ADDED Requirements

### Requirement: Describe drawings and diagrams per page via AI vision model
The system SHALL send each page PNG to a configured AI vision model (default: Claude API) with a prompt instructing it to describe any diagrams, sketches, charts, or drawings present. The description SHALL be stored per page in the local database, keyed by page PNG hash.

#### Scenario: Page containing a diagram or sketch
- **WHEN** a page PNG is submitted to the vision model and the model identifies non-text visual content
- **THEN** the system SHALL store the returned natural-language description for that page

#### Scenario: Page containing only handwriting (no drawings)
- **WHEN** the vision model determines a page contains only handwritten text with no diagram or sketch elements
- **THEN** the system SHALL store an empty drawing description for that page

#### Scenario: Vision result cache hit
- **WHEN** a page PNG hash matches a previously stored vision result in the local database
- **THEN** the system SHALL use the cached description and SHALL NOT make an API call

#### Scenario: Vision API error
- **WHEN** the vision model API returns an error for a page
- **THEN** the system SHALL log the error, store a null description for that page, and continue processing remaining pages

### Requirement: Vision model and prompt configurable
The system SHALL read the vision model provider, model ID, and API key from `config.toml`. The default vision prompt SHALL be overridable via a `vision.prompt` field in `config.toml`.

#### Scenario: Custom vision prompt configured
- **WHEN** `vision.prompt` is set in `config.toml`
- **THEN** the system SHALL use that prompt string instead of the built-in default when calling the vision API

### Requirement: Drawing interpretation step can be disabled via configuration
The system SHALL support a `vision.enabled = false` flag in `config.toml` that skips all vision API calls, leaving drawing description fields empty in the output.

#### Scenario: Vision disabled
- **WHEN** `vision.enabled = false` is set in `config.toml`
- **THEN** the system SHALL skip all vision API calls and produce Markdown output with empty drawing description sections

### Requirement: Pages with no ink strokes may be skipped
The system SHALL inspect the `.rm` file metadata to detect pages that contain no stroke data. Such pages SHALL be skipped for the vision step without making an API call.

#### Scenario: Empty page detected via metadata
- **WHEN** a page's `.rm` file contains zero ink strokes according to its metadata
- **THEN** the system SHALL skip the vision API call for that page and record an empty description
