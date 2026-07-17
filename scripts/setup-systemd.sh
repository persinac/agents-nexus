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

sudo tee /etc/systemd/system/agents-nexus-arbiter.service << EOF
[Unit]
Description=agents-nexus arbiter (tmux to dashboard bridge)
After=agents-nexus.service

[Service]
User=${NEXUS_USER}
WorkingDirectory=${NEXUS_DIR}/arbiter
ExecStart=/usr/bin/node index.js
Restart=on-failure
Environment=PORT=8420

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

sudo tee /etc/systemd/system/spark-nightly.service << EOF
[Unit]
Description=Spark nightly reindex

[Service]
Type=oneshot
User=${NEXUS_USER}
WorkingDirectory=${NEXUS_DIR}
ExecStart=/bin/bash -c 'task spark:reclaim'
EOF

sudo tee /etc/systemd/system/spark-nightly.timer << 'EOF'
[Unit]
Description=Spark nightly reindex at 2am

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable agents-nexus agents-nexus-arbiter agents-nexus-flush.timer spark-nightly.timer
sudo systemctl start agents-nexus-flush.timer spark-nightly.timer

echo "Done. All units enabled."
echo "Docker stack is already running — will auto-start on next reboot."
echo "Verify with: systemctl list-timers"
