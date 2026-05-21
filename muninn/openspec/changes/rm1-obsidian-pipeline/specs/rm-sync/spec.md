## ADDED Requirements

### Requirement: Pull new and modified notebooks from reMarkable 1 over SSH
The system SHALL connect to the reMarkable 1 tablet via SSH and use rclone to pull notebooks (`.rm` files, `.metadata` files, and `.content` files) that are new or have changed since the last sync. The system SHALL record the last-synced state in a local SQLite database keyed by notebook UUID and file hash.

#### Scenario: New notebook detected on device
- **WHEN** a notebook exists on the tablet that has no entry in the local state database
- **THEN** the system SHALL download all associated files (`.rm`, `.metadata`, `.content`) to a local staging directory

#### Scenario: Modified notebook detected on device
- **WHEN** a notebook exists on the tablet whose file hash differs from the stored hash in the local state database
- **THEN** the system SHALL re-download all associated files for that notebook to the local staging directory

#### Scenario: Unchanged notebook detected on device
- **WHEN** a notebook exists on the tablet whose file hash matches the stored hash in the local state database
- **THEN** the system SHALL skip that notebook and produce no output for it

#### Scenario: Tablet unreachable
- **WHEN** the SSH connection to the tablet cannot be established within a configurable timeout
- **THEN** the system SHALL log an error, skip the sync step, and exit without modifying local state

### Requirement: Configuration-driven SSH connection
The system SHALL read SSH host, port, username, and identity file path from a `config.toml` file. Credentials SHALL NOT be hardcoded.

#### Scenario: Valid SSH configuration
- **WHEN** `config.toml` contains a valid SSH host and identity file path
- **THEN** the system SHALL connect to the tablet using those credentials without prompting for a password

#### Scenario: Missing identity file
- **WHEN** the identity file path in `config.toml` does not exist on disk
- **THEN** the system SHALL raise a configuration error on startup and exit before attempting any sync

### Requirement: Tailscale as off-network transport
The system SHALL support connecting to the reMarkable 1 over a Tailscale address when the tablet and host are not on the same local network. The SSH host in `config.toml` MAY be a Tailscale hostname or IP (e.g., `remarkable.tail12345.ts.net`). No special handling is required beyond using the configured host — Tailscale handles the transport transparently. The system SHALL NOT bundle or manage the Tailscale daemon; it MUST already be running on both the host machine and the tablet before sync is invoked.

#### Scenario: Sync over Tailscale when off local network
- **WHEN** the configured SSH host is a Tailscale address and both the host machine and tablet have active Tailscale connections
- **THEN** the system SHALL connect and sync successfully using the same SSH code path as local network sync

#### Scenario: Tailscale not running on tablet
- **WHEN** the configured SSH host is a Tailscale address but the tablet is not connected to Tailscale
- **THEN** the system SHALL fail with the same SSH timeout/unreachable error as any other unreachable host and log accordingly

### Requirement: Dry-run mode
The system SHALL support a `--dry-run` flag that reports which notebooks would be synced without downloading any files or modifying local state.

#### Scenario: Dry run with pending changes
- **WHEN** the user runs `muninn sync --dry-run` and new notebooks exist on the tablet
- **THEN** the system SHALL print the list of notebooks that would be downloaded and exit without writing any files
