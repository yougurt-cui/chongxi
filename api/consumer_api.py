"""Consumer analysis API entrypoints."""


from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from hashlib import md5
from datetime import datetime
from datetime import date
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
from services.catfood_standardization_service import normalize_name
from vendor.feature_score_pipeline.scripts.brand_normalizer import canonicalize_brand, infer_brand_from_text


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
DISEASE_REVIEW_SOURCE_DB = "csv_labeling"
DISEASE_REVIEW_SOURCE_TABLE = "cat_disease_clue_candidates"
DISEASE_REVIEW_TARGET_TABLE = "cat_disease_clues"
_disease_structure_executor = ThreadPoolExecutor(max_workers=1)
_disease_structure_jobs = {}
_disease_structure_jobs_lock = Lock()


def _split_brand_parts(value) -> list[str]:
    return [
        part.strip()
        for part in str(value or "").replace("，", ",").replace("、", ",").split(",")
        if part.strip()
    ]


def _load_standard_brand_lookup(conn) -> dict[str, str]:
    """Return normalized brand/alias -> standard brand using the standard brand tables."""
    rows = conn.execute(
        text(
            """
            SELECT b.standard_brand_name, NULL AS alias_name
            FROM catfood_standard_brand b
            WHERE b.active = 1
            UNION ALL
            SELECT b.standard_brand_name, a.alias_name
            FROM catfood_standard_brand_alias a
            JOIN catfood_standard_brand b ON b.brand_id = a.brand_id
            WHERE a.active = 1 AND b.active = 1
            """
        )
    ).mappings().fetchall()
    lookup: dict[str, str] = {}
    for row in rows:
        standard = _clean_field(row.get("standard_brand_name"))
        if not standard:
            continue
        candidates = [standard, row.get("alias_name")]
        for candidate in candidates:
            key = normalize_name(candidate)
            if key:
                lookup.setdefault(key, standard)
            canonical_key = normalize_name(canonicalize_brand(candidate))
            if canonical_key:
                lookup.setdefault(canonical_key, standard)
    return lookup


def _match_standard_brand_name(value, lookup: dict[str, str]) -> str:
    """Map raw brand text to standard brand names, preserving multi-brand events."""
    if not lookup:
        return ""
    matched_parts: list[str] = []
    seen: set[str] = set()
    for part in _split_brand_parts(value) or [_clean_field(value)]:
        if not part:
            continue
        candidates = [part, canonicalize_brand(part)]
        standard = ""
        for candidate in candidates:
            key = normalize_name(candidate)
            if key in lookup:
                standard = lookup[key]
                break
            if len(key) >= 2:
                fuzzy_matches = {
                    brand
                    for alias_key, brand in lookup.items()
                    if len(alias_key) >= 2 and (key in alias_key or alias_key in key)
                }
                if len(fuzzy_matches) == 1:
                    standard = next(iter(fuzzy_matches))
                    break
        if standard and standard.lower() not in seen:
            seen.add(standard.lower())
            matched_parts.append(standard)
    return ",".join(matched_parts)


def _normalized_brand_filter_value(value) -> str:
    return ",".join(
        normalize_name(part)
        for part in _split_brand_parts(value)
        if normalize_name(part)
    )


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


def _get_csv_engine(db_payload: dict | None = None):
    db_config = app_config.get_mysql_config(database=DISEASE_REVIEW_SOURCE_DB, payload_db=db_payload)
    return create_engine(_mysql_url(db_config), pool_pre_ping=True, future=True)


def _get_feature_engine(db_payload: dict | None = None):
    db_config = app_config.get_feature_mysql_config(payload_db=db_payload)
    return create_engine(_mysql_url(db_config), pool_pre_ping=True, future=True)


def _json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def _clean_field(value, max_length: int | None = None) -> str:
    text_value = str(value or "").strip()
    if max_length is not None:
        return text_value[:max_length]
    return text_value


def _disease_case_id(values: dict) -> str:
    raw = "|".join(
        [
            _clean_field(values.get("comment_hash")),
            _clean_field(values.get("brand")),
            _clean_field(values.get("comment_text")),
            _clean_field(values.get("primary_symptom")),
            _clean_field(values.get("secondary_symptom")),
            _clean_field(values.get("direct")),
        ]
    )
    return md5(raw.encode("utf-8")).hexdigest()


