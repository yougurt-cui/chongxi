#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-8.130.170.148}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/home/admin/projects/chongxi}"
REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
REMOTE_REPO="${REMOTE_REPO:-https://github.com/yougurt-cui/chongxi.git}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
APP_PORT="${APP_PORT:-15386}"

ssh -i "$SSH_KEY" -o BatchMode=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_DIR='$REMOTE_DIR' REMOTE_BRANCH='$REMOTE_BRANCH' REMOTE_REPO='$REMOTE_REPO' APP_PORT='$APP_PORT' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

cd "$REMOTE_DIR"
git config --global --add safe.directory "$REMOTE_DIR"

if [ ! -d .git ]; then
  echo "ERROR: $REMOTE_DIR is not a Git worktree. Run scripts/bootstrap_chongxi_git.sh first." >&2
  exit 2
fi

git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REMOTE_REPO"
git fetch origin "$REMOTE_BRANCH"
git checkout "$REMOTE_BRANCH"
git pull --ff-only origin "$REMOTE_BRANCH"

.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python -m py_compile \
  main.py \
  app_config.py \
  api/catfood_standardization_api.py \
  api/consumer_api.py \
  api/miniprogram_api.py \
  api/pipeline_api.py \
  api/product_identity_api.py \
  api/product_catalog_api.py \
  api/product_function_api.py \
  api/taobao_sku_api.py \
  services/catfood_standardization_service.py \
  services/formula_feature_link_service.py \
  services/formula_incremental_service.py \
  services/consumer_analysis_service.py \
  services/orchestrator_service.py \
  services/cat_food_task_service.py \
  services/cat_food_product_catalog_service.py \
  services/miniprogram_content_review_service.py \
  services/miniprogram_moment_service.py \
  services/miniprogram_moment_report_service.py \
  services/product_function_service.py \
  services/product_identity_service.py \
  services/taobao_sku_import_service.py \
  vendor/feature_score_pipeline/black_risk_done.py \
  vendor/feature_score_pipeline/soft_risk_done.py \
  vendor/feature_score_pipeline/scripts/build_catfood_score_wide_table.py \
  vendor/feature_score_pipeline/scripts/build_sku_feature_input.py \
  vendor/feature_score_pipeline/scripts/fat_material_remark.py \
  vendor/feature_score_pipeline/scripts/fat_score1.py \
  vendor/feature_score_pipeline/scripts/fiber_remark.py \
  vendor/feature_score_pipeline/scripts/fiber_remark_score.py \
  vendor/feature_score_pipeline/scripts/protein_score1.py \
  vendor/feature_score_pipeline/scripts/rebuild_protein_source_aggregate.py \
  scripts/rebuild_protein_source_from_profiles.py \
  vendor/csv_mysql_labeling/src/parse_catfood_ocr.py \
  vendor/csv_mysql_labeling/src/extract_catfood_brand_relations.py \
  vendor/feature_score_pipeline/scripts/brand_normalizer.py

for port in "$APP_PORT" 8502; do
  pid=$(ss -ltnp | sed -n "s/.*:$port.*pid=\([0-9][0-9]*\).*/\1/p" | head -n 1 || true)
  if [ -n "$pid" ]; then
    kill "$pid" || true
  fi
done

sleep 3
mkdir -p var
: > var/flask.log
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
nohup .venv/bin/python -m flask --app main run --host 0.0.0.0 --port "$APP_PORT" > var/flask.log 2>&1 < /dev/null &

for _ in $(seq 1 20); do
  if curl -fsS -I --max-time 3 "http://127.0.0.1:$APP_PORT/cat-food-compare.html" >/dev/null; then
    break
  fi
  sleep 1
done

ss -ltnp | grep -E ":($APP_PORT|8501|8502)" || true
curl -fsS -I --max-time 5 "http://127.0.0.1:$APP_PORT/cat-food-compare.html" | head -n 5
curl -fsS --retry 5 --retry-delay 1 --max-time 8 "http://127.0.0.1:$APP_PORT/api/cat-food-compare/brands" \
  | .venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); print("brand_count", len(data.get("brands", [])))'
curl -fsS --retry 5 --retry-delay 1 --max-time 8 "http://127.0.0.1:$APP_PORT/api/catfood/standardization/brands?limit=500" \
  | .venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); assert data.get("ok") is True; print("standard_brand_count", len(data.get("items", [])))'
REMOTE_SCRIPT
