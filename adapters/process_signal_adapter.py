"""Adapter for writing standardized brand process signals."""


import uuid
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

from vendor.csv_mysql_labeling.src.db import make_engine
from vendor.csv_mysql_labeling.src.settings import load_settings


DEFAULT_PROCESS_SIGNAL_TABLE = "catfood_brand_process_signal_standardized"
TABLE_RE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


TEXT_LIMITS = {
    "run_id": 64,
    "source_platform": 32,
    "external_id": 255,
    "source_keyword": 255,
    "source_comment_time": 64,
    "brand_name": 128,
    "brand_mentions": 255,
    "brand_confidence": 16,
    "process_level_1": 64,
    "process_level_2": 128,
    "process_evidence_text": 300,
    "matched_expression": 128,
    "signal_polarity": 16,
    "process_confidence": 16,
    "recommended_qc_items": 500,
    "review_status": 32,
    "model_name": 64,
    "comment_text": 800,
}


REQUIRED_TEXT_FIELDS = (
    "source_platform",
    "brand_confidence",
    "process_level_1",
    "process_level_2",
    "process_evidence_text",
    "matched_expression",
    "signal_polarity",
    "process_confidence",
    "recommended_qc_items",
    "review_status",
    "model_name",
    "comment_text",
)


def load_default_db_config() -> Dict[str, Any]:
    return dict(load_settings().mysql)


def _safe_table(name: str) -> str:
    value = str(name or "").strip()
    if not value or any(ch not in TABLE_RE for ch in value):
        raise ValueError(f"invalid table name: {name}")
    return value


def _clean_text(value: Any, field_name: str, default: str = "") -> str:
    text_value = str(value if value is not None else default).strip()
    max_len = TEXT_LIMITS.get(field_name)
    if max_len and len(text_value) > max_len:
        text_value = text_value[:max_len]
    return text_value


def _optional_text(value: Any, field_name: str) -> Optional[str]:
    text_value = _clean_text(value, field_name)
    return text_value or None


