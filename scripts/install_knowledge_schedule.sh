#!/usr/bin/env bash
# Run on the deployment host. The calendar uses Asia/Shanghai explicitly.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
calendar="${KNOWLEDGE_JOB_CALENDAR:-*-*-* 02:00:00 Asia/Shanghai}"
max_posts="${KNOWLEDGE_JOB_MAX_POSTS:-500}"
[[ "$max_posts" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid KNOWLEDGE_JOB_MAX_POSTS' >&2; exit 2; }
systemd-analyze calendar "$calendar" >/dev/null
cat > /etc/systemd/system/chongxi-knowledge.service <<EOF
[Unit]
Description=Chongxi two-stage knowledge production
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$project_dir
EnvironmentFile=-$project_dir/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$project_dir/.venv/bin/python $project_dir/scripts/run_knowledge_schedule.py --max-posts $max_posts
TimeoutStartSec=12h
UMask=0077
EOF
cat > /etc/systemd/system/chongxi-knowledge.timer <<EOF
[Unit]
Description=Daily Chongxi knowledge production

[Timer]
OnCalendar=$calendar
Persistent=true
RandomizedDelaySec=120
Unit=chongxi-knowledge.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now chongxi-knowledge.timer
systemctl list-timers chongxi-knowledge.timer --no-pager
