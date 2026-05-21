## ADDED Requirements

### Requirement: Generate one Markdown file per notebook and write it to the Obsidian vault
The system SHALL produce a single `.md` file for each processed notebook. The file SHALL be named after the notebook title (sanitized for filesystem use) and written to the resolved Obsidian vault directory for that notebook. If a file with the same name already exists, it SHALL be overwritten atomically (write to temp file, then rename).

#### Scenario: New notebook written to vault
- **WHEN** a notebook has been fully processed (conversion, OCR, vision) and no corresponding `.md` file exists in the target vault
- **THEN** the system SHALL create a new `.md` file in the resolved vault directory with the notebook's title as the filename

#### Scenario: Existing notebook file overwritten on reprocess
- **WHEN** a notebook is reprocessed (e.g., new pages added on device) and a `.md` file already exists in the target vault
- **THEN** the system SHALL overwrite the existing file atomically without leaving a partially-written file at any point

#### Scenario: Notebook title contains filesystem-unsafe characters
- **WHEN** a notebook title contains characters that are illegal in filenames (e.g., `/`, `:`, `?`, `*`)
- **THEN** the system SHALL replace each unsafe character with an underscore in the output filename

### Requirement: Markdown file structure
Each generated Markdown file SHALL follow a defined structure:
- YAML frontmatter with: `title`, `notebook_id` (UUID), `created_at`, `updated_at`, `page_count`, `source: reMarkable`, `rm_folder` (the rM1 folder path of the notebook)
- One section per page containing: the page's OCR transcription (if any) and the page's drawing description (if any)
- Pages with empty transcription AND empty drawing description SHALL still appear as a section with a placeholder note.

#### Scenario: Page with both OCR text and drawing description
- **WHEN** a page has a non-empty transcription and a non-empty drawing description
- **THEN** the Markdown section for that page SHALL contain both under separate sub-headings (`### Transcription` and `### Drawing`)

#### Scenario: Page with OCR text only
- **WHEN** a page has a non-empty transcription and an empty drawing description
- **THEN** the Markdown section SHALL contain only the `### Transcription` sub-heading with its text

#### Scenario: Page with no content
- **WHEN** a page has both empty transcription and empty drawing description
- **THEN** the Markdown section SHALL contain a `*No content detected*` placeholder

### Requirement: Multiple vault configuration with folder-based routing
The system SHALL support multiple named Obsidian vaults defined as an array in `config.toml`. Each vault entry SHALL specify a `path` and an optional list of `folders` (rM1 folder prefixes that route to that vault). A notebook's rM1 folder path SHALL be matched against vault `folders` patterns; the first match wins. One vault MUST be designated `default = true` and SHALL receive all notebooks that do not match any other vault's folder patterns.

Config shape:
```toml
[[vaults]]
name = "work"
path = "/path/to/work-vault"
subfolder = "Muninn"
folders = ["Work/"]

[[vaults]]
name = "personal"
path = "/path/to/personal-vault"
subfolder = "Muninn"
default = true
```

#### Scenario: Notebook in a matched rM1 folder routes to correct vault
- **WHEN** a notebook's rM1 folder path starts with `Work/` and a vault is configured with `folders = ["Work/"]`
- **THEN** the system SHALL write the notebook's Markdown file to that vault's path

#### Scenario: Notebook not matching any folder pattern routes to default vault
- **WHEN** a notebook's rM1 folder path does not match any vault's `folders` patterns
- **THEN** the system SHALL write the notebook's Markdown file to the vault marked `default = true`

#### Scenario: No default vault configured
- **WHEN** no vault in the config has `default = true`
- **THEN** the system SHALL raise a configuration error on startup and exit

#### Scenario: Multiple folder prefixes on one vault
- **WHEN** a vault is configured with `folders = ["Work/", "Clients/"]`
- **THEN** notebooks whose rM1 folder path starts with either prefix SHALL route to that vault

### Requirement: Per-vault subfolder support
Each vault entry SHALL support an optional `subfolder` field. When set, Muninn-generated notes SHALL be placed in that subdirectory within the vault, created if it does not exist. When absent, notes SHALL be written directly to the vault root.

#### Scenario: Subfolder configured on vault
- **WHEN** a vault entry has `subfolder = "Muninn"` and a notebook routes to that vault
- **THEN** the system SHALL write the `.md` file to `<vault_path>/Muninn/` and create the directory if needed

### Requirement: All vault paths must exist at startup
The system SHALL verify that every configured vault `path` exists on disk during startup. If any vault path does not exist, the system SHALL raise a configuration error and exit.

#### Scenario: A vault path does not exist
- **WHEN** any vault entry's `path` does not exist on the filesystem at startup
- **THEN** the system SHALL log a descriptive error naming the missing vault and exit without processing any notebooks
