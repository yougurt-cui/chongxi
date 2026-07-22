# Comment Labeler API

宠物数据清洗流程。

Flask API service for cat food comment ingestion, OCR parsing, exception recycling, disease clue extraction, feature score pipelines, and brand process signal standardization.

## Setup

Create or activate the Python environment used by this project:

```bash
conda activate comment_labeler_env
```

Install dependencies from `requirements.txt` if needed:

```bash
pip install -r requirements.txt
```

Copy the example config and fill in local credentials:

```bash
cp vendor/csv_mysql_labeling/config/config.example.yaml vendor/csv_mysql_labeling/config/config.yaml
```

`config.yaml` is intentionally ignored by Git because it contains database passwords and API keys.

Environment variables can override config values:

```bash
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD=...
export MYSQL_DATABASE=csv_labeling
export DASHSCOPE_API_KEY=...
export QWEN_MODEL=qwen-plus
```

## Run

From the parent directory of `app`:

```bash
cd /Users/yoghourt/anaconda3/envs/comment_labeler_env
python -m flask --app app.main run --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Open `http://127.0.0.1:8000/` to use the workbench. The cat food comparison page is available at `http://127.0.0.1:8000/cat-food-compare.html`, and the official site is available at `http://127.0.0.1:8000/official-site.html`.

## Main APIs

WeChat mini-program food-change intent recognition (Qwen), product-catalog matching, and audit storage:

```http
POST /api/miniprogram/food-change/intent
Content-Type: application/json

{
  "user_id": "openid-or-business-user-id",
  "session_id": "conversation-id",
  "message": "我家三岁英短最近软便，正在吃皇家肠胃舒适，想换粮",
  "cat_status": {"weight_kg": 4.8, "neutered": true}
}
```

The first request creates `miniprogram_food_change_intent` in the configured application MySQL database.
Set `DASHSCOPE_API_KEY` (or `QWEN_API_KEY`) before calling the endpoint. Products are matched against
`catfood_product_catalog` in the configured feature database.

Mini-program product editing and ingredient lookup:

```http
GET  /api/miniprogram/products?brand=皇家&limit=50
POST /api/miniprogram/products/ingredients

{"catalog_key":"score:404"}
```

Collect Douyin/Xiaohongshu comments and archive source files:

```http
POST /api/consumer/comments/collect
```

Run ingredient OCR / OCR JSON parsing / guarantee parsing:

```http
POST /api/catfood/ingredients/ingest
```

Run brand process signal pipeline:

```http
POST /api/process-signals/run
```

Dry run:

```bash
curl -X POST http://127.0.0.1:8000/api/process-signals/run \
  -H 'Content-Type: application/json' \
  -d '{"dry_run": true}'
```

Pipeline orchestrator:

```http
POST /api/orchestrator/tasks
GET  /api/orchestrator/tasks/{task_id}
POST /api/orchestrator/tasks/{task_id}/run
POST /api/orchestrator/tasks/{task_id}/nodes/{node_code}/result
POST /api/orchestrator/dispatch-scan
POST /api/orchestrator/dispatch-claim
POST /api/orchestrator/dispatch-call-sync
```

Create and run a dry-run process signal task:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/tasks \
  -H 'Content-Type: application/json' \
  -d '{"task_type":"process_signal","payload":{"dry_run":true},"auto_run":true}'
```

Create a catfood ingredient ingest task:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/tasks \
  -H 'Content-Type: application/json' \
  -d '{"task_type":"catfood_ingredient_ingest","payload":{"incremental_only":true}}'
```

Run pending orchestrator tasks:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/dispatch-scan \
  -H 'Content-Type: application/json' \
  -d '{"limit":10}'
```

Write a node result and let the orchestrator run output checks:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/tasks/<task_id>/nodes/upload_check/result \
  -H 'Content-Type: application/json' \
  -d '{"call_status":"success","output":{}}'
```

Scan ready nodes, call the configured API synchronously, write the API response back, and run checks:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/dispatch-call-sync \
  -H 'Content-Type: application/json' \
  -d '{"limit":1,"task_type":"process_signal"}'
```

For long-running nodes, claim ready jobs and execute them asynchronously in a third-party worker:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/dispatch-claim \
  -H 'Content-Type: application/json' \
  -d '{"limit":1,"task_type":"catfood_image_analysis","node_codes":["ocr_formula"]}'
```

To claim a specific orchestrator task:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/dispatch-claim \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"<task_id>","node_codes":["ocr_formula"]}'
```

The claim response contains the API URL and input. After the worker finishes, write the node result back:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/tasks/<task_id>/nodes/<node_code>/result \
  -H 'Content-Type: application/json' \
  -d '{"call_status":"success","output":{"ocr_text":"..."}}'
```

Supported callback statuses:

- Success: `success`, `succeeded`, `ok`, `done`
- Failure: `failure`, `failed`, `fail`, `error`, `timeout`, `cancelled`, `canceled`
- Waiting: `waiting_result`, `waiting`, `pending`, `running`, `processing`

Failure callback example:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/tasks/<task_id>/nodes/<node_code>/result \
  -H 'Content-Type: application/json' \
  -d '{
    "call_status": "failure",
    "error_message": "Qwen OCR network timeout",
    "output": {"ocr_import_failed": 1}
  }'
```

When a node does not have a built-in API URL, pass an override:

```bash
curl -X POST http://127.0.0.1:8000/api/orchestrator/dispatch-call-sync \
  -H 'Content-Type: application/json' \
  -d '{
    "limit": 1,
    "task_type": "catfood_image_analysis",
    "node_codes": ["ocr_formula"],
    "api_overrides": {
      "ocr_formula": {
        "method": "POST",
        "url": "http://127.0.0.1:9000/ocr/formula",
        "timeout_seconds": 60
      }
    }
  }'
```

The `catfood_image_analysis` checks are:

- `upload_check`: image file exists and is under `var/cat_food_uploads`.
- `ocr_formula`: `ocr_text` is non-empty, contains OCR keywords, effective character ratio is at least `0.7`, repeated 8-gram ratio is not above `0.35`, and nutrition values are detected.
- `ingredient_extract`: required parsed fields are `image_path`, `image_name`, `file_sha256`, `brand`, `product_name`, `ingredient_composition`, `ocr_text`, and `ocr_json`.
- `ingredient_standardize`: required positive score fields are `protein_structure_score`, `protein_quality_score`, `fat_oily_score`, `fat_regulation_score`, `fat_score`, `omega_imbalance_score`, `fat_mix_complexity_score`, `p_form_score`, `p_bulk_score`, `p_buffer`, `p_total_score`, `q_feed`, `q_scfa`, `q_total_score`, and `starch_burden_score`.
- `formula_profile`: required fields are `protein_score`, `fat_burden_score`, `carb_burden_score`, and `fiber_support_score`.

Exception queue:

```http
POST  /api/exceptions/recycle
GET   /api/exceptions
GET   /api/exceptions/gate
PATCH /api/exceptions/{id}
POST  /api/exceptions/{id}/claim
POST  /api/exceptions/{id}/fixed
POST  /api/exceptions/{id}/release
```

## Pre-Commit Check

```bash
python -m py_compile $(find . -name "*.py" -not -path "./__pycache__/*")
```
