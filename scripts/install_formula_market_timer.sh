#!/usr/bin/env bash
# Run on the deployment host after migrate_formula_calculations.py.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cat > /etc/systemd/system/chongxi-market-updates.service <<EOF
[Unit]
Description=Chongxi coalesced market updates
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$project_dir
EnvironmentFile=-$project_dir/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$project_dir/.venv/bin/python $project_dir/scripts/run_formula_market_updates.py
Nice=10
IOSchedulingClass=idle
CPUQuota=50%
TimeoutStartSec=70min
UMask=0077
EOF
cat > /etc/systemd/system/chongxi-market-updates.timer <<EOF
[Unit]
Description=Schedule Chongxi market updates

[Timer]
OnBootSec=2min
OnUnitInactiveSec=5min
Unit=chongxi-market-updates.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now chongxi-market-updates.timer
systemctl list-timers chongxi-market-updates.timer --no-pager
