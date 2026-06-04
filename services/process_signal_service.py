"""Service layer for brand process signal pipelines."""


import argparse
import os
from typing import Any, Dict, List

from app_config import get_mysql_config, get_qwen_config
from adapters.process_signal_adapter import (
    DEFAULT_PROCESS_SIGNAL_TABLE,
    insert_process_signals,
)
from vendor.process_signal_pipeline import filter_catfood_process_signals as candidate_pipeline
from vendor.process_signal_pipeline import standardize_catfood_process_signals_qwen as standard_pipeline


DEFAULT_PROCESS_SIGNAL_CANDIDATE_TABLE = "catfood_process_signal_candidates"
DEFAULT_PROCESS_SIGNAL_STANDARD_TABLE = DEFAULT_PROCESS_SIGNAL_TABLE
DEFAULT_SOURCE_TABLES = ("xiaohongshu_raw_comments", "douyin_raw_comments")
DEFAULT_STEPS = ("extract_candidates", "standardize")


def write_process_signals(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    rows_value = payload.get("rows")
    if rows_value is None:
        row_value = payload.get("row")
        if row_value is None:
            row_value = {
                key: value
                for key, value in payload.items()
                if key not in {"db", "table_name", "run_id", "defaults"}
            }
        rows: List[Dict[str, Any]] = [dict(row_value or {})]
    else:
        rows = [dict(item or {}) for item in rows_value]

    result = insert_process_signals(
        db=payload.get("db"),
        table_name=payload.get("table_name") or DEFAULT_PROCESS_SIGNAL_TABLE,
        rows=rows,
        run_id=payload.get("run_id"),
        defaults=payload.get("defaults"),
    )
    return {"ok": True, **result}


def _default_db_config() -> Dict[str, Any]:
    return get_mysql_config()


def _apply_db_config(db_config: Dict[str, Any]) -> None:
    base_config = {
        "host": db_config["host"],
        "port": int(db_config.get("port", 3306)),
        "user": db_config["user"],
        "password": str(db_config.get("password", "")),
        "charset": db_config.get("charset", "utf8mb4"),
    }
    candidate_pipeline.DB_CONFIG = dict(base_config)
    standard_pipeline.DB_CONFIG = dict(base_config)


def _apply_qwen_config(payload: Dict[str, Any]) -> str:
    qwen_config = get_qwen_config(payload)
    api_key = qwen_config["api_key"]
    if api_key:
        os.environ["DASHSCOPE_API_KEY"] = api_key

    standard_pipeline.QWEN_BASE_URL = qwen_config["base_url"]
    return qwen_config["model"]


def _normalize_steps(value: Any) -> List[str]:
    if not value:
        return list(DEFAULT_STEPS)
    steps = [str(item).strip() for item in value if str(item).strip()]
    unknown = [item for item in steps if item not in DEFAULT_STEPS]
    if unknown:
        raise ValueError(f"unsupported steps: {', '.join(unknown)}")
    return steps


def run_process_signal_pipeline(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run candidate extraction and Qwen standardization for process signals."""
    payload = dict(payload or {})
    db_config = dict(payload.get("db") or _default_db_config())
    source_db = str(payload.get("source_db") or db_config.get("database") or "csv_labeling")
    output_db = str(payload.get("output_db") or source_db)
    candidate_table = str(payload.get("candidate_table") or DEFAULT_PROCESS_SIGNAL_CANDIDATE_TABLE)
    standard_table = str(payload.get("standard_table") or DEFAULT_PROCESS_SIGNAL_STANDARD_TABLE)
    steps = _normalize_steps(payload.get("steps"))
    dry_run = bool(payload.get("dry_run", False))

    _apply_db_config(db_config)
    model = _apply_qwen_config(payload)

    result: Dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "steps": steps,
        "tables": {
            "candidate_table": f"{output_db}.{candidate_table}",
            "standard_table": f"{output_db}.{standard_table}",
        },
        "results": {},
    }

    if dry_run:
        result["results"]["dry_run"] = {
            "source_db": source_db,
            "output_db": output_db,
            "model": model,
            "candidate_if_exists": payload.get("candidate_if_exists") or "replace",
            "standard_if_exists": payload.get("standard_if_exists") or "replace",
        }
        return result

    if "extract_candidates" in steps:
        source_tables = tuple(
            str(item).strip()
            for item in (payload.get("source_tables") or DEFAULT_SOURCE_TABLES)
            if str(item).strip()
        )
        candidate_args = argparse.Namespace(
            source_db=source_db,
            output_db=output_db,
            output_table=candidate_table,
            output_dir=str(payload.get("artifact_dir") or candidate_pipeline.ARTIFACT_ROOT),
            if_exists=str(payload.get("candidate_if_exists") or payload.get("if_exists") or "replace"),
            limit=int(payload.get("candidate_limit") or payload.get("limit") or 0),
            source_tables=source_tables,
        )
        candidate_summary = candidate_pipeline.main_from_args(candidate_args)
        result["results"]["extract_candidates"] = {
            "source_db": source_db,
            "output_table": f"{output_db}.{candidate_table}",
            "if_exists": candidate_args.if_exists,
            "limit": candidate_args.limit,
            "summary": candidate_summary,
        }

    if "standardize" in steps:
        standard_args = argparse.Namespace(
            source_db=output_db,
            source_table=candidate_table,
            output_db=output_db,
            output_table=standard_table,
            if_exists=str(payload.get("standard_if_exists") or payload.get("if_exists") or "replace"),
            model=model,
            temperature=float(payload.get("temperature", 0.0)),
            batch_size=int(payload.get("batch_size") or 5),
            limit=int(payload.get("standardize_limit") or payload.get("limit") or 0),
            where=str(payload.get("where") or ""),
            sleep=float(payload.get("sleep") or 0.2),
            max_comment_chars=int(payload.get("max_comment_chars") or 500),
            skip_multiline=bool(payload.get("skip_multiline", True)),
        )
        standard_pipeline.standardize(standard_args)
        result["results"]["standardize"] = {
            "source_table": f"{output_db}.{candidate_table}",
            "output_table": f"{output_db}.{standard_table}",
            "if_exists": standard_args.if_exists,
            "model": model,
            "limit": standard_args.limit,
        }

    return result
