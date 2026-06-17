"""Adapter around the legacy catfood ingredient OCR pipeline."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text


try:
    from app.vendor.csv_mysql_labeling.src.db import make_engine
    from app.vendor.csv_mysql_labeling.src.ocr_image import run_ocr_image, update_ocr_image_record_path
    from app.vendor.csv_mysql_labeling.src.parse_catfood_guarantee import parse_catfood_guarantee_values
    from app.vendor.csv_mysql_labeling.src.parse_catfood_ocr import parse_catfood_ingredient_ocr_json
    from app.vendor.csv_mysql_labeling.src.settings import load_settings
except ModuleNotFoundError:
    from vendor.csv_mysql_labeling.src.db import make_engine
    from vendor.csv_mysql_labeling.src.ocr_image import run_ocr_image, update_ocr_image_record_path
    from vendor.csv_mysql_labeling.src.parse_catfood_guarantee import parse_catfood_guarantee_values
    from vendor.csv_mysql_labeling.src.parse_catfood_ocr import parse_catfood_ingredient_ocr_json
    from vendor.csv_mysql_labeling.src.settings import load_settings


DEFAULT_IMAGE_GLOB = "*.jpg,*.jpeg,*.png,*.bmp,*.webp,*.heic,*.heif,*.tif,*.tiff,*.jfif"
DEFAULT_OCR_TABLE = "catfood_ingredient_ocr_results"
DEFAULT_PARSED_TABLE = "catfood_ingredient_ocr_parsed"
DEFAULT_PRODUCT_INFO_TABLE = "product_info"
DEFAULT_GUARANTEE_TABLE = "product_guarantee"
DATA_ROOT = Path(os.getenv("CHONGXI_DATA_ROOT", "/home/admin/data/chongxi"))
DEFAULT_IMAGE_DIR = DATA_ROOT / "images"
DEFAULT_INGREDIENT_HISTORY_DIR = DATA_ROOT / "archive" / "ingredient_images"
DEFAULT_GUARANTEE_HISTORY_DIR = DATA_ROOT / "archive" / "guarantee_images"


def load_default_db_config() -> Dict[str, Any]:
    return dict(load_settings().mysql)


def load_model_configs() -> tuple[Dict[str, Any], Dict[str, Any]]:
    settings = load_settings()
    return dict(settings.ocr), dict(settings.openai)


def collect_image_files(image_dir: Path, patterns_text: str = DEFAULT_IMAGE_GLOB) -> List[Path]:
    if not image_dir.exists():
        raise FileNotFoundError(f"图片目录不存在: {image_dir}")
    if not image_dir.is_dir():
        raise NotADirectoryError(f"路径不是目录: {image_dir}")

    patterns = [p.strip().lower() for p in re.split(r"[,，\n\r]+", patterns_text or "") if p.strip()]
    if not patterns:
        patterns = [p.strip().lower() for p in DEFAULT_IMAGE_GLOB.split(",")]

    uniq: Dict[str, Path] = {}
    for file_path in image_dir.rglob("*"):
        if not file_path.is_file():
            continue
        rel = str(file_path.relative_to(image_dir)).replace("\\", "/").lower()
        name = file_path.name.lower()
        if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in patterns):
            uniq[str(file_path.resolve())] = file_path
    return [uniq[k] for k in sorted(uniq.keys())]


def _unique_target_path(path: Path) -> Path:
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    idx = 1
    while True:
        candidate = parent / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def move_to_history(image_path: Path, source_root: Path, history_root: Path) -> Path:
    history_root.mkdir(parents=True, exist_ok=True)
    try:
        rel = image_path.resolve().relative_to(source_root.resolve())
        target = history_root / rel
    except Exception:
        target = history_root / image_path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _unique_target_path(target)
    shutil.move(str(image_path), str(target))
    return target


def import_ingredient_images(
    *,
    image_dir: Path,
    db_config: Dict[str, Any],
    image_glob: str = DEFAULT_IMAGE_GLOB,
    table_name: str = DEFAULT_OCR_TABLE,
    history_dir: Path = DEFAULT_INGREDIENT_HISTORY_DIR,
    move_success_images: bool = True,
    sleep_seconds: float = 1.5,
) -> Dict[str, Any]:
    files = collect_image_files(image_dir, image_glob)
    if not files:
        raise FileNotFoundError(f"目录中未找到匹配图片: {image_glob}")

    ocr_cfg, openai_cfg = load_model_configs()
    engine = make_engine(db_config)
    succeeded: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    try:
        for idx, image_path in enumerate(files, start=1):
            if idx > 1 and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            try:
                result = run_ocr_image(
                    engine=engine,
                    image_path=image_path,
                    out_json_path=None,
                    table_name=table_name,
                    ocr_cfg=ocr_cfg,
                    openai_cfg=openai_cfg,
                )
                moved_path: Optional[Path] = None
                if move_success_images:
                    moved_path = move_to_history(
                        image_path=image_path,
                        source_root=image_dir,
                        history_root=history_dir,
                    )
                    update_ocr_image_record_path(
                        engine=engine,
                        table_name=table_name,
                        file_sha256=result["file_sha256"],
                        image_path=moved_path,
                    )
                succeeded.append(
                    {
                        "image_name": image_path.name,
                        "file_sha256": result["file_sha256"],
                        "ocr_text": result.get("ocr_text"),
                        "model_name": result["model_name"],
                        "model_latency_ms": result["model_latency_ms"],
                        "moved_path": str(moved_path) if moved_path else None,
                    }
                )
            except Exception as exc:
                failed.append({"image_name": image_path.name, "error": str(exc)})
    finally:
        engine.dispose()

    return {
        "total": len(files),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "table_name": table_name,
        "history_dir": str(history_dir) if move_success_images else None,
        "success_samples": succeeded[:10],
        "failure_samples": failed[:10],
    }


def fetch_ocr_context_by_sha256(
    *,
    db_config: Dict[str, Any],
    file_sha256: str,
    ocr_table: str = DEFAULT_OCR_TABLE,
    parsed_table: str = DEFAULT_PARSED_TABLE,
) -> Dict[str, Any]:
    """Return OCR text and parsed identifiers for a single imported image."""
    sha = str(file_sha256 or "").strip()
    if not sha:
        return {}

    engine = make_engine(db_config)
    try:
        with engine.connect() as conn:
            ocr_row = conn.execute(
                text(
                    f"""
                    SELECT id, image_path, image_name, file_sha256, ocr_text, model_name
                    FROM `{ocr_table}`
                    WHERE file_sha256 = :file_sha256
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {"file_sha256": sha},
            ).mappings().first()
            if not ocr_row:
                return {}

            parsed_row = conn.execute(
                text(
                    f"""
                    SELECT id, source_id, brand, product_name, ingredient_composition
                    FROM `{parsed_table}`
                    WHERE file_sha256 = :file_sha256
                       OR source_id = :source_id
                    ORDER BY
                        CASE WHEN source_id = :source_id THEN 0 ELSE 1 END,
                        id DESC
                    LIMIT 1
                    """
                ),
                {"file_sha256": sha, "source_id": ocr_row["id"]},
            ).mappings().first()

        context: Dict[str, Any] = dict(ocr_row)
        context["source_id"] = context.get("id")
        if parsed_row:
            context["parsed_row_id"] = parsed_row.get("id")
            context["ingredient_composition"] = parsed_row.get("ingredient_composition")
            context["brand"] = parsed_row.get("brand")
            context["product_name"] = parsed_row.get("product_name")
            context["parsed"] = dict(parsed_row)
        return context
    finally:
        engine.dispose()