def _standardize_disease_brand(
    candidate: dict,
    override: str | None = None,
    *,
    brand_lookup: dict[str, str] | None = None,
) -> str:
    brand_text = _clean_field(override if override is not None else candidate.get("brand_name"))
    if brand_text:
        standard_brand = _match_standard_brand_name(brand_text, brand_lookup or {})
        if standard_brand:
            return standard_brand
        parts = [part.strip() for part in brand_text.replace("，", ",").replace("、", ",").split(",") if part.strip()]
        canonical_parts = []
        seen = set()
        for part in parts or [brand_text]:
            canonical = canonicalize_brand(part)
            key = canonical.lower()
            if canonical and key not in seen:
                seen.add(key)
                canonical_parts.append(canonical)
        if canonical_parts:
            return ",".join(canonical_parts)
    inferred = infer_brand_from_text(
        candidate.get("search_keyword"),
        candidate.get("mentioned_brands"),
        candidate.get("review_text"),
    )
    standard_inferred = _match_standard_brand_name(inferred, brand_lookup or {})
    if standard_inferred:
        return standard_inferred
    return inferred or brand_text


def _candidate_to_clue_values(
    candidate: dict,
    payload: dict | None = None,
    *,
    brand_lookup: dict[str, str] | None = None,
) -> dict:
    payload = dict(payload or {})
    brand = _standardize_disease_brand(
        candidate,
        payload.get("brand") if "brand" in payload else None,
        brand_lookup=brand_lookup,
    )
    values = {
        "comment_hash": candidate.get("comment_hash"),
        "brand": _clean_field(brand, 32),
        "comment_text": _clean_field(payload.get("comment_text", candidate.get("review_text"))),
        "primary_symptom": _clean_field(payload.get("primary_symptom", candidate.get("symptom_category")), 64),
        "secondary_symptom": _clean_field(payload.get("secondary_symptom", candidate.get("symptom_name")), 64),
        "direct": _clean_field(payload.get("direct", candidate.get("effect_direction")), 16),
        "event_date_raw": _clean_field(payload.get("event_date_raw", candidate.get("review_date_raw")), 16) or None,
        "event_date": payload.get("event_date", candidate.get("review_date")),
    }
    if isinstance(values["event_date"], str):
        values["event_date"] = values["event_date"].strip() or None
    values["case_id"] = _disease_case_id(values)
    return values


def _serialize_disease_candidate(
    row: dict,
    target_case_ids: set[str] | None = None,
    *,
    brand_lookup: dict[str, str] | None = None,
) -> dict:
    item = {key: _json_value(value) for key, value in row.items()}
    clue_values = _candidate_to_clue_values(row, brand_lookup=brand_lookup)
    item["raw_brand_name"] = item.get("brand_name")
    item["brand_name"] = clue_values["brand"] or "其他"
    item["target_case_id"] = clue_values["case_id"]
    item["target_exists"] = clue_values["case_id"] in (target_case_ids or set())
    return item


def _apply_disease_review_filters(
    *,
    filters: list[str],
    params: dict,
    status: str,
    brand: str,
    symptom: str,
    min_confidence,
    max_confidence,
    brand_lookup: dict[str, str],
) -> None:
    if status != "ALL":
        filters.append("review_status = :status")
        params["status"] = status
    if symptom:
        filters.append("(symptom_category LIKE :symptom OR symptom_name LIKE :symptom)")
        params["symptom"] = f"%{symptom}%"
    if min_confidence not in (None, ""):
        filters.append("confidence >= :min_confidence")
        params["min_confidence"] = float(min_confidence)
    if max_confidence not in (None, ""):
        filters.append("confidence <= :max_confidence")
        params["max_confidence"] = float(max_confidence)
    if not brand:
        return

    if brand == "其他":
        filters.append(
            """
            (
              standard_brand_name IS NULL
              OR TRIM(standard_brand_name) = ''
              OR normalized_brand_name IS NULL
              OR TRIM(normalized_brand_name) = ''
            )
            """
        )
        return

    standard_brand = _match_standard_brand_name(brand, brand_lookup) or canonicalize_brand(brand)
    brand_key = _normalized_brand_filter_value(standard_brand)
    if brand_key:
        filters.append(
            """
            (
              normalized_brand_name = :brand_key
              OR normalized_brand_name LIKE :brand_key_prefix
              OR normalized_brand_name LIKE :brand_key_suffix
              OR normalized_brand_name LIKE :brand_key_contains
            )
            """
        )
        params.update(
            {
                "brand_key": brand_key,
                "brand_key_prefix": f"{brand_key},%",
                "brand_key_suffix": f"%,{brand_key}",
                "brand_key_contains": f"%,{brand_key},%",
            }
        )
    else:
        filters.append(
            """
            (
              standard_brand_name IS NULL
              OR TRIM(standard_brand_name) = ''
              OR normalized_brand_name IS NULL
              OR TRIM(normalized_brand_name) = ''
            )
            """
        )


