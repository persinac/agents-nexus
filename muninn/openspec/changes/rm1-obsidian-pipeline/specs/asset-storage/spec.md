## ADDED Requirements

### Requirement: Upload raw and converted assets to S3-compatible storage
The system SHALL upload the notebook PDF and all page PNGs to an S3-compatible bucket after conversion. Assets SHALL be stored under a deterministic key path: `muninn/<notebook-uuid>/<ISO-8601-date>/`.

#### Scenario: Successful upload of notebook assets
- **WHEN** conversion completes for a notebook with UUID `<uuid>` processed on date `<date>`
- **THEN** the system SHALL upload `notebook.pdf` to `muninn/<uuid>/<date>/notebook.pdf` and each `page_N.png` to `muninn/<uuid>/<date>/pages/page_N.png`

#### Scenario: Asset already exists in S3 (idempotent re-upload)
- **WHEN** an asset with the same S3 key already exists in the bucket
- **THEN** the system SHALL skip the upload for that asset and log a debug message

#### Scenario: S3 upload failure
- **WHEN** an S3 upload returns an error for one or more files
- **THEN** the system SHALL log the error, mark the notebook as upload-failed in the local database, and continue processing other notebooks without retrying in the same run

### Requirement: S3 credentials and bucket from configuration
The system SHALL read the S3 endpoint, bucket name, access key ID, and secret access key from `config.toml`. The system SHALL support any S3-compatible endpoint (AWS S3, Backblaze B2, MinIO, etc.).

#### Scenario: Non-AWS S3-compatible endpoint configured
- **WHEN** `storage.endpoint` is set to a non-AWS URL in `config.toml`
- **THEN** the system SHALL use that endpoint for all S3 operations instead of the default AWS endpoint

#### Scenario: Missing bucket name at startup
- **WHEN** `storage.bucket` is absent from `config.toml` and asset storage is not disabled
- **THEN** the system SHALL raise a configuration error on startup and exit before processing any notebooks

### Requirement: Asset storage step can be disabled via configuration
The system SHALL support a `storage.enabled = false` flag in `config.toml` that skips all S3 uploads, keeping assets only in the local staging directory.

#### Scenario: Storage disabled
- **WHEN** `storage.enabled = false` is set in `config.toml`
- **THEN** the system SHALL skip all S3 upload operations and retain converted assets only on local disk
