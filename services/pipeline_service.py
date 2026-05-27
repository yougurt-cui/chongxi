"""Pipeline orchestration service layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.adapters.catfood_ingredient_adapter import (
    DEFAULT_GUARANTEE_HISTORY_DIR,
    DEFAULT_GUARANTEE_TABLE,
    DEFAULT_IMAGE_GLOB,
    DEFAULT_INGREDIENT_HISTORY_DIR,
    DEFAULT_OCR_TABLE,
    DEFAULT_PARSED_TABLE,
    DEFAULT_PRODUCT_INFO_TABLE,
    import_ingredient_images,
    load_default_db_config,
    parse_guarantee_values,
    parse_ingredient_ocr,
)


DEFAULT_STEPS = ["ocr_import", "parse_ocr_json", "parse_guarantee"]


def _normalize_steps(steps: Optional[Iterable[str]]) -> List[str]:
    if not steps:
        return list(DEFAULT_STEPS)
    normalized = [str(step).strip() for step in steps if str(step).strip()]
    allowed = set(DEFAULT_STEPS)
    unknown = [step for step in normalized if step not in allowed]
    if unknown:
        raise ValueError(f"unsupported steps: {', '.join(unknown)}")
    return normalized


def ingest_catfood_ingredients(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run catfood ingredient image OCR, structured parsing, and guarantee parsing."""
    payload = dict(payload or {})
    db_config = dict(payload.get("db") or load_default_db_config())
    steps = _normalize_steps(payload.get("steps"))

    image_dir_value = payload.get("image_dir")
    image_dir = Path(str(image_dir_value)).expanduser() if image_dir_value else None
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
        str(payload.get("ingredient_history_dir") or DEFAULT_INGREDIENT_HISTORY_DIR)
    ).expanduser()
    guarantee_history_dir = Path(
        str(payload.get("guarantee_history_dir") or DEFAULT_GUARANTEE_HISTORY_DIR)
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
        if image_dir is None:
            raise ValueError("image_dir is required when steps includes ocr_import")
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

    return result

