"""Exception queue adapter for manual review workflows."""


import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from vendor.csv_mysql_labeling.src.db import make_engine
from vendor.csv_mysql_labeling.src.settings import load_settings


DEFAULT_EXCEPTION_QUEUE_TABLE = "pipeline_exception_queue"
BLOCKING_STATUSES = {"pending_review", "in_review"}
READY_STATUSES = {"fixed", "released", "ignored"}
ALL_STATUSES = BLOCKING_STATUSES | READY_STATUSES


def load_default_db_config() -> Dict[str, Any]:
    return dict(load_settings().mysql)


def _safe_table(name: str) -> str:
    text_value = str(name or "").strip()
    if not text_value or not all(ch.isalnum() or ch == "_" for ch in text_value):
        raise ValueError(f"invalid table name: {name}")
    return text_value


def _json_dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalize_status(value: Any, *, default: str = "pending_review") -> str:
    status = str(value or default).strip()
    if status not in ALL_STATUSES:
        raise ValueError(f"unsupported exception status: {status}")
    return status


def _queue_key(
    *,
    data_scope: str,
    source_table: str,
    source_id: Any,
    business_key: Optional[str],
    error_code: Optional[str],
) -> str:
    identity = "|".join(
        [
            str(data_scope or "").strip(),
            str(source_table or "").strip(),
            str(source_id or "").strip(),
            str(business_key or "").strip(),
            str(error_code or "").strip(),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def ensure_exception_queue_table(
    engine: Engine,
    table_name: str = DEFAULT_EXCEPTION_QUEUE_TABLE,
) -> None:
    table_name = _safe_table(table_name)
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{table_name}` (
      id BIGINT PRIMARY KEY AUTO_INCREMENT,
      queue_key CHAR(64) NOT NULL,
      data_scope VARCHAR(100) NOT NULL,
      source_table VARCHAR(128) NULL,
      source_id VARCHAR(128) NULL,
      business_key VARCHAR(255) NULL,
      error_code VARCHAR(100) NULL,
      error_message TEXT NULL,
      payload_json LONGTEXT NULL,
      status VARCHAR(32) NOT NULL DEFAULT 'pending_review',
      reviewer VARCHAR(100) NULL,
      review_note TEXT NULL,
      fix_note TEXT NULL,
      blocked_reason TEXT NULL,
      first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      reviewed_at DATETIME NULL,
      fixed_at DATETIME NULL,
      released_at DATETIME NULL,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY uq_queue_key (queue_key),
      KEY idx_status (status),
      KEY idx_data_ref (data_scope, source_table, source_id),
      KEY idx_business_key (business_key),
      KEY idx_last_seen_at (last_seen_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


def record_exception(
    *,
    db: Optional[Dict[str, Any]] = None,
    table_name: str = DEFAULT_EXCEPTION_QUEUE_TABLE,
    data_scope: str,
    source_table: Optional[str] = None,
    source_id: Optional[Any] = None,
    business_key: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    payload: Any = None,
    blocked_reason: Optional[str] = None,
) -> Dict[str, Any]:
    data_scope = str(data_scope or "").strip()
    if not data_scope:
        raise ValueError("data_scope is required")
    key = _queue_key(
        data_scope=data_scope,
        source_table=str(source_table or ""),
        source_id=source_id,
        business_key=business_key,
        error_code=error_code,
    )
    table_name = _safe_table(table_name)
    engine = make_engine(db or load_default_db_config())
    try:
        ensure_exception_queue_table(engine, table_name)
        payload_json = _json_dumps(payload)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO `{table_name}`(
                      queue_key, data_scope, source_table, source_id, business_key,
                      error_code, error_message, payload_json, status, blocked_reason,
                      first_seen_at, last_seen_at
                    )
                    VALUES(
                      :queue_key, :data_scope, :source_table, :source_id, :business_key,
                      :error_code, :error_message, :payload_json, 'pending_review', :blocked_reason,
                      NOW(), NOW()
                    )
                    ON DUPLICATE KEY UPDATE
                      error_message = VALUES(error_message),
                      payload_json = VALUES(payload_json),
                      blocked_reason = VALUES(blocked_reason),
                      status = CASE
                        WHEN status IN ('fixed', 'released', 'ignored') THEN 'pending_review'
                        ELSE status
                      END,
                      last_seen_at = NOW()
                    """
                ),
                {
                    "queue_key": key,
                    "data_scope": data_scope,
                    "source_table": str(source_table or "") or None,
                    "source_id": str(source_id or "") or None,
                    "business_key": str(business_key or "") or None,
                    "error_code": str(error_code or "") or None,
                    "error_message": str(error_message or "") or None,
                    "payload_json": payload_json,
                    "blocked_reason": str(blocked_reason or "") or None,
                },
            )
            row = conn.execute(
                text(f"SELECT * FROM `{table_name}` WHERE queue_key = :queue_key"),
                {"queue_key": key},
            ).mappings().first()
        return _row_to_dict(row)
    finally:
        engine.dispose()


def list_exceptions(
    *,
    db: Optional[Dict[str, Any]] = None,
    table_name: str = DEFAULT_EXCEPTION_QUEUE_TABLE,
    status: Optional[str] = None,
    data_scope: Optional[str] = None,
    source_table: Optional[str] = None,
    source_id: Optional[Any] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    table_name = _safe_table(table_name)
    engine = make_engine(db or load_default_db_config())
    try:
        ensure_exception_queue_table(engine, table_name)
        filters: List[str] = []
        params: Dict[str, Any] = {
            "limit": max(1, min(int(limit or 100), 500)),
            "offset": max(0, int(offset or 0)),
        }
        if status:
            filters.append("status = :status")
            params["status"] = _normalize_status(status)
        if data_scope:
            filters.append("data_scope = :data_scope")
            params["data_scope"] = str(data_scope)
        if source_table:
            filters.append("source_table = :source_table")
            params["source_table"] = str(source_table)
        if source_id is not None:
            filters.append("source_id = :source_id")
            params["source_id"] = str(source_id)
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        with engine.connect() as conn:
            total = int(
                conn.execute(
                    text(f"SELECT COUNT(*) FROM `{table_name}` {where_sql}"),
                    params,
                ).scalar()
                or 0
            )
            rows = conn.execute(
                text(
                    f"""
                    SELECT *
                    FROM `{table_name}`
                    {where_sql}
                    ORDER BY
                      CASE status
                        WHEN 'pending_review' THEN 1
                        WHEN 'in_review' THEN 2
                        ELSE 3
                      END,
                      last_seen_at DESC,
                      id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).mappings().all()
        return {"total": total, "items": [_row_to_dict(row) for row in rows]}
    finally:
        engine.dispose()


def update_exception_status(
    *,
    db: Optional[Dict[str, Any]] = None,
    table_name: str = DEFAULT_EXCEPTION_QUEUE_TABLE,
    exception_id: int,
    status: str,
    reviewer: Optional[str] = None,
    review_note: Optional[str] = None,
    fix_note: Optional[str] = None,
) -> Dict[str, Any]:
    status = _normalize_status(status)
    table_name = _safe_table(table_name)
    engine = make_engine(db or load_default_db_config())
    try:
        ensure_exception_queue_table(engine, table_name)
        timestamp_sql = {
            "in_review": "reviewed_at = COALESCE(reviewed_at, NOW()),",
            "fixed": "fixed_at = NOW(),",
            "released": "released_at = NOW(),",
            "ignored": "released_at = NOW(),",
            "pending_review": "",
        }[status]
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    f"""
                    UPDATE `{table_name}`
                    SET status = :status,
                        reviewer = COALESCE(:reviewer, reviewer),
                        review_note = COALESCE(:review_note, review_note),
                        fix_note = COALESCE(:fix_note, fix_note),
                        {timestamp_sql}
                        updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                {
                    "id": int(exception_id),
                    "status": status,
                    "reviewer": str(reviewer or "") or None,
                    "review_note": str(review_note or "") or None,
                    "fix_note": str(fix_note or "") or None,
                },
            )
            if result.rowcount == 0:
                raise KeyError(f"exception not found: {exception_id}")
            row = conn.execute(
                text(f"SELECT * FROM `{table_name}` WHERE id = :id"),
                {"id": int(exception_id)},
            ).mappings().first()
        return _row_to_dict(row)
    finally:
        engine.dispose()


def check_exception_gate(
    *,
    db: Optional[Dict[str, Any]] = None,
    table_name: str = DEFAULT_EXCEPTION_QUEUE_TABLE,
    data_scope: str,
    source_table: Optional[str] = None,
    source_id: Optional[Any] = None,
    business_key: Optional[str] = None,
) -> Dict[str, Any]:
    table_name = _safe_table(table_name)
    engine = make_engine(db or load_default_db_config())
    try:
        ensure_exception_queue_table(engine, table_name)
        filters = ["data_scope = :data_scope", "status IN ('pending_review', 'in_review')"]
        params: Dict[str, Any] = {"data_scope": str(data_scope or "")}
        if source_table:
            filters.append("source_table = :source_table")
            params["source_table"] = str(source_table)
        if source_id is not None:
            filters.append("source_id = :source_id")
            params["source_id"] = str(source_id)
        if business_key:
            filters.append("business_key = :business_key")
            params["business_key"] = str(business_key)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT *
                    FROM `{table_name}`
                    WHERE {' AND '.join(filters)}
                    ORDER BY last_seen_at DESC, id DESC
                    """
                ),
                params,
            ).mappings().all()
        return {
            "can_run": len(rows) == 0,
            "blocking_count": len(rows),
            "blocking_items": [_row_to_dict(row) for row in rows],
        }
    finally:
        engine.dispose()


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    out = dict(row)
    payload_json = out.get("payload_json")
    if payload_json:
        try:
            out["payload"] = json.loads(payload_json)
        except Exception:
            out["payload"] = None
    return out