def parse_ingredient_ocr(
    *,
    db_config: Dict[str, Any],
    source_table: str = DEFAULT_OCR_TABLE,
    target_table: str = DEFAULT_PARSED_TABLE,
    limit: int = 500,
    incremental_only: bool = True,
) -> Dict[str, Any]:
    engine = make_engine(db_config)
    try:
        result = parse_catfood_ingredient_ocr_json(
            engine=engine,
            source_table=source_table,
            target_table=target_table,
            limit=limit,
            incremental_only=incremental_only,
        )
        return {
            "scanned": result.scanned,
            "upserted": result.upserted,
            "source_table": result.source_table,
            "target_table": result.target_table,
            "batch_id": result.batch_id,
        }
    finally:
        engine.dispose()


def parse_guarantee_values(
    *,
    db_config: Dict[str, Any],
    source_table: str = DEFAULT_OCR_TABLE,
    parsed_table: str = DEFAULT_PARSED_TABLE,
    info_table: str = DEFAULT_PRODUCT_INFO_TABLE,
    guarantee_table: str = DEFAULT_GUARANTEE_TABLE,
    limit: int = 200,
    incremental_only: bool = True,
    processed_dir: Path = DEFAULT_GUARANTEE_HISTORY_DIR,
) -> Dict[str, Any]:
    ocr_cfg, openai_cfg = load_model_configs()
    engine = make_engine(db_config)
    try:
        result = parse_catfood_guarantee_values(
            engine=engine,
            openai_cfg=openai_cfg,
            ocr_cfg=ocr_cfg,
            source_table=source_table,
            parsed_table=parsed_table,
            info_table=info_table,
            guarantee_table=guarantee_table,
            limit=limit,
            incremental_only=incremental_only,
            processed_dir=processed_dir,
        )
        return {
            "scanned": result.scanned,
            "succeeded": result.succeeded,
            "empty_guarantees": result.empty_guarantees,
            "guarantee_rows": result.guarantee_rows,
            "failed": result.failed,
            "source_table": result.source_table,
            "parsed_table": result.parsed_table,
            "info_table": result.info_table,
            "guarantee_table": result.guarantee_table,
            "batch_id": result.batch_id,
            "error_samples": result.error_samples,
        }
    finally:
        engine.dispose()
