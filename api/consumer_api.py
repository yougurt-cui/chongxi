"""Consumer analysis API entrypoints."""


from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock
from uuid import uuid4

from flask import Blueprint, jsonify, request
from sqlalchemy import create_engine, text

import app_config
from services.consumer_analysis_service import (
    calculate_material_scores,
    calculate_risk_scores,
    collect_consumer_comments,
    engineer_consumer_features,
    structure_consumer_disease_clues,
)


consumer_api = Blueprint("consumer_api", __name__, url_prefix="/api/consumer")
MAX_DISEASE_STRUCTURE_GET_LIMIT = 50
DEFAULT_DISEASE_STRUCTURE_JOB_LIMIT = 100
MAX_DISEASE_STRUCTURE_JOB_LIMIT = 10000
DEFAULT_DISEASE_STRUCTURE_CHUNK_SIZE = 100
MAX_DISEASE_STRUCTURE_CHUNK_SIZE = 200
DEFAULT_DISEASE_STRUCTURE_BATCH_SIZE = 10
MAX_DISEASE_STRUCTURE_BATCH_SIZE = 20
DEFAULT_DISEASE_STRUCTURE_MAX_ERRORS = 100
MAX_DISEASE_STRUCTURE_RETRIES = 1
DISEASE_STRUCTURE_CURSOR_TABLE = "cat_disease_structure_cursor"
_disease_structure_executor = ThreadPoolExecutor(max_workers=1)
_disease_structure_jobs = {}
_disease_structure_jobs_lock = Lock()


