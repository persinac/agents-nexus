#!/usr/bin/env bash
set -euo pipefail

# Phase 7 — systemd units for agents-nexus on the mini PC
# Run with: sudo bash scripts/setup-systemd.sh

# Derive the target user + repo dir from the invoking environment (not hardcoded), so
# this works for any Linux user. Under sudo, $SUDO_USER is the real invoker ($USER=root).
NEXUS_USER="${SUDO_USER:-$USER}"
NEXUS_HOME="$(getent passwd "$NEXUS_USER" | cut -d: -f6)"
NEXUS_DIR="${AGENTS_NEXUS_DIR:-$NEXUS_HOME/repos/agents-nexus}"
echo "Installing units for user=$NEXUS_USER, dir=$NEXUS_DIR"

sudo tee /etc/systemd/system/agents-nexus.service << EOF
[Unit]
Description=agents-nexus Docker stack
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=${NEXUS_USER}
WorkingDirectory=${NEXUS_DIR}
ExecStart=/usr/bin/docker compose up --no-recreate -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/agents-nexus-flush.service << EOF
[Unit]
Description=Flush agent memory events

[Service]
Type=oneshot
User=${NEXUS_USER}
WorkingDirectory=${NEXUS_DIR}
ExecStart=/bin/bash -c 'task mnemon:flush'
EOF

sudo tee /etc/systemd/system/agents-nexus-flush.timer << 'EOF'
[Unit]
Description=Flush agent memory events every 2 minutes

[Timer]
OnBootSec=30
OnUnitActiveSec=120

[Install]
WantedBy=timers.target
EOF

# doc-vault: long-running server, so Restart=always rather than a timer. Code is in
# the repo; the vault (docs/, index.db, config.json) is DOCVAULT_HOME and is not.
# Needs python 3.9+ (verified on 3.9.6 and 3.14.2).
sudo tee /etc/systemd/system/agents-nexus-doc-vault.service << EOF
[Unit]
Description=doc-vault — index and serve agent-authored HTML docs
After=network-online.target

[Service]
Type=simple
User=${NEXUS_USER}
WorkingDirectory=${NEXUS_DIR}/doc-vault
Environment=HOME=${NEXUS_HOME}
Environment=DOCVAULT_HOME=${NEXUS_HOME}/doc-vault
ExecStart=/usr/bin/env python3 ${NEXUS_DIR}/doc-vault/docvault.py serve --watch 120
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable agents-nexus agents-nexus-flush.timer agents-nexus-doc-vault
sudo systemctl start agents-nexus-flush.timer agents-nexus-doc-vault

echo "Done. All units enabled."
echo "Docker stack is already running — will auto-start on next reboot."
echo "Verify with: systemctl list-timers"