def ensure_process_signal_table(
    engine: Engine,
    table_name: str = DEFAULT_PROCESS_SIGNAL_TABLE,
) -> None:
    table_name = _safe_table(table_name)
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{table_name}` (
      id BIGINT PRIMARY KEY AUTO_INCREMENT,
      run_id VARCHAR(64) NOT NULL,
      source_candidate_id BIGINT NOT NULL,
      source_platform VARCHAR(32) NOT NULL,
      source_row_id BIGINT NULL,
      external_id VARCHAR(255) NULL,
      source_keyword VARCHAR(255) NULL,
      source_comment_time VARCHAR(64) NULL,
      brand_name VARCHAR(128) NULL,
      brand_mentions VARCHAR(255) NULL,
      brand_confidence VARCHAR(16) NOT NULL,
      process_level_1 VARCHAR(64) NOT NULL,
      process_level_2 VARCHAR(128) NOT NULL,
      process_evidence_text VARCHAR(300) NOT NULL,
      matched_expression VARCHAR(128) NOT NULL,
      signal_polarity VARCHAR(16) NOT NULL,
      process_confidence VARCHAR(16) NOT NULL,
      recommended_qc_items VARCHAR(500) NOT NULL,
      review_status VARCHAR(32) NOT NULL,
      model_name VARCHAR(64) NOT NULL,
      comment_text VARCHAR(800) NOT NULL,
      source_title TEXT NULL,
      created_at DATETIME NOT NULL,
      KEY idx_candidate (source_candidate_id),
      KEY idx_brand (brand_name),
      KEY idx_process_tag (process_level_1, process_level_2),
      KEY idx_review (review_status),
      KEY idx_source (source_platform, source_row_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


def _normalize_signal_row(row: Dict[str, Any], run_id: str, defaults: Dict[str, Any]) -> Dict[str, Any]:
    merged = {**defaults, **dict(row or {})}
    source_candidate_id = merged.get("source_candidate_id")
    if source_candidate_id is None or str(source_candidate_id).strip() == "":
        raise ValueError("source_candidate_id is required")

    normalized = {
        "run_id": _clean_text(merged.get("run_id") or run_id, "run_id"),
        "source_candidate_id": int(source_candidate_id),
        "source_platform": _clean_text(merged.get("source_platform"), "source_platform"),
        "source_row_id": int(merged["source_row_id"]) if merged.get("source_row_id") not in (None, "") else None,
        "external_id": _optional_text(merged.get("external_id"), "external_id"),
        "source_keyword": _optional_text(merged.get("source_keyword"), "source_keyword"),
        "source_comment_time": _optional_text(merged.get("source_comment_time"), "source_comment_time"),
        "brand_name": _optional_text(merged.get("brand_name"), "brand_name"),
        "brand_mentions": _optional_text(merged.get("brand_mentions"), "brand_mentions"),
        "brand_confidence": _clean_text(merged.get("brand_confidence") or "中", "brand_confidence"),
        "process_level_1": _clean_text(merged.get("process_level_1"), "process_level_1"),
        "process_level_2": _clean_text(merged.get("process_level_2"), "process_level_2"),
        "process_evidence_text": _clean_text(merged.get("process_evidence_text"), "process_evidence_text"),
        "matched_expression": _clean_text(merged.get("matched_expression"), "matched_expression"),
        "signal_polarity": _clean_text(merged.get("signal_polarity") or "负向", "signal_polarity"),
        "process_confidence": _clean_text(merged.get("process_confidence") or "中", "process_confidence"),
        "recommended_qc_items": _clean_text(merged.get("recommended_qc_items"), "recommended_qc_items"),
        "review_status": _clean_text(merged.get("review_status") or "待复核", "review_status"),
        "model_name": _clean_text(merged.get("model_name") or "external_api", "model_name"),
        "comment_text": _clean_text(merged.get("comment_text"), "comment_text"),
        "source_title": str(merged.get("source_title") or "").strip() or None,
    }

    missing = [field for field in REQUIRED_TEXT_FIELDS if not normalized[field]]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    return normalized


def insert_process_signals(
    *,
    db: Optional[Dict[str, Any]] = None,
    table_name: str = DEFAULT_PROCESS_SIGNAL_TABLE,
    rows: Sequence[Dict[str, Any]],
    run_id: Optional[str] = None,
    defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not rows:
        raise ValueError("rows cannot be empty")
    table_name = _safe_table(table_name)
    run_id = _clean_text(run_id or f"external_process_signal_{uuid.uuid4().hex[:12]}", "run_id")
    defaults = dict(defaults or {})
    payload: List[Dict[str, Any]] = [_normalize_signal_row(row, run_id, defaults) for row in rows]

    insert_sql = text(
        f"""
        INSERT INTO `{table_name}` (
          run_id, source_candidate_id, source_platform, source_row_id, external_id,
          source_keyword, source_comment_time, brand_name, brand_mentions, brand_confidence,
          process_level_1, process_level_2, process_evidence_text, matched_expression,
          signal_polarity, process_confidence, recommended_qc_items, review_status,
          model_name, comment_text, source_title, created_at
        )
        VALUES (
          :run_id, :source_candidate_id, :source_platform, :source_row_id, :external_id,
          :source_keyword, :source_comment_time, :brand_name, :brand_mentions, :brand_confidence,
          :process_level_1, :process_level_2, :process_evidence_text, :matched_expression,
          :signal_polarity, :process_confidence, :recommended_qc_items, :review_status,
          :model_name, :comment_text, :source_title, NOW()
        )
        """
    )

    engine = make_engine(db or load_default_db_config())
    try:
        ensure_process_signal_table(engine, table_name)
        with engine.begin() as conn:
            result = conn.execute(insert_sql, payload)
        return {
            "table_name": table_name,
            "run_id": run_id,
            "requested_rows": len(payload),
            "inserted_rows": int(result.rowcount or 0),
        }
    finally:
        engine.dispose()