def _ensure_disease_clues_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS `{DISEASE_REVIEW_TARGET_TABLE}` (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    case_id VARCHAR(32) NOT NULL,
                    brand VARCHAR(32) NOT NULL,
                    comment_text TEXT NOT NULL,
                    primary_symptom VARCHAR(64) NOT NULL,
                    secondary_symptom VARCHAR(64) NOT NULL,
                    direct VARCHAR(16) NOT NULL,
                    event_date_raw VARCHAR(16),
                    event_date DATE,
                    imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_case_id (case_id),
                    KEY idx_brand (brand),
                    KEY idx_primary_symptom (primary_symptom),
                    KEY idx_secondary_symptom (secondary_symptom),
                    KEY idx_direct (direct),
                    KEY idx_event_date (event_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
            )


def _ensure_disease_review_source_table(engine) -> None:
    with engine.begin() as conn:
        column_rows = conn.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                """
            ),
            {"table_name": DISEASE_REVIEW_SOURCE_TABLE},
        ).fetchall()
        columns = {row[0] for row in column_rows}
        if "standard_brand_name" not in columns:
            conn.execute(
                text(
                    f"""
                    ALTER TABLE `{DISEASE_REVIEW_SOURCE_TABLE}`
                    ADD COLUMN standard_brand_name VARCHAR(100) NULL AFTER brand_name
                    """
                )
            )
        if "normalized_brand_name" not in columns:
            conn.execute(
                text(
                    f"""
                    ALTER TABLE `{DISEASE_REVIEW_SOURCE_TABLE}`
                    ADD COLUMN normalized_brand_name VARCHAR(255) NULL AFTER standard_brand_name
                    """
                )
            )

        index_rows = conn.execute(text(f"SHOW INDEX FROM `{DISEASE_REVIEW_SOURCE_TABLE}`")).fetchall()
        index_names = {row[2] for row in index_rows}
        index_sql = {
            "idx_disease_review_status_id": (
                "review_status, id"
            ),
            "idx_disease_review_brand_status": (
                "normalized_brand_name, review_status, id"
            ),
            "idx_disease_review_symptom_status": (
                "review_status, symptom_category, symptom_name, id"
            ),
            "idx_disease_review_confidence_status": (
                "review_status, confidence, id"
            ),
        }
        for index_name, columns_sql in index_sql.items():
            if index_name not in index_names:
                conn.execute(
                    text(
                        f"""
                        ALTER TABLE `{DISEASE_REVIEW_SOURCE_TABLE}`
                        ADD KEY {index_name} ({columns_sql})
                        """
                    )
                )


def _backfill_disease_candidate_brands(
    engine,
    *,
    brand_lookup: dict[str, str],
    limit: int = 5000,
) -> int:
    if not brand_lookup:
        return 0
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT id, brand_name, search_keyword, mentioned_brands, review_text
                FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                WHERE standard_brand_name IS NULL
                   OR normalized_brand_name IS NULL
                   OR normalized_brand_name = ''
                ORDER BY id ASC
                LIMIT :limit
                """
            ),
            {"limit": max(1, min(int(limit or 5000), 20000))},
        ).mappings().fetchall()
        updates = []
        for row in rows:
            candidate = dict(row)
            raw_brand = _clean_field(candidate.get("brand_name"))
            standard_brand = _match_standard_brand_name(raw_brand, brand_lookup)
            if not standard_brand:
                inferred_brand = infer_brand_from_text(
                    candidate.get("search_keyword"),
                    candidate.get("mentioned_brands"),
                    candidate.get("review_text"),
                )
                standard_brand = _match_standard_brand_name(inferred_brand, brand_lookup)
            normalized_brand = _normalized_brand_filter_value(standard_brand)
            if standard_brand or normalized_brand:
                updates.append(
                    {
                        "id": row["id"],
                        "standard_brand_name": standard_brand or None,
                        "normalized_brand_name": normalized_brand or None,
                    }
                )
        if not updates:
            return 0
        conn.execute(
            text(
                f"""
                UPDATE `{DISEASE_REVIEW_SOURCE_TABLE}`
                SET standard_brand_name = :standard_brand_name,
                    normalized_brand_name = :normalized_brand_name
                WHERE id = :id
                """
            ),
            updates,
        )
        return len(updates)


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


