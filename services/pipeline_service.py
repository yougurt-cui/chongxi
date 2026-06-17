"""Pipeline orchestration service layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from app.adapters.catfood_ingredient_adapter import (
        DEFAULT_GUARANTEE_HISTORY_DIR,
        DEFAULT_GUARANTEE_TABLE,
        DEFAULT_IMAGE_DIR,
        DEFAULT_IMAGE_GLOB,
        DEFAULT_INGREDIENT_HISTORY_DIR,
        DEFAULT_OCR_TABLE,
        DEFAULT_PARSED_TABLE,
        DEFAULT_PRODUCT_INFO_TABLE,
        import_ingredient_images,
        fetch_ocr_context_by_sha256,
        load_default_db_config,
        parse_guarantee_values,
        parse_ingredient_ocr,
    )
    from app.vendor.csv_mysql_labeling.src.settings import load_settings
except ModuleNotFoundError:
    from adapters.catfood_ingredient_adapter import (
        DEFAULT_GUARANTEE_HISTORY_DIR,
        DEFAULT_GUARANTEE_TABLE,
        DEFAULT_IMAGE_DIR,
        DEFAULT_IMAGE_GLOB,
        DEFAULT_INGREDIENT_HISTORY_DIR,
        DEFAULT_OCR_TABLE,
        DEFAULT_PARSED_TABLE,
        DEFAULT_PRODUCT_INFO_TABLE,
        import_ingredient_images,
        fetch_ocr_context_by_sha256,
        load_default_db_config,
        parse_guarantee_values,
        parse_ingredient_ocr,
    )
    from vendor.csv_mysql_labeling.src.settings import load_settings


DEFAULT_STEPS = ["ocr_import", "parse_ocr_json", "parse_guarantee"]


def _configured_pipeline_paths() -> Dict[str, Any]:
    return dict((load_settings().pipeline or {}).get("paths") or {})


def _normalize_steps(steps: Optional[Iterable[str]]) -> List[str]:
    if not steps:
        return list(DEFAULT_STEPS)
    normalized = [str(step).strip() for step in steps if str(step).strip()]
    allowed = set(DEFAULT_STEPS)
    unknown = [step for step in normalized if step not in allowed]
    if unknown:
        raise ValueError(f"unsupported steps: {', '.join(unknown)}")
    return normalized


def _first_success_sha(result: Dict[str, Any]) -> Optional[str]:
    ocr_result = (result.get("results") or {}).get("ocr_import") or {}
    samples = ocr_result.get("success_samples") or []
    if not isinstance(samples, list):
        return None
    for sample in samples:
        if isinstance(sample, dict) and sample.get("file_sha256"):
            return str(sample["file_sha256"])
    return None


def _attach_single_image_ocr_context(
    result: Dict[str, Any],
    *,
    payload: Dict[str, Any],
    db_config: Dict[str, Any],
    ocr_table: str,
    parsed_table: str,
) -> None:
    file_sha256 = (
        payload.get("file_sha256")
        or payload.get("sha256")
        or _first_success_sha(result)
    )
    if not file_sha256:
        return

    context = fetch_ocr_context_by_sha256(
        db_config=db_config,
        file_sha256=str(file_sha256),
        ocr_table=ocr_table,
        parsed_table=parsed_table,
    )
    if not context:
        return

    result["source_id"] = context.get("source_id")
    result["ocr_id"] = context.get("id")
    result["parsed_row_id"] = context.get("parsed_row_id")
    result["image_name"] = context.get("image_name")
    result["file_sha256"] = context.get("file_sha256")
    result["ocr_text"] = context.get("ocr_text")
    result["ingredient_composition"] = context.get("ingredient_composition")
    result["brand"] = context.get("brand")
    result["product_name"] = context.get("product_name")
    if context.get("parsed"):
        result["parsed"] = context["parsed"]


def ingest_catfood_ingredients(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run catfood ingredient image OCR, structured parsing, and guarantee parsing."""
    payload = dict(payload or {})
    configured_paths = _configured_pipeline_paths()
    db_config = dict(payload.get("db") or load_default_db_config())
    steps = _normalize_steps(payload.get("steps"))

    image_dir_value = payload.get("image_dir")
    image_dir = (
        Path(str(image_dir_value)).expanduser()
        if image_dir_value
        else Path(str(configured_paths["image_dir"])).expanduser()
        if configured_paths.get("image_dir")
        else DEFAULT_IMAGE_DIR
    )
    image_glob = str(payload.get("image_glob") or DEFAULT_IMAGE_GLOB)
    ocr_table = str(payload.get("ocr_table") or DEFAULT_OCR_TABLE)
    parsed_table = str(payload.get("parsed_table") or DEFAULT_PARSED_TABLE)
    product_info_table = str(payload.get("product_info_table") or DEFAULT_PRODUCT_INFO_TABLE)
    guarantee_table = str(payload.get("guarantee_table") or DEFAULT_GUARANTEE_TABLE)
    parse_limit = int(payload.get("parse_limit") or 500)
    guarantee_limit = int(payload.get("guarantee_limit") or 200)
    incremental_only = bool(payload.get("incremental_only", True))
    move_success_images = bool(payload.get("move_success_images", True))
    sleep_seconds = float(payload.get("sleep_seconds", 1.5))

    ingredient_history_dir = Path(
        str(
            payload.get("ingredient_history_dir")
            or configured_paths.get("ingredient_history_dir")
            or DEFAULT_INGREDIENT_HISTORY_DIR
        )
    ).expanduser()
    guarantee_history_dir = Path(
        str(
            payload.get("guarantee_history_dir")
            or configured_paths.get("guarantee_history_dir")
            or DEFAULT_GUARANTEE_HISTORY_DIR
        )
    ).expanduser()

    result: Dict[str, Any] = {
        "ok": True,
        "steps": steps,
        "tables": {
            "ocr_table": ocr_table,
            "parsed_table": parsed_table,
            "product_info_table": product_info_table,
            "guarantee_table": guarantee_table,
        },
        "results": {},
    }

    if "ocr_import" in steps:
        result["results"]["ocr_import"] = import_ingredient_images(
            image_dir=image_dir,
            db_config=db_config,
            image_glob=image_glob,
            table_name=ocr_table,
            history_dir=ingredient_history_dir,
            move_success_images=move_success_images,
            sleep_seconds=sleep_seconds,
        )

    if "parse_ocr_json" in steps:
        result["results"]["parse_ocr_json"] = parse_ingredient_ocr(
            db_config=db_config,
            source_table=ocr_table,
            target_table=parsed_table,
            limit=parse_limit,
            incremental_only=incremental_only,
        )

    if "parse_guarantee" in steps:
        result["results"]["parse_guarantee"] = parse_guarantee_values(
            db_config=db_config,
            source_table=ocr_table,
            parsed_table=parsed_table,
            info_table=product_info_table,
            guarantee_table=guarantee_table,
            limit=guarantee_limit,
            incremental_only=incremental_only,
            processed_dir=guarantee_history_dir,
        )

    _attach_single_image_ocr_context(
        result,
        payload=payload,
        db_config=db_config,
        ocr_table=ocr_table,
        parsed_table=parsed_table,
    )

    return result
