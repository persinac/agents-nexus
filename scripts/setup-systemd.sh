#!/usr/bin/env bash
set -euo pipefail

# Phase 7 — systemd units for agents-nexus on the mini PC
# Run with: sudo bash scripts/setup-systemd.sh

sudo tee /etc/systemd/system/agents-nexus.service << 'EOF'
[Unit]
Description=agents-nexus Docker stack
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=persinac
WorkingDirectory=/home/persinac/repos/agents-nexus
ExecStart=/usr/bin/docker compose up --no-recreate -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/agents-nexus-arbiter.service << 'EOF'
[Unit]
Description=agents-nexus arbiter (tmux to dashboard bridge)
After=agents-nexus.service

[Service]
User=persinac
WorkingDirectory=/home/persinac/repos/agents-nexus/arbiter
ExecStart=/usr/bin/node index.js
Restart=on-failure
Environment=PORT=8420

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/agents-nexus-flush.service << 'EOF'
[Unit]
Description=Flush agent memory events

[Service]
Type=oneshot
User=persinac
WorkingDirectory=/home/persinac/repos/agents-nexus
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

sudo tee /etc/systemd/system/spark-nightly.service << 'EOF'
[Unit]
Description=Spark nightly reindex

[Service]
Type=oneshot
User=persinac
WorkingDirectory=/home/persinac/repos/agents-nexus
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