@consumer_api.get("/disease/reviews")
def consumer_disease_reviews():
    status = (request.args.get("status") or "PENDING").strip().upper()
    if status not in {"PENDING", "APPROVED", "REJECTED", "ALL"}:
        return jsonify({"ok": False, "error": "unsupported status"}), 400
    limit = _clamp_int(request.args.get("limit"), 100, 1, 500)
    offset = _clamp_int(request.args.get("offset"), 0, 0, 1_000_000_000)
    brand = _clean_field(request.args.get("brand"))
    symptom = _clean_field(request.args.get("symptom"))
    min_confidence = request.args.get("min_confidence")
    max_confidence = request.args.get("max_confidence")
    source_engine = _get_csv_engine()
    target_engine = _get_feature_engine()
    try:
        _ensure_disease_review_source_table(source_engine)
        _ensure_disease_clues_table(target_engine)
        filters = []
        params = {"limit": limit, "offset": offset}
        with source_engine.connect() as conn:
            brand_lookup = _load_standard_brand_lookup(conn)
        _backfill_disease_candidate_brands(source_engine, brand_lookup=brand_lookup)
        _apply_disease_review_filters(
            filters=filters,
            params=params,
            status=status,
            brand=brand,
            symptom=symptom,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            brand_lookup=brand_lookup,
        )
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        with source_engine.connect() as conn:
            total = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                    {where_sql}
                    """
                ),
                params,
            ).scalar()
            rows = conn.execute(
                text(
                    f"""
                    SELECT *
                    FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                    {where_sql}
                    ORDER BY id ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).mappings().fetchall()
            status_rows = conn.execute(
                text(
                    f"""
                    SELECT review_status, COUNT(*) AS cnt
                    FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                    GROUP BY review_status
                    """
                )
            ).mappings().fetchall()

        target_case_ids = {
            _candidate_to_clue_values(dict(row), brand_lookup=brand_lookup)["case_id"]
            for row in rows
        }
        existing_case_ids: set[str] = set()
        if target_case_ids:
            placeholders = ", ".join(f":case_id_{idx}" for idx, _ in enumerate(target_case_ids))
            case_params = {f"case_id_{idx}": case_id for idx, case_id in enumerate(target_case_ids)}
            with target_engine.connect() as conn:
                existing_case_ids = {
                    item[0]
                    for item in conn.execute(
                        text(
                            f"""
                            SELECT case_id
                            FROM `{DISEASE_REVIEW_TARGET_TABLE}`
                            WHERE case_id IN ({placeholders})
                            """
                        ),
                        case_params,
                    ).fetchall()
                }

        return jsonify(
            {
                "ok": True,
                "items": [
                    _serialize_disease_candidate(
                        dict(row),
                        existing_case_ids,
                        brand_lookup=brand_lookup,
                    )
                    for row in rows
                ],
                "status_counts": {row["review_status"]: int(row["cnt"]) for row in status_rows},
                "total": int(total or 0),
                "limit": limit,
                "offset": offset,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        source_engine.dispose()
        target_engine.dispose()


@consumer_api.get("/disease/reviews/brand-stats")
def consumer_disease_review_brand_stats():
    status = (request.args.get("status") or "PENDING").strip().upper()
    if status not in {"PENDING", "APPROVED", "REJECTED", "ALL"}:
        return jsonify({"ok": False, "error": "unsupported status"}), 400
    brand = _clean_field(request.args.get("brand"))
    symptom = _clean_field(request.args.get("symptom"))
    min_confidence = request.args.get("min_confidence")
    max_confidence = request.args.get("max_confidence")
    limit = _clamp_int(request.args.get("limit"), 500, 1, 2000)
    source_engine = _get_csv_engine()
    try:
        _ensure_disease_review_source_table(source_engine)
        filters = []
        params = {"limit": limit}
        with source_engine.connect() as conn:
            brand_lookup = _load_standard_brand_lookup(conn)
        _backfill_disease_candidate_brands(source_engine, brand_lookup=brand_lookup)
        _apply_disease_review_filters(
            filters=filters,
            params=params,
            status=status,
            brand=brand,
            symptom=symptom,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            brand_lookup=brand_lookup,
        )
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        with source_engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                      COALESCE(NULLIF(TRIM(standard_brand_name), ''), '其他') AS brand_name,
                      COUNT(*) AS cnt
                    FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                    {where_sql}
                    GROUP BY COALESCE(NULLIF(TRIM(standard_brand_name), ''), '其他')
                    ORDER BY cnt DESC, COALESCE(NULLIF(TRIM(standard_brand_name), ''), '其他') ASC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().fetchall()
            total = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                    {where_sql}
                    """
                ),
                params,
            ).scalar()
        return jsonify(
            {
                "ok": True,
                "items": [
                    {
                        "brand_name": str(row["brand_name"] or "其他"),
                        "count": int(row["cnt"] or 0),
                    }
                    for row in rows
                ],
                "total": int(total or 0),
                "status": status,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        source_engine.dispose()


@consumer_api.get("/disease/reviews/options")
def consumer_disease_review_options():
    source_engine = _get_csv_engine()
    try:
        _ensure_disease_review_source_table(source_engine)
        with source_engine.connect() as conn:
            brand_lookup = _load_standard_brand_lookup(conn)
        _backfill_disease_candidate_brands(source_engine, brand_lookup=brand_lookup)
        with source_engine.connect() as conn:
            standard_brands = [
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT standard_brand_name
                        FROM catfood_standard_brand
                        WHERE active = 1
                          AND standard_brand_name IS NOT NULL
                          AND TRIM(standard_brand_name) <> ''
                        ORDER BY standard_brand_name ASC
                        LIMIT 500
                        """
                    )
                ).fetchall()
            ]
            brands = sorted(
                {
                    part.strip()
                    for value in standard_brands
                    for part in _split_brand_parts(value)
                    if part.strip()
                }
            )
            brands.append("其他")
            categories = [
                row[0]
                for row in conn.execute(
                    text(
                        f"""
                        SELECT DISTINCT symptom_category
                        FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                        WHERE symptom_category IS NOT NULL AND TRIM(symptom_category) <> ''
                        ORDER BY symptom_category ASC
                        LIMIT 200
                        """
                    )
                ).fetchall()
            ]
            symptoms = [
                row[0]
                for row in conn.execute(
                    text(
                        f"""
                        SELECT DISTINCT symptom_name
                        FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                        WHERE symptom_name IS NOT NULL AND TRIM(symptom_name) <> ''
                        ORDER BY symptom_name ASC
                        LIMIT 500
                        """
                    )
                ).fetchall()
            ]
            pairs = conn.execute(
                text(
                    f"""
                    SELECT DISTINCT symptom_category, symptom_name
                    FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                    WHERE symptom_category IS NOT NULL
                      AND TRIM(symptom_category) <> ''
                      AND symptom_name IS NOT NULL
                      AND TRIM(symptom_name) <> ''
                    ORDER BY symptom_category ASC, symptom_name ASC
                    """
                )
            ).fetchall()
        symptom_map: dict[str, list[str]] = {}
        for category, symptom_name in pairs:
            symptom_map.setdefault(str(category), [])
            if symptom_name not in symptom_map[str(category)]:
                symptom_map[str(category)].append(str(symptom_name))
        return jsonify(
            {
                "ok": True,
                "brands": brands,
                "categories": categories,
                "symptoms": symptoms,
                "symptom_map": symptom_map,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        source_engine.dispose()


@consumer_api.post("/disease/reviews/<int:candidate_id>/approve")
def consumer_disease_review_approve(candidate_id: int):
    payload = request.get_json(silent=True) or {}
    source_engine = _get_csv_engine(payload.get("db"))
    target_engine = _get_feature_engine(payload.get("db"))
    try:
        _ensure_disease_review_source_table(source_engine)
        _ensure_disease_clues_table(target_engine)
        with source_engine.connect() as conn:
            brand_lookup = _load_standard_brand_lookup(conn)
        with source_engine.connect() as conn:
            candidate = conn.execute(
                text(
                    f"""
                    SELECT *
                    FROM `{DISEASE_REVIEW_SOURCE_TABLE}`
                    WHERE id = :candidate_id
                    """
                ),
                {"candidate_id": candidate_id},
            ).mappings().first()
        if not candidate:
            return jsonify({"ok": False, "error": f"candidate not found: {candidate_id}"}), 404

        candidate_dict = dict(candidate)
        clue_values = _candidate_to_clue_values(
            candidate_dict,
            payload,
            brand_lookup=brand_lookup,
        )
        required_fields = ["brand", "comment_text", "primary_symptom", "secondary_symptom", "direct"]
        missing = [field for field in required_fields if not clue_values.get(field)]
        if missing:
            return jsonify({"ok": False, "error": f"missing required fields: {', '.join(missing)}"}), 400

        with target_engine.begin() as conn:
            result = conn.execute(
                text(
                    f"""
                    INSERT INTO `{DISEASE_REVIEW_TARGET_TABLE}` (
                        case_id,
                        brand,
                        comment_text,
                        primary_symptom,
                        secondary_symptom,
                        direct,
                        event_date_raw,
                        event_date
                    )
                    VALUES (
                        :case_id,
                        :brand,
                        :comment_text,
                        :primary_symptom,
                        :secondary_symptom,
                        :direct,
                        :event_date_raw,
                        :event_date
                    )
                    ON DUPLICATE KEY UPDATE
                        brand = VALUES(brand),
                        comment_text = VALUES(comment_text),
                        primary_symptom = VALUES(primary_symptom),
                        secondary_symptom = VALUES(secondary_symptom),
                        direct = VALUES(direct),
                        event_date_raw = VALUES(event_date_raw),
                        event_date = VALUES(event_date)
                    """
                ),
                clue_values,
            )

        reviewer_note = _clean_field(payload.get("reviewer_note") or payload.get("note"))
        reviewer = _clean_field(payload.get("reviewer"))
        note_parts = [part for part in [reviewer, reviewer_note] if part]
        note = "｜".join(note_parts) or "pipeline-review-page approved"
        with source_engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    UPDATE `{DISEASE_REVIEW_SOURCE_TABLE}`
                    SET review_status = 'APPROVED',
                        standard_brand_name = :standard_brand_name,
                        normalized_brand_name = :normalized_brand_name,
                        reviewer_note = :reviewer_note,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :candidate_id
                    """
                ),
                {
                    "candidate_id": candidate_id,
                    "standard_brand_name": clue_values["brand"] or None,
                    "normalized_brand_name": _normalized_brand_filter_value(clue_values["brand"]) or None,
                    "reviewer_note": note,
                },
            )

        return jsonify(
            {
                "ok": True,
                "candidate_id": candidate_id,
                "case_id": clue_values["case_id"],
                "target_table": f"protein_feature_platform.{DISEASE_REVIEW_TARGET_TABLE}",
                "affected_rows": int(result.rowcount or 0),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        source_engine.dispose()
        target_engine.dispose()


@consumer_api.post("/disease/reviews/<int:candidate_id>/reject")
def consumer_disease_review_reject(candidate_id: int):
    payload = request.get_json(silent=True) or {}
    note = _clean_field(payload.get("reviewer_note") or payload.get("note") or "pipeline-review-page rejected")
    source_engine = _get_csv_engine(payload.get("db"))
    try:
        with source_engine.begin() as conn:
            result = conn.execute(
                text(
                    f"""
                    UPDATE `{DISEASE_REVIEW_SOURCE_TABLE}`
                    SET review_status = 'REJECTED',
                        reviewer_note = :reviewer_note,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :candidate_id
                    """
                ),
                {"candidate_id": candidate_id, "reviewer_note": note},
            )
        if not result.rowcount:
            return jsonify({"ok": False, "error": f"candidate not found: {candidate_id}"}), 404
        return jsonify({"ok": True, "candidate_id": candidate_id, "status": "REJECTED"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        source_engine.dispose()


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
