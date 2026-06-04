#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-8.130.170.148}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/home/admin/projects/chongxi}"
REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
REMOTE_REPO="${REMOTE_REPO:-https://github.com/yougurt-cui/chongxi.git}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"

ssh -i "$SSH_KEY" -o BatchMode=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_DIR='$REMOTE_DIR' REMOTE_BRANCH='$REMOTE_BRANCH' REMOTE_REPO='$REMOTE_REPO' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

if [ ! -d "$REMOTE_DIR" ]; then
  mkdir -p "$REMOTE_DIR"
fi

cd "$REMOTE_DIR"
git config --global --add safe.directory "$REMOTE_DIR"

backup_dir="/home/admin/projects/chongxi_pre_git_$(date +%Y%m%d_%H%M%S)"
cp -a "$REMOTE_DIR" "$backup_dir"
echo "backup=$backup_dir"

config_backup=""
if [ -f vendor/csv_mysql_labeling/config/config.yaml ]; then
  config_backup="/tmp/chongxi_config_$(date +%Y%m%d_%H%M%S).yaml"
  cp vendor/csv_mysql_labeling/config/config.yaml "$config_backup"
fi

if [ ! -d .git ]; then
  git init
fi

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_REPO"
else
  git remote add origin "$REMOTE_REPO"
fi

find . -mindepth 1 -maxdepth 1 \
  ! -name .git \
  ! -name .venv \
  ! -name var \
  -exec rm -rf {} +

git fetch origin "$REMOTE_BRANCH"
git checkout -B "$REMOTE_BRANCH" "origin/$REMOTE_BRANCH"

if [ -n "$config_backup" ]; then
  mkdir -p vendor/csv_mysql_labeling/config
  cp "$config_backup" vendor/csv_mysql_labeling/config/config.yaml
  rm -f "$config_backup"
fi

mkdir -p var /home/admin/data/chongxi/images/cat_food_uploads
chown -R admin:admin "$REMOTE_DIR" /home/admin/data/chongxi

echo "remote git worktree ready: $REMOTE_DIR"
REMOTE_SCRIPT
