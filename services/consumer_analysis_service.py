"""Consumer-side analysis service layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.adapters.comment_data_adapter import (
    DEFAULT_DOUYIN_ARCHIVE_DIR,
    DEFAULT_DOUYIN_DIR,
    DEFAULT_XHS_DIR,
    collect_comment_data,
)
from app.adapters.cat_disease import structure_cat_disease_clues
from app.adapters.feature_adapter import (
    run_consumer_feature_engineering,
    run_material_score_pipeline,
    run_risk_score_pipeline,
)


def engineer_consumer_features(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    return run_consumer_feature_engineering(
        db=payload.get("db"),
        steps=payload.get("steps"),
        protein_limit=int(payload.get("protein_limit") or 0),
        protein_concurrency=int(payload.get("protein_concurrency") or 4),
        timeout_seconds=payload.get("timeout_seconds"),
    )


def collect_consumer_comments(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    return collect_comment_data(
        db=payload.get("db"),
        steps=payload.get("steps"),
        douyin_dir=Path(payload["douyin_dir"]) if payload.get("douyin_dir") else DEFAULT_DOUYIN_DIR,
        xhs_dir=Path(payload["xhs_dir"]) if payload.get("xhs_dir") else DEFAULT_XHS_DIR,
        douyin_table=payload.get("douyin_table") or "douyin_raw_comments",
        xhs_table=payload.get("xhs_table") or "xiaohongshu_raw_comments",
        target_table=payload.get("target_table") or "catfood_brand_health_candidates",
        state_table=payload.get("state_table") or "catfood_brand_health_extract_state",
        xhs_pattern=payload.get("xhs_pattern") or "*.csv",
        xhs_source_name=payload.get("xhs_source_name") or "xiaohongshu",
        xhs_fallback_keyword=payload.get("xhs_fallback_keyword"),
        xhs_encoding=payload.get("xhs_encoding") or "utf-8-sig",
        batch_size=int(payload.get("batch_size") or 2000),
        douyin_archive_dir=(
            Path(payload["douyin_archive_dir"]) if payload.get("douyin_archive_dir") else DEFAULT_DOUYIN_ARCHIVE_DIR
        ),
        move_douyin_success_files=bool(payload.get("move_douyin_success_files", True)),
    )


def structure_consumer_disease_clues(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    return structure_cat_disease_clues(
        db=payload.get("db"),
        source_table=payload.get("source_table") or "catfood_brand_health_candidates",
        target_table=payload.get("target_table") or "cat_disease_clue_candidates",
        limit=payload.get("limit", 100),
        batch_size=int(payload.get("batch_size") or 20),
        min_id=payload.get("min_id"),
        max_id=payload.get("max_id"),
        skip_existing=bool(payload.get("skip_existing", True)),
        max_retries=int(payload.get("max_retries") or 3),
        max_comment_chars=int(payload.get("max_comment_chars") or 500),
    )


def calculate_material_scores(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    return run_material_score_pipeline(
        api_url=payload.get("api_url"),
        db=payload.get("db"),
        steps=payload.get("steps"),
        wait=bool(payload.get("wait", False)),
        timeout_seconds=int(payload.get("timeout_seconds") or 1800),
        poll_interval_seconds=float(payload.get("poll_interval_seconds") or 1.5),
    )


def calculate_risk_scores(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    return run_risk_score_pipeline(
        api_url=payload.get("api_url"),
        db=payload.get("db"),
        steps=payload.get("steps"),
        wait=bool(payload.get("wait", True)),
        refresh_sku_feature=bool(payload.get("refresh_sku_feature", True)),
        timeout_seconds=int(payload.get("timeout_seconds") or 300),
        poll_interval_seconds=float(payload.get("poll_interval_seconds") or 1.5),
    )
