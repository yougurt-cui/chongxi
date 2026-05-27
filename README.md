# Comment Labeler API

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

## Main APIs

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