@consumer_api.post("/features/engineer")
def consumer_features_engineer():
    payload = request.get_json(silent=True) or {}
    try:
        result = engineer_consumer_features(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@consumer_api.post("/comments/collect")
def consumer_comments_collect():
    payload = request.get_json(silent=True) or {}
    try:
        result = collect_consumer_comments(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def _disease_structure_payload() -> dict:
    if request.method == "GET":
        payload = request.args.to_dict()
        for key in ("limit", "batch_size", "min_id", "max_id", "max_retries", "max_comment_chars"):
            if key in payload and payload[key] != "":
                payload[key] = int(payload[key])
        payload["limit"] = min(int(payload.get("limit") or MAX_DISEASE_STRUCTURE_GET_LIMIT), MAX_DISEASE_STRUCTURE_GET_LIMIT)
        payload["batch_size"] = min(int(payload.get("batch_size") or 10), 10)
        payload["max_retries"] = min(int(payload.get("max_retries") or 1), 1)
        if "skip_existing" in payload:
            payload["skip_existing"] = payload["skip_existing"].lower() not in {"0", "false", "no"}
        return payload
    return request.get_json(silent=True) or {}


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _normalize_disease_structure_payload(payload: dict) -> dict:
    payload = dict(payload or {})
    requested_limit = _clamp_int(
        payload.get("limit"),
        DEFAULT_DISEASE_STRUCTURE_JOB_LIMIT,
        1,
        1_000_000_000,
    )
    accepted_limit = min(requested_limit, MAX_DISEASE_STRUCTURE_JOB_LIMIT)
    chunk_size = _clamp_int(
        payload.get("chunk_size"),
        DEFAULT_DISEASE_STRUCTURE_CHUNK_SIZE,
        1,
        min(MAX_DISEASE_STRUCTURE_CHUNK_SIZE, accepted_limit),
    )
    batch_size = _clamp_int(
        payload.get("batch_size"),
        DEFAULT_DISEASE_STRUCTURE_BATCH_SIZE,
        1,
        MAX_DISEASE_STRUCTURE_BATCH_SIZE,
    )
    max_errors = _clamp_int(payload.get("max_errors"), DEFAULT_DISEASE_STRUCTURE_MAX_ERRORS, 0, 10000)
    max_retries = _clamp_int(payload.get("max_retries"), MAX_DISEASE_STRUCTURE_RETRIES, 1, MAX_DISEASE_STRUCTURE_RETRIES)

    payload["requested_limit"] = requested_limit
    payload["accepted_limit"] = accepted_limit
    payload["limit"] = accepted_limit
    payload["chunk_size"] = chunk_size
    payload["batch_size"] = batch_size
    payload["max_errors"] = max_errors
    payload["max_retries"] = max_retries
    payload["source_table"] = payload.get("source_table") or "catfood_brand_health_candidates"
    payload["target_table"] = payload.get("target_table") or "cat_disease_clue_candidates"

    manual_range = payload.get("min_id") not in (None, "") or payload.get("max_id") not in (None, "")
    payload["update_cursor"] = _as_bool(payload.get("update_cursor"), default=not manual_range)
    if payload.get("min_id") not in (None, ""):
        payload["min_id"] = int(payload["min_id"])
    else:
        payload.pop("min_id", None)
    if payload.get("max_id") not in (None, ""):
        payload["max_id"] = int(payload["max_id"])
    else:
        payload.pop("max_id", None)
    return payload


def _mysql_url(db_config: dict) -> str:
    return (
        f"mysql+pymysql://{db_config['user']}:{db_config.get('password') or ''}"
        f"@{db_config['host']}:{int(db_config.get('port', 3306))}/{db_config['database']}"
        f"?charset={db_config.get('charset', 'utf8mb4')}"
    )


def _get_engine(db_payload: dict | None = None):
    db_config = app_config.get_mysql_config(payload_db=db_payload)
    return create_engine(_mysql_url(db_config), pool_pre_ping=True, future=True)


def _ensure_disease_cursor_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS `{DISEASE_STRUCTURE_CURSOR_TABLE}` (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    source_table VARCHAR(128) NOT NULL,
                    target_table VARCHAR(128) NOT NULL,
                    last_source_id BIGINT NOT NULL DEFAULT 0,
                    last_job_id VARCHAR(64),
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_source_target (source_table, target_table)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        )


def _get_disease_cursor(engine, source_table: str, target_table: str) -> int:
    _ensure_disease_cursor_table(engine)
    with engine.begin() as conn:
        row = conn.execute(
            text(
                f"""
                SELECT last_source_id
                FROM `{DISEASE_STRUCTURE_CURSOR_TABLE}`
                WHERE source_table = :source_table
                  AND target_table = :target_table
                """
            ),
            {"source_table": source_table, "target_table": target_table},
        ).mappings().first()
    return int(row["last_source_id"]) if row else 0


def _update_disease_cursor(engine, source_table: str, target_table: str, last_source_id: int, job_id: str) -> None:
    _ensure_disease_cursor_table(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                INSERT INTO `{DISEASE_STRUCTURE_CURSOR_TABLE}` (
                    source_table,
                    target_table,
                    last_source_id,
                    last_job_id
                )
                VALUES (
                    :source_table,
                    :target_table,
                    :last_source_id,
                    :last_job_id
                )
                ON DUPLICATE KEY UPDATE
                    last_source_id = GREATEST(last_source_id, VALUES(last_source_id)),
                    last_job_id = VALUES(last_job_id),
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "source_table": source_table,
                "target_table": target_table,
                "last_source_id": int(last_source_id),
                "last_job_id": job_id,
            },
        )


def _active_disease_structure_job() -> tuple[str, dict] | None:
    for job_id, job in _disease_structure_jobs.items():
        if job["status"] in {"queued", "running"}:
            return job_id, job
    return None


def _serialize_disease_structure_job(job_id: str, job: dict) -> dict:
    return {
        "ok": job["status"] != "failed",
        "job_id": job_id,
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "requested_limit": job.get("requested_limit"),
        "accepted_limit": job.get("accepted_limit"),
        "chunk_size": job.get("chunk_size"),
        "batch_size": job.get("batch_size"),
        "max_errors": job.get("max_errors"),
        "source_table": job.get("source_table"),
        "target_table": job.get("target_table"),
        "start_id": job.get("start_id"),
        "current_id": job.get("current_id"),
        "processed_source_rows": job.get("processed_source_rows", 0),
        "processed_rows": job.get("processed_rows", 0),
        "event_rows": job.get("event_rows", 0),
        "error_count": job.get("error_count", 0),
        "result": job.get("result"),
        "error": job.get("error"),
    }


def _set_disease_job_fields(job_id: str, **fields) -> None:
    with _disease_structure_jobs_lock:
        if job_id in _disease_structure_jobs:
            _disease_structure_jobs[job_id].update(fields)


def _run_disease_structure_chunks(job_id: str, payload: dict) -> dict:
    source_table = payload["source_table"]
    target_table = payload["target_table"]
    accepted_limit = int(payload["accepted_limit"])
    chunk_size = int(payload["chunk_size"])
    max_errors = int(payload["max_errors"])
    update_cursor = bool(payload["update_cursor"])
    engine = _get_engine(payload.get("db"))
    try:
        next_min_id = payload.get("min_id")
        if next_min_id is None:
            last_cursor = _get_disease_cursor(engine, source_table, target_table)
            next_min_id = last_cursor + 1 if last_cursor > 0 else None

        totals = {
            "ok": True,
            "source_table": source_table,
            "target_table": target_table,
            "requested_limit": payload["requested_limit"],
            "accepted_limit": accepted_limit,
            "chunk_size": chunk_size,
            "batch_size": payload["batch_size"],
            "update_cursor": update_cursor,
            "processed_source_rows": 0,
            "processed_rows": 0,
            "event_rows": 0,
            "inserted_or_updated_rows": 0,
            "error_count": 0,
            "chunks": [],
            "errors": [],
        }

        while totals["processed_source_rows"] < accepted_limit:
            remaining = accepted_limit - totals["processed_source_rows"]
            chunk_payload = dict(payload)
            chunk_payload["limit"] = min(chunk_size, remaining)
            chunk_payload["min_id"] = next_min_id
            result = structure_consumer_disease_clues(chunk_payload)
            candidate_rows = int(result.get("candidate_rows") or 0)
            source_end_id = result.get("source_end_id")
            source_start_id = result.get("source_start_id")

            if source_start_id is not None and totals.get("start_id") is None:
                totals["start_id"] = int(source_start_id)
                _set_disease_job_fields(job_id, start_id=totals["start_id"])

            chunk_summary = {
                "candidate_rows": candidate_rows,
                "source_start_id": source_start_id,
                "source_end_id": source_end_id,
                "keyword_candidate_rows": int(result.get("keyword_candidate_rows") or 0),
                "processed_rows": int(result.get("processed_rows") or 0),
                "event_rows": int(result.get("event_rows") or 0),
                "error_count": int(result.get("error_count") or 0),
            }
            totals["chunks"].append(chunk_summary)

            if candidate_rows == 0 or source_end_id is None:
                totals["finished_reason"] = "no_more_data"
                break

            source_end_id = int(source_end_id)
            totals["processed_source_rows"] += candidate_rows
            totals["processed_rows"] += int(result.get("processed_rows") or 0)
            totals["event_rows"] += int(result.get("event_rows") or 0)
            totals["inserted_or_updated_rows"] += int(result.get("inserted_or_updated_rows") or 0)
            totals["error_count"] += int(result.get("error_count") or 0)
            totals["errors"].extend(result.get("errors") or [])
            totals["current_id"] = source_end_id

            if update_cursor:
                _update_disease_cursor(engine, source_table, target_table, source_end_id, job_id)

            _set_disease_job_fields(
                job_id,
                current_id=source_end_id,
                processed_source_rows=totals["processed_source_rows"],
                processed_rows=totals["processed_rows"],
                event_rows=totals["event_rows"],
                error_count=totals["error_count"],
            )

            if totals["error_count"] > max_errors:
                totals["ok"] = False
                totals["finished_reason"] = "max_errors_exceeded"
                break
            if payload.get("max_id") is not None and source_end_id >= int(payload["max_id"]):
                totals["finished_reason"] = "max_id_reached"
                break
            next_min_id = source_end_id + 1

        totals.setdefault("finished_reason", "accepted_limit_reached")
        return totals
    finally:
        engine.dispose()


def _run_disease_structure_job(job_id: str, payload: dict) -> None:
    _set_disease_job_fields(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        result = _run_disease_structure_chunks(job_id, payload)
        status = "done" if result.get("ok") and result.get("error_count", 0) == 0 else "done_with_errors"
        if not result.get("ok"):
            status = "failed"
        _set_disease_job_fields(
            job_id,
            status=status,
            result=result,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as exc:
        _set_disease_job_fields(
            job_id,
            status="failed",
            error=str(exc),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )


@consumer_api.route("/disease/structure", methods=["GET", "POST"])
def consumer_disease_structure():
    if request.method == "GET":
        job_id = request.args.get("id") or request.args.get("job_id")
        if not job_id:
            return jsonify(
                {
                    "ok": True,
                    "status": "idle",
                    "message": "Use POST /api/consumer/disease/structure to start a job, then GET with ?id=<job_id>.",
                }
            ), 200
        with _disease_structure_jobs_lock:
            job = _disease_structure_jobs.get(job_id)
            if not job:
                return jsonify({"ok": False, "error": f"unknown job_id: {job_id}"}), 404
            return jsonify(_serialize_disease_structure_job(job_id, job)), 200

    payload = _normalize_disease_structure_payload(_disease_structure_payload())
    if _as_bool(payload.pop("wait", None), default=False):
        try:
            sync_job_id = uuid4().hex
            result = _run_disease_structure_chunks(sync_job_id, payload)
            return jsonify(result), 200 if result.get("ok") else 500
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    job_id = uuid4().hex
    with _disease_structure_jobs_lock:
        active = _active_disease_structure_job()
        if active:
            active_job_id, active_job = active
            return jsonify(
                {
                    "ok": False,
                    "error": "another disease structure job is already queued or running",
                    "active_job": _serialize_disease_structure_job(active_job_id, active_job),
                }
            ), 409
        _disease_structure_jobs[job_id] = {
            "status": "queued",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "payload": payload,
            "requested_limit": payload["requested_limit"],
            "accepted_limit": payload["accepted_limit"],
            "chunk_size": payload["chunk_size"],
            "batch_size": payload["batch_size"],
            "max_errors": payload["max_errors"],
            "source_table": payload["source_table"],
            "target_table": payload["target_table"],
            "processed_source_rows": 0,
            "processed_rows": 0,
            "event_rows": 0,
            "error_count": 0,
        }
    _disease_structure_executor.submit(_run_disease_structure_job, job_id, payload)
    with _disease_structure_jobs_lock:
        return jsonify(_serialize_disease_structure_job(job_id, _disease_structure_jobs[job_id])), 202


@consumer_api.post("/materials/scores/calculate")
def consumer_material_scores_calculate():
    payload = request.get_json(silent=True) or {}
    try:
        result = calculate_material_scores(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@consumer_api.post("/risks/calculate")
def consumer_risks_calculate():
    payload = request.get_json(silent=True) or {}
    try:
        result = calculate_risk_scores(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
