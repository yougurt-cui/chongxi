"""Lightweight data pipeline orchestration service."""

from __future__ import annotations

import importlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pymysql

from app_config import get_cat_food_upload_root, get_feature_mysql_config, get_mysql_config


TASK_CREATED = "created"
TASK_PROCESSING = "processing"
TASK_WAITING_INPUT = "waiting_input"
TASK_NEED_REVIEW = "need_review"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

NODE_PENDING = "pending"
NODE_READY = "ready"
NODE_RUNNING = "running"
NODE_SUCCESS = "success"
NODE_FAILED = "failed"
NODE_BLOCKED = "blocked"
NODE_NEED_REVIEW = "need_review"
NODE_WAITING_RESULT = "waiting_result"
NODE_SKIPPED = "skipped"

SUCCESS_CALL_STATUSES = {"success", "succeeded", "ok", "done"}
FAILURE_CALL_STATUSES = {"failure", "failed", "fail", "error", "timeout", "cancelled", "canceled"}
WAITING_CALL_STATUSES = {"waiting_result", "waiting", "pending", "running", "processing"}

BASE_DIR = Path(__file__).resolve().parents[1]
FIXED_UPLOAD_ROOT = get_cat_food_upload_root(BASE_DIR / "var" / "cat_food_uploads")

INGREDIENT_STANDARDIZE_POSITIVE_FIELDS = (
    "protein_structure_score",
    "protein_quality_score",
    "fat_oily_score",
    "fat_regulation_score",
    "fat_score",
    "omega_imbalance_score",
    "fat_mix_complexity_score",
    "p_form_score",
    "p_bulk_score",
    "p_buffer",
    "p_total_score",
    "q_feed",
    "q_scfa",
    "q_total_score",
    "starch_burden_score",
)

INGREDIENT_STANDARDIZE_ZERO_ALLOWED_FIELDS = {
    "p_form_score",
    "p_bulk_score",
    "p_buffer",
    "p_total_score",
    "q_feed",
    "q_scfa",
    "q_total_score",
}

FORMULA_PROFILE_REQUIRED_FIELDS = (
    "protein_score",
    "fat_burden_score",
    "carb_burden_score",
    "fiber_support_score",
)

OCR_KEYWORDS = (
    "配料",
    "成分",
    "营养",
    "保证",
    "粗蛋白",
    "粗脂肪",
    "粗纤维",
    "水分",
)

NUTRITION_PATTERNS = (
    re.compile(r"粗?蛋白[^0-9０-９]{0,12}[0-9０-９]+(?:[.．][0-9０-９]+)?\s*%?"),
    re.compile(r"粗?脂肪[^0-9０-９]{0,12}[0-9０-９]+(?:[.．][0-9０-９]+)?\s*%?"),
    re.compile(r"粗?纤维[^0-9０-９]{0,12}[0-9０-９]+(?:[.．][0-9０-９]+)?\s*%?"),
    re.compile(r"水分[^0-9０-９]{0,12}[0-9０-９]+(?:[.．][0-9０-９]+)?\s*%?"),
)


@dataclass(frozen=True)
class ApiRoute:
    method: str
    url: str
    timeout_seconds: int = 30


@dataclass(frozen=True)
class NodeDefinition:
    node_code: str
    node_name: str
    callable_path: str | None = None
    api: ApiRoute | None = None
    depends_on: tuple[str, ...] = ()
    payload_patch: dict[str, Any] = field(default_factory=dict)
    required_task_fields: tuple[str, ...] = ()
    skippable: bool = False
    priority: int = 100


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    status: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


PIPELINE_DEFINITIONS: dict[str, tuple[NodeDefinition, ...]] = {
    "catfood_image_analysis": (
        NodeDefinition(
            node_code="upload_check",
            node_name="上传内容检查",
            callable_path="services.orchestrator_service.noop_node",
            required_task_fields=("image_path",),
            priority=10,
        ),
        NodeDefinition(
            node_code="ocr_formula",
            node_name="配料表 OCR 识别",
            callable_path="services.orchestrator_service.noop_node",
            api=ApiRoute(method="POST", url="/api/catfood/ingredients/ingest", timeout_seconds=300),
            depends_on=("upload_check",),
            priority=20,
        ),
        NodeDefinition(
            node_code="ingredient_extract",
            node_name="原料抽取",
            callable_path="services.orchestrator_service.noop_node",
            api=ApiRoute(method="POST", url="/api/consumer/features/engineer", timeout_seconds=300),
            depends_on=("formula_input_build",),
            priority=30,
        ),
        NodeDefinition(
            node_code="brand_standardize",
            node_name="品牌标准化",
            callable_path="services.catfood_standardization_service.standardize_brand",
            api=ApiRoute(method="POST", url="/api/catfood/standardization/brand", timeout_seconds=60),
            depends_on=("ocr_formula",),
            priority=21,
        ),
        NodeDefinition(
            node_code="product_standardize",
            node_name="产品标准化",
            callable_path="services.catfood_standardization_service.standardize_product",
            api=ApiRoute(method="POST", url="/api/catfood/standardization/product", timeout_seconds=60),
            depends_on=("brand_standardize",),
            priority=22,
        ),
        NodeDefinition(
            node_code="formula_standardize",
            node_name="配方标准化",
            callable_path="services.catfood_standardization_service.standardize_formula",
            api=ApiRoute(method="POST", url="/api/catfood/standardization/formula", timeout_seconds=60),
            depends_on=("product_standardize",),
            priority=23,
        ),
        NodeDefinition(
            node_code="formula_input_build",
            node_name="配方计算输入准备",
            callable_path="services.catfood_standardization_service.build_formula_feature_input",
            api=ApiRoute(
                method="POST",
                url="/api/catfood/standardization/formula-input/build",
                timeout_seconds=60,
            ),
            depends_on=("formula_standardize",),
            priority=24,
        ),
        NodeDefinition(
            node_code="ingredient_standardize",
            node_name="原料标准化",
            callable_path="services.orchestrator_service.noop_node",
            api=ApiRoute(method="POST", url="/api/consumer/materials/scores/calculate", timeout_seconds=300),
            depends_on=("ingredient_extract",),
            priority=40,
        ),
        NodeDefinition(
            node_code="formula_profile",
            node_name="配方画像生成",
            callable_path="services.orchestrator_service.noop_node",
            api=ApiRoute(method="POST", url="/api/consumer/risks/calculate", timeout_seconds=300),
            depends_on=("ingredient_standardize",),
            priority=50,
        ),
    ),
    "catfood_ingredient_ingest": (
        NodeDefinition(
            node_code="ocr_import",
            node_name="原料图片 OCR 导入",
            callable_path="services.pipeline_service.ingest_catfood_ingredients",
            api=ApiRoute(method="POST", url="/api/catfood/ingredients/ingest", timeout_seconds=300),
            payload_patch={"steps": ["ocr_import"]},
            priority=10,
        ),
        NodeDefinition(
            node_code="parse_ocr_json",
            node_name="OCR JSON 结构化解析",
            callable_path="services.pipeline_service.ingest_catfood_ingredients",
            api=ApiRoute(method="POST", url="/api/catfood/ingredients/ingest", timeout_seconds=300),
            depends_on=("ocr_import",),
            payload_patch={"steps": ["parse_ocr_json"]},
            priority=20,
        ),
        NodeDefinition(
            node_code="parse_guarantee",
            node_name="保证值解析",
            callable_path="services.pipeline_service.ingest_catfood_ingredients",
            api=ApiRoute(method="POST", url="/api/catfood/ingredients/ingest", timeout_seconds=300),
            depends_on=("parse_ocr_json",),
            payload_patch={"steps": ["parse_guarantee"]},
            priority=30,
        ),
    ),
    "process_signal": (
        NodeDefinition(
            node_code="extract_candidates",
            node_name="工艺信号候选抽取",
            callable_path="services.process_signal_service.run_process_signal_pipeline",
            api=ApiRoute(method="POST", url="/api/process-signals/run", timeout_seconds=300),
            payload_patch={"steps": ["extract_candidates"]},
            priority=10,
        ),
        NodeDefinition(
            node_code="standardize",
            node_name="工艺信号标准化",
            callable_path="services.process_signal_service.run_process_signal_pipeline",
            api=ApiRoute(method="POST", url="/api/process-signals/run", timeout_seconds=300),
            depends_on=("extract_candidates",),
            payload_patch={"steps": ["standardize"]},
            priority=20,
        ),
    ),
}


def noop_node(payload: dict[str, Any]) -> dict[str, Any]:
    """Placeholder node callable for initialized pipelines that are not wired to workers yet."""
    return {
        "ok": True,
        "message": "节点已初始化，等待接入具体执行器。",
        "orchestrator_task_id": payload.get("orchestrator_task_id"),
        "orchestrator_node_code": payload.get("orchestrator_node_code"),
    }


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _get_output_value(output: dict[str, Any], field_name: str) -> Any:
    if field_name in output:
        return output.get(field_name)
    profile = output.get("profile")
    if isinstance(profile, dict) and field_name in profile:
        return profile.get(field_name)
    metrics = output.get("metrics")
    if isinstance(metrics, dict) and field_name in metrics:
        return metrics.get(field_name)
    data = output.get("data")
    if isinstance(data, dict) and field_name in data:
        return data.get(field_name)
    return None


def _effective_text_ratio(text: str) -> float:
    if not text:
        return 0.0
    total_chars = [char for char in text if not char.isspace()]
    if not total_chars:
        return 0.0
    effective_chars = [
        char
        for char in total_chars
        if re.match(r"[\u4e00-\u9fffA-Za-z0-9０-９]", char)
    ]
    return len(effective_chars) / len(total_chars)


def _repeat_ngram_ratio(text: str, ngram_size: int = 8) -> float:
    normalized = re.sub(r"\s+", "", text or "")
    if len(normalized) < ngram_size * 2:
        return 0.0
    ngrams = [
        normalized[index:index + ngram_size]
        for index in range(0, len(normalized) - ngram_size + 1)
    ]
    if not ngrams:
        return 0.0
    repeated = len(ngrams) - len(set(ngrams))
    return repeated / len(ngrams)


def _nutrition_match_count(text: str) -> int:
    return sum(1 for pattern in NUTRITION_PATTERNS if pattern.search(text or ""))


def _check_upload(output: dict[str, Any], task_payload: dict[str, Any]) -> CheckResult:
    image_path_value = output.get("image_path") or task_payload.get("image_path")
    if not image_path_value:
        return CheckResult(False, NODE_BLOCKED, "缺少 image_path")

    image_path = Path(str(image_path_value)).expanduser()
    if not image_path.exists() or not image_path.is_file():
        return CheckResult(False, NODE_FAILED, f"图片文件不存在: {image_path}")

    try:
        image_path.resolve().relative_to(FIXED_UPLOAD_ROOT.resolve())
    except ValueError:
        return CheckResult(
            False,
            NODE_FAILED,
            f"图片未落到固定上传目录: {FIXED_UPLOAD_ROOT}",
            {"image_path": str(image_path), "expected_root": str(FIXED_UPLOAD_ROOT)},
        )

    return CheckResult(
        True,
        NODE_SUCCESS,
        "上传图片已落到固定目录",
        {"image_path": str(image_path), "fixed_upload_root": str(FIXED_UPLOAD_ROOT)},
    )


def _check_ocr_formula(output: dict[str, Any]) -> CheckResult:
    ocr_text = str(output.get("ocr_text") or output.get("text") or "").strip()
    if not ocr_text:
        return CheckResult(False, NODE_FAILED, "ocr_text 为空")

    keyword_hits = [keyword for keyword in OCR_KEYWORDS if keyword in ocr_text]
    if not keyword_hits:
        return CheckResult(False, NODE_NEED_REVIEW, "OCR 初步文本未命中配料/营养关键词")

    nutrition_count = _nutrition_match_count(ocr_text)
    nutrition_values = output.get("nutrition_values") or output.get("guarantee_values")
    has_structured_nutrition = isinstance(nutrition_values, dict) and any(_is_non_empty(value) for value in nutrition_values.values())

    effective_ratio = _effective_text_ratio(ocr_text)
    if effective_ratio < 0.55:
        return CheckResult(
            False,
            NODE_NEED_REVIEW,
            "OCR 有效字符占比过低",
            {
                "effective_ratio": round(effective_ratio, 4),
                "threshold": 0.55,
                "nutrition_pattern_count": nutrition_count,
                "has_structured_nutrition": has_structured_nutrition,
            },
        )
    if effective_ratio < 0.7 and nutrition_count < 2 and not has_structured_nutrition:
        return CheckResult(
            False,
            NODE_NEED_REVIEW,
            "OCR 有效字符占比偏低且营养数值证据不足",
            {
                "effective_ratio": round(effective_ratio, 4),
                "threshold": 0.7,
                "nutrition_pattern_count": nutrition_count,
                "has_structured_nutrition": has_structured_nutrition,
            },
        )

    repeat_ratio = _repeat_ngram_ratio(ocr_text)
    if repeat_ratio > 0.35:
        return CheckResult(
            False,
            NODE_NEED_REVIEW,
            "OCR 文本重复率过高",
            {"repeat_ratio": round(repeat_ratio, 4)},
        )

    if nutrition_count < 2 and not has_structured_nutrition:
        return CheckResult(
            False,
            NODE_NEED_REVIEW,
            "营养数值识别不足",
            {
                "nutrition_pattern_count": nutrition_count,
                "effective_ratio": round(effective_ratio, 4),
            },
        )

    return CheckResult(
        True,
        NODE_SUCCESS,
        "OCR 输出检查通过",
        {
            "keyword_hits": keyword_hits,
            "effective_ratio": round(effective_ratio, 4),
            "effective_ratio_warning": effective_ratio < 0.7,
            "repeat_ratio": round(repeat_ratio, 4),
            "nutrition_pattern_count": nutrition_count,
        },
    )


def _check_ingredient_extract(output: dict[str, Any]) -> CheckResult:
    source_id = _get_output_value(output, "source_id")
    parsed_row_id = _get_output_value(output, "parsed_row_id")
    product_key = _get_output_value(output, "product_key")
    if not _is_non_empty(product_key):
        brand = _get_output_value(output, "brand")
        product_name = _get_output_value(output, "product_name")
        if _is_non_empty(brand) and _is_non_empty(product_name):
            product_key = f"{brand}||{product_name}"

    if not any(_is_non_empty(value) for value in (source_id, parsed_row_id, product_key)):
        return CheckResult(
            False,
            NODE_FAILED,
            "原料特征检查缺少单图定位字段",
            {
                "required_identity_fields": ["source_id", "parsed_row_id", "product_key"],
            },
        )

    parsed_check = _check_parsed_ingredient_ready(
        source_id=source_id,
        parsed_row_id=parsed_row_id,
        product_key=product_key,
        output=output,
    )
    if parsed_check:
        return parsed_check

    return _check_ingredient_feature_tables(
        source_id=source_id,
        parsed_row_id=parsed_row_id,
        product_key=product_key,
    )


def _check_parsed_ingredient_ready(
    *,
    source_id: Any,
    parsed_row_id: Any,
    product_key: Any,
    output: dict[str, Any] | None = None,
) -> CheckResult | None:
    parsed = (output or {}).get("parsed")
    ingredient_text = _get_output_value(output or {}, "ingredient_composition")
    details: dict[str, Any] = {
        "source_id": source_id,
        "parsed_row_id": parsed_row_id,
        "product_key": product_key,
        "table": "catfood_ingredient_ocr_parsed",
    }

    if isinstance(parsed, dict):
        details["parsed"] = {
            key: parsed.get(key)
            for key in ("id", "source_id", "brand", "product_name", "ingredient_composition")
            if key in parsed
        }
        ingredient_text = ingredient_text or parsed.get("ingredient_composition")

    row = None
    if not _is_non_empty(ingredient_text):
        try:
            row = _fetch_parsed_ingredient_row(source_id=source_id, parsed_row_id=parsed_row_id)
        except Exception as exc:
            return CheckResult(False, NODE_FAILED, f"原料解析表检查失败: {exc}", details)
        if row:
            details["parsed"] = {
                key: row.get(key)
                for key in ("id", "source_id", "brand", "product_name", "ingredient_composition")
                if key in row
            }
            ingredient_text = row.get("ingredient_composition")

    if not row and not isinstance(parsed, dict):
        details["parsed"] = {"found": False}

    if not _is_non_empty(ingredient_text):
        return CheckResult(
            False,
            NODE_FAILED,
            "原料解析结果为空，请先重跑 OCR 解析",
            details,
        )
    return None


def _check_ingredient_feature_tables(
    *,
    source_id: Any,
    parsed_row_id: Any,
    product_key: Any,
) -> CheckResult:
    details: dict[str, Any] = {
        "source_id": source_id,
        "parsed_row_id": parsed_row_id,
        "product_key": product_key,
        "tables": {},
    }
    missing_tables: list[str] = []
    missing_fields: dict[str, list[str]] = {}

    try:
        with _connect_feature() as conn:
            with conn.cursor() as cursor:
                protein_row = _fetch_protein_feature_row(cursor, source_id, product_key)
                fat_row = _fetch_fat_feature_row(cursor, source_id, product_key)
                fiber_row = _fetch_fiber_feature_row(cursor, parsed_row_id, product_key)
    except Exception as exc:
        return CheckResult(False, NODE_FAILED, f"原料特征表检查失败: {exc}", details)

    protein_required = ("animal_sources", "protein_source_details", "animal_source_level1_categories")
    fat_any_fields = (
        "fat_sources",
        "antioxidant_sources",
        "micronutrient_sources",
        "omega6_sources",
        "omega3_sources",
        "guarantee_crude_fat_value",
    )
    fiber_required = ("ingredient_feature_json",)

    _collect_feature_check_result(
        table_name="protein_source_aggregate",
        row=protein_row,
        required_fields=protein_required,
        missing_tables=missing_tables,
        missing_fields=missing_fields,
        details=details,
    )
    _collect_feature_check_result(
        table_name="catfood_fiber_feature_json",
        row=fiber_row,
        required_fields=fiber_required,
        missing_tables=missing_tables,
        missing_fields=missing_fields,
        details=details,
    )
    if not fat_row:
        missing_tables.append("catfood_fat_material_features")
        details["tables"]["catfood_fat_material_features"] = {"found": False}
    else:
        present_fat_fields = [field for field in fat_any_fields if _is_non_empty(fat_row.get(field))]
        details["tables"]["catfood_fat_material_features"] = {
            "found": True,
            "required_any_fields": list(fat_any_fields),
            "present_fields": present_fat_fields,
            "row_identity": {
                key: fat_row.get(key)
                for key in ("id", "source_id", "product_key")
                if key in fat_row
            },
        }
        if not present_fat_fields:
            missing_fields["catfood_fat_material_features"] = ["/".join(fat_any_fields)]

    if missing_tables or missing_fields:
        return CheckResult(
            False,
            NODE_FAILED,
            "原料特征表检查未通过",
            {
                **details,
                "missing_tables": missing_tables,
                "missing_fields": missing_fields,
            },
        )

    return CheckResult(True, NODE_SUCCESS, "原料特征表检查通过", details)


def _collect_feature_check_result(
    *,
    table_name: str,
    row: dict[str, Any] | None,
    required_fields: tuple[str, ...],
    missing_tables: list[str],
    missing_fields: dict[str, list[str]],
    details: dict[str, Any],
) -> None:
    if not row:
        missing_tables.append(table_name)
        details["tables"][table_name] = {"found": False}
        return
    missing = [field for field in required_fields if not _is_non_empty(row.get(field))]
    present_fields = [field for field in required_fields if _is_non_empty(row.get(field))]
    details["tables"][table_name] = {
        "found": True,
        "required_fields": list(required_fields),
        "present_fields": present_fields,
        "missing_fields": missing,
        "row_identity": {
            key: row.get(key)
            for key in ("id", "source_id", "product_key", "source_ids")
            if key in row
        },
    }
    if missing:
        missing_fields[table_name] = missing


def _check_ingredient_standardize(output: dict[str, Any]) -> CheckResult:
    if any(_is_non_empty(_get_output_value(output, field_name)) for field_name in INGREDIENT_STANDARDIZE_POSITIVE_FIELDS):
        return _check_required_fields(
            output,
            INGREDIENT_STANDARDIZE_POSITIVE_FIELDS,
            positive=True,
            zero_allowed_fields=INGREDIENT_STANDARDIZE_ZERO_ALLOWED_FIELDS,
        )

    source_id = _get_output_value(output, "source_id")
    product_key = _get_output_value(output, "product_key")
    if not _is_non_empty(product_key):
        brand = _get_output_value(output, "brand")
        product_name = _get_output_value(output, "product_name")
        if _is_non_empty(brand) and _is_non_empty(product_name):
            product_key = f"{brand}||{product_name}"

    if not any(_is_non_empty(value) for value in (source_id, product_key)):
        return CheckResult(
            False,
            NODE_FAILED,
            "标准化评分检查缺少单图定位字段",
            {"required_identity_fields": ["source_id", "product_key"]},
        )

    try:
        with _connect_feature() as conn:
            with conn.cursor() as cursor:
                row = _fetch_score_wide_row(cursor, source_id=source_id, product_key=product_key)
    except Exception as exc:
        return CheckResult(False, NODE_FAILED, f"标准化评分表检查失败: {exc}")

    if not row:
        return CheckResult(
            False,
            NODE_FAILED,
            "标准化评分宽表缺少单图记录",
            {"source_id": source_id, "product_key": product_key, "table": "catfood_protein_fat_fiber_score_wide"},
        )

    missing_fields: list[str] = []
    non_positive_fields: list[str] = []
    for field_name in INGREDIENT_STANDARDIZE_POSITIVE_FIELDS:
        value = row.get(field_name)
        if not _is_non_empty(value):
            missing_fields.append(field_name)
            continue
        try:
            if float(value) < 0 or (
                field_name not in INGREDIENT_STANDARDIZE_ZERO_ALLOWED_FIELDS and float(value) == 0
            ):
                non_positive_fields.append(field_name)
        except (TypeError, ValueError):
            non_positive_fields.append(field_name)

    details = {
        "source_id": source_id,
        "product_key": product_key,
        "table": "catfood_protein_fat_fiber_score_wide",
        "required_fields": list(INGREDIENT_STANDARDIZE_POSITIVE_FIELDS),
        "missing_fields": missing_fields,
        "non_positive_fields": non_positive_fields,
    }
    if missing_fields or non_positive_fields:
        return CheckResult(False, NODE_FAILED, "标准化评分字段检查未通过", details)

    return CheckResult(True, NODE_SUCCESS, "标准化评分宽表检查通过", details)


def _fetch_score_wide_row(cursor: pymysql.cursors.DictCursor, source_id: Any, product_key: Any) -> dict[str, Any] | None:
    where, params = _feature_identity_where(source_id=source_id, product_key=product_key)
    if not where:
        return None
    cursor.execute(
        f"""
        SELECT source_id, product_key, brand, product_name,
               protein_structure_score, protein_quality_score,
               fat_oily_score, fat_regulation_score, fat_score,
               omega_imbalance_score, fat_mix_complexity_score,
               p_form_score, p_bulk_score, p_buffer, p_total_score,
               q_feed, q_scfa, q_total_score, starch_burden_score
        FROM catfood_protein_fat_fiber_score_wide
        WHERE {where}
        LIMIT 1
        """,
        params,
    )
    return cursor.fetchone()


def _check_formula_profile(output: dict[str, Any]) -> CheckResult:
    if any(_is_non_empty(_get_output_value(output, field_name)) for field_name in FORMULA_PROFILE_REQUIRED_FIELDS):
        return _check_required_fields(output, FORMULA_PROFILE_REQUIRED_FIELDS)

    source_id = _get_output_value(output, "source_id")
    product_key = _get_output_value(output, "product_key") or _get_output_value(output, "sku_id")
    if not _is_non_empty(product_key):
        brand = _get_output_value(output, "brand")
        product_name = _get_output_value(output, "product_name")
        if _is_non_empty(brand) and _is_non_empty(product_name):
            product_key = f"{brand}||{product_name}"

    if not any(_is_non_empty(value) for value in (source_id, product_key)):
        return CheckResult(
            False,
            NODE_FAILED,
            "配方画像检查缺少单图定位字段",
            {"required_identity_fields": ["source_id", "product_key/sku_id"]},
        )

    try:
        with _connect_feature() as conn:
            with conn.cursor() as cursor:
                wide_row = _fetch_score_wide_row(cursor, source_id=source_id, product_key=product_key)
                product_key_candidates = _formula_profile_product_key_candidates(product_key, wide_row)
                if not _is_non_empty(product_key) and product_key_candidates:
                    product_key = product_key_candidates[0]
                sku_feature_row, resolved_product_key = _fetch_first_sku_feature_input_row(
                    cursor,
                    product_key_candidates,
                )
                if _is_non_empty(resolved_product_key):
                    product_key = resolved_product_key
                risk_rows = _fetch_risk_result_rows(cursor, product_key)
    except Exception as exc:
        return CheckResult(False, NODE_FAILED, f"配方画像结果表检查失败: {exc}")

    details = {
        "source_id": source_id,
        "product_key": product_key,
        "wide_product_key": wide_row.get("product_key") if wide_row else None,
        "tables": {
            "sku_feature_input": {"found": bool(sku_feature_row)},
            "sku_risk_score_result": {
                "found_count": len(risk_rows),
                "required_score_model_versions": [
                    "BLACK_CHIN_M2_FAT_OMEGA_FAT_B",
                    "SOFT_STOOL_M2_PQ_NO_G_FAT_B",
                ],
            },
        },
    }
    if not sku_feature_row:
        return CheckResult(False, NODE_FAILED, "画像输入表缺少单图记录", details)

    sku_required = ("protein_score", "fat_score", "carb_score", "fiber_score")
    sku_missing = [field for field in sku_required if not _is_non_empty(sku_feature_row.get(field))]
    details["tables"]["sku_feature_input"]["required_fields"] = list(sku_required)
    details["tables"]["sku_feature_input"]["missing_fields"] = sku_missing
    if sku_missing:
        return CheckResult(False, NODE_FAILED, "画像输入字段检查未通过", details)

    risk_versions = {str(row.get("score_model_version") or "") for row in risk_rows}
    required_versions = {
        "BLACK_CHIN_M2_FAT_OMEGA_FAT_B",
        "SOFT_STOOL_M2_PQ_NO_G_FAT_B",
    }
    missing_versions = sorted(required_versions - risk_versions)
    details["tables"]["sku_risk_score_result"]["present_score_model_versions"] = sorted(risk_versions)
    details["tables"]["sku_risk_score_result"]["missing_score_model_versions"] = missing_versions
    if missing_versions:
        return CheckResult(False, NODE_FAILED, "风险画像结果表缺少模型结果", details)

    risk_required = ("history_percentile", "current_pool_percentile", "final_risk_type")
    risk_missing: dict[str, list[str]] = {}
    for row in risk_rows:
        version = str(row.get("score_model_version") or "")
        if version not in required_versions:
            continue
        missing = [field for field in risk_required if not _is_non_empty(row.get(field))]
        if missing:
            risk_missing[version] = missing
    details["tables"]["sku_risk_score_result"]["required_fields"] = list(risk_required)
    details["tables"]["sku_risk_score_result"]["missing_fields_by_model"] = risk_missing
    if risk_missing:
        return CheckResult(False, NODE_FAILED, "风险画像结果字段检查未通过", details)

    return CheckResult(True, NODE_SUCCESS, "配方画像结果表检查通过", details)


def _formula_profile_product_key_candidates(
    product_key: Any,
    wide_row: dict[str, Any] | None,
) -> list[str]:
    candidates: list[str] = []

    def add(value: Any) -> None:
        if not _is_non_empty(value):
            return
        text = str(value)
        if text not in candidates:
            candidates.append(text)

    add(product_key)
    if wide_row:
        add(wide_row.get("product_key"))
        add(_normalized_product_key(brand=wide_row.get("brand"), product_name=wide_row.get("product_name")))
    return candidates


def _normalized_product_key(*, brand: Any, product_name: Any) -> str:
    if not (_is_non_empty(brand) and _is_non_empty(product_name)):
        return ""
    try:
        brand_normalizer = importlib.import_module("vendor.feature_score_pipeline.scripts.brand_normalizer")
        canonical_brand = brand_normalizer.canonicalize_brand(brand)
        if canonical_brand and not brand_normalizer.is_generic_brand(canonical_brand):
            corrected_brand = canonical_brand
        else:
            corrected_brand = brand_normalizer.correct_brand(brand, product_name)
        return brand_normalizer.build_product_key(corrected_brand, product_name)
    except Exception:
        return f"{brand}||{product_name}"


def _fetch_sku_feature_input_row(cursor: pymysql.cursors.DictCursor, product_key: Any) -> dict[str, Any] | None:
    if not _is_non_empty(product_key):
        return None
    cursor.execute(
        """
        SELECT sku_id, sku_name, brand_name, feature_version,
               protein_score, carb_score, fiber_score, fat_score,
               prebiotic_score, antioxidant_score, p_buffer, q_feed, q_scfa
        FROM sku_feature_input
        WHERE sku_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (str(product_key),),
    )
    return cursor.fetchone()


def _fetch_first_sku_feature_input_row(
    cursor: pymysql.cursors.DictCursor,
    product_keys: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    for key in product_keys:
        row = _fetch_sku_feature_input_row(cursor, key)
        if row:
            return row, key
    return None, None


def _fetch_risk_result_rows(cursor: pymysql.cursors.DictCursor, product_key: Any) -> list[dict[str, Any]]:
    if not _is_non_empty(product_key):
        return []
    cursor.execute(
        """
        SELECT sku_id, sku_name, brand_name, feature_version, score_model_version,
               history_percentile, history_risk_level,
               current_pool_percentile, current_pool_risk_level,
               final_risk_type, reason_tags, calculated_at
        FROM sku_risk_score_result
        WHERE sku_id = %s
        ORDER BY calculated_at DESC
        """,
        (str(product_key),),
    )
    return list(cursor.fetchall() or [])


def _fetch_protein_feature_row(cursor: pymysql.cursors.DictCursor, source_id: Any, product_key: Any) -> dict[str, Any] | None:
    where, params = _feature_identity_where(source_id=source_id, product_key=product_key)
    if not where:
        return None
    cursor.execute(
        f"""
        SELECT source_id, product_key, animal_sources, animal_source_level1_categories,
               animal_source_level2_sources, protein_source_details,
               primary_meat_source_species, secondary_meat_source_species
        FROM protein_source_aggregate
        WHERE {where}
        ORDER BY aggregated_at DESC
        LIMIT 1
        """,
        params,
    )
    return cursor.fetchone()


def _fetch_parsed_ingredient_row(source_id: Any, parsed_row_id: Any) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            if _is_non_empty(source_id):
                cursor.execute(
                    """
                    SELECT id, source_id, brand, product_name, ingredient_composition
                    FROM catfood_ingredient_ocr_parsed
                    WHERE source_id = %s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (int(source_id),),
                )
                row = cursor.fetchone()
                if row:
                    return row
            if _is_non_empty(parsed_row_id):
                cursor.execute(
                    """
                    SELECT id, source_id, brand, product_name, ingredient_composition
                    FROM catfood_ingredient_ocr_parsed
                    WHERE id = %s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (int(parsed_row_id),),
                )
                return cursor.fetchone()
    return None


def _fetch_fat_feature_row(cursor: pymysql.cursors.DictCursor, source_id: Any, product_key: Any) -> dict[str, Any] | None:
    where, params = _feature_identity_where(source_id=source_id, product_key=product_key)
    if not where:
        return None
    cursor.execute(
        f"""
        SELECT source_id, product_key, fat_sources, antioxidant_sources,
               micronutrient_sources, omega6_sources, omega3_sources,
               guarantee_crude_fat_value
        FROM catfood_fat_material_features
        WHERE {where}
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        params,
    )
    return cursor.fetchone()


def _fetch_fiber_feature_row(cursor: pymysql.cursors.DictCursor, parsed_row_id: Any, product_key: Any) -> dict[str, Any] | None:
    clauses: list[str] = []
    params: list[Any] = []
    if _is_non_empty(product_key):
        clauses.append("product_key = %s")
        params.append(str(product_key))
    if _is_non_empty(parsed_row_id):
        clauses.append("JSON_CONTAINS(source_ids, CAST(%s AS JSON))")
        params.append(str(int(parsed_row_id)))
    if not clauses:
        return None
    cursor.execute(
        f"""
        SELECT id, product_key, source_ids, raw_ingredient_text,
               ingredient_feature_json, starch_ingredients_json
        FROM catfood_fiber_feature_json
        WHERE {" OR ".join(clauses)}
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        params,
    )
    return cursor.fetchone()


def _feature_identity_where(*, source_id: Any, product_key: Any) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if _is_non_empty(source_id):
        clauses.append("source_id = %s")
        params.append(int(source_id))
    if _is_non_empty(product_key):
        clauses.append("product_key = %s")
        params.append(str(product_key))
    return " OR ".join(clauses), params


def _check_required_fields(
    output: dict[str, Any],
    required_fields: tuple[str, ...],
    *,
    positive: bool = False,
    zero_allowed_fields: set[str] | None = None,
) -> CheckResult:
    missing_fields = []
    non_positive_fields = []
    zero_allowed_fields = zero_allowed_fields or set()
    for field_name in required_fields:
        value = _get_output_value(output, field_name)
        if not _is_non_empty(value):
            missing_fields.append(field_name)
            continue
        if positive:
            try:
                numeric_value = float(value)
                if numeric_value < 0 or (field_name not in zero_allowed_fields and numeric_value == 0):
                    non_positive_fields.append(field_name)
            except (TypeError, ValueError):
                non_positive_fields.append(field_name)

    if missing_fields or non_positive_fields:
        details = {
            "missing_fields": missing_fields,
            "non_positive_fields": non_positive_fields,
            "required_fields": list(required_fields),
        }
        return CheckResult(False, NODE_FAILED, "节点输出字段检查未通过", details)

    return CheckResult(True, NODE_SUCCESS, "节点输出字段检查通过", {"required_fields": list(required_fields)})


def output_check(node_code: str, output: dict[str, Any], task_payload: dict[str, Any] | None = None) -> CheckResult:
    task_payload = dict(task_payload or {})
    output = dict(output or {})
    if node_code == "upload_check":
        return _check_upload(output, task_payload)
    if node_code == "ocr_formula":
        return _check_ocr_formula(output)
    if node_code in {"brand_standardize", "product_standardize", "formula_standardize"}:
        status_field = {
            "brand_standardize": "brand_status",
            "product_standardize": "product_status",
            "formula_standardize": "formula_status",
        }[node_code]
        status = str(output.get(status_field) or "").strip()
        if status == "matched":
            return CheckResult(True, NODE_SUCCESS, f"{node_code} 匹配成功")
        if status in {"pending", "conflict", "blocked"}:
            return CheckResult(
                False,
                NODE_NEED_REVIEW,
                str(output.get("reason") or f"{node_code} 状态: {status}"),
                {"standardization_status": status},
            )
        return CheckResult(False, NODE_FAILED, f"{node_code} 返回未知状态: {status or 'empty'}")
    if node_code == "formula_input_build":
        status = str(output.get("build_status") or "").strip()
        if status in {"ready", "nutrition_missing"} and output.get("formula_id"):
            return CheckResult(True, NODE_SUCCESS, f"配方输入已准备: {status}")
        if status == "blocked":
            return CheckResult(False, NODE_NEED_REVIEW, "配方输入缺少有效原料")
        return CheckResult(False, NODE_FAILED, f"配方输入状态异常: {status or 'empty'}")
    if node_code == "ingredient_extract":
        return _check_ingredient_extract(output)
    if node_code == "ingredient_standardize":
        return _check_ingredient_standardize(output)
    if node_code == "formula_profile":
        return _check_formula_profile(output)
    if output:
        return CheckResult(True, NODE_SUCCESS, "默认输出检查通过")
    return CheckResult(False, NODE_FAILED, "输出为空")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> pymysql.connections.Connection:
    cfg = get_mysql_config()
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg.get("port", 3306)),
        user=cfg["user"],
        password=str(cfg.get("password", "")),
        database=cfg["database"],
        charset=str(cfg.get("charset") or "utf8mb4"),
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _connect_feature() -> pymysql.connections.Connection:
    cfg = get_feature_mysql_config()
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg.get("port", 3306)),
        user=cfg["user"],
        password=str(cfg.get("password", "")),
        database=cfg["database"],
        charset=str(cfg.get("charset") or "utf8mb4"),
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def init_db() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_task (
                    id VARCHAR(64) PRIMARY KEY,
                    task_type VARCHAR(96) NOT NULL,
                    task_status VARCHAR(32) NOT NULL,
                    payload_json LONGTEXT NOT NULL,
                    error_message TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    finished_at DATETIME NULL,
                    KEY idx_pipeline_task_type_status (task_type, task_status),
                    KEY idx_pipeline_task_updated_at (updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_task_node (
                    id VARCHAR(96) PRIMARY KEY,
                    task_id VARCHAR(64) NOT NULL,
                    node_code VARCHAR(96) NOT NULL,
                    node_name VARCHAR(255) NOT NULL,
                    node_status VARCHAR(32) NOT NULL,
                    priority INT NOT NULL DEFAULT 100,
                    error_message TEXT,
                    started_at DATETIME NULL,
                    finished_at DATETIME NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uniq_pipeline_task_node (task_id, node_code),
                    KEY idx_pipeline_task_node_status (node_status, priority),
                    KEY idx_pipeline_task_node_task_id (task_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_node_output (
                    id VARCHAR(96) PRIMARY KEY,
                    task_id VARCHAR(64) NOT NULL,
                    node_code VARCHAR(96) NOT NULL,
                    output_json LONGTEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uniq_pipeline_node_output (task_id, node_code),
                    KEY idx_pipeline_node_output_task_id (task_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def get_pipeline_definition(task_type: str) -> tuple[NodeDefinition, ...]:
    definition = PIPELINE_DEFINITIONS.get(task_type)
    if not definition:
        supported = ", ".join(sorted(PIPELINE_DEFINITIONS))
        raise ValueError(f"unsupported task_type: {task_type}; supported: {supported}")
    return definition


def create_task(task_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_db()
    task_type = str(task_type or "").strip()
    payload = dict(payload or {})
    nodes = get_pipeline_definition(task_type)
    task_id = uuid.uuid4().hex
    now = utc_now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pipeline_task
                    (id, task_type, task_status, payload_json, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (task_id, task_type, TASK_CREATED, _json_dumps(payload), now, now),
            )
            for node in nodes:
                cursor.execute(
                    """
                    INSERT INTO pipeline_task_node
                        (id, task_id, node_code, node_name, node_status, priority, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        f"{task_id}:{node.node_code}",
                        task_id,
                        node.node_code,
                        node.node_name,
                        NODE_PENDING,
                        node.priority,
                        now,
                        now,
                    ),
                )
        conn.commit()
    return get_task(task_id) or {"id": task_id}


def _task_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_type": row["task_type"],
        "task_status": row["task_status"],
        "payload": _json_loads(row["payload_json"], {}),
        "error_message": row["error_message"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
    }


def _node_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_code": row["node_code"],
        "node_name": row["node_name"],
        "node_status": row["node_status"],
        "priority": row["priority"],
        "error_message": row["error_message"],
        "started_at": str(row["started_at"]) if row["started_at"] else None,
        "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def get_task(task_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM pipeline_task WHERE id = %s", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "SELECT * FROM pipeline_task_node WHERE task_id = %s ORDER BY priority ASC, created_at ASC",
                (task_id,),
            )
            node_rows = cursor.fetchall()
            cursor.execute(
                "SELECT * FROM pipeline_node_output WHERE task_id = %s ORDER BY created_at ASC",
                (task_id,),
            )
            output_rows = cursor.fetchall()
    task = _task_from_row(row)
    task["nodes"] = [_node_from_row(node) for node in node_rows]
    task["outputs"] = {
        row["node_code"]: _json_loads(row["output_json"], {})
        for row in output_rows
    }
    return task


def list_review_items(limit: int = 50, statuses: list[str] | None = None) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 50), 200))
    allowed_statuses = [
        str(status).strip()
        for status in (statuses or [NODE_NEED_REVIEW, NODE_FAILED])
        if str(status).strip()
    ]
    if not allowed_statuses:
        allowed_statuses = [NODE_NEED_REVIEW, NODE_FAILED]
    placeholders = ", ".join(["%s"] * len(allowed_statuses))
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    t.id AS task_id,
                    t.task_type,
                    t.task_status,
                    t.payload_json,
                    t.created_at AS task_created_at,
                    t.updated_at AS task_updated_at,
                    n.node_code,
                    n.node_name,
                    n.node_status,
                    n.error_message AS node_error_message,
                    n.updated_at AS node_updated_at,
                    o.output_json
                FROM pipeline_task_node n
                JOIN pipeline_task t ON t.id = n.task_id
                LEFT JOIN pipeline_node_output o
                    ON o.task_id = n.task_id AND o.node_code = n.node_code
                WHERE n.node_status IN ({placeholders})
                ORDER BY n.updated_at DESC
                LIMIT %s
                """,
                (*allowed_statuses, limit),
            )
            rows = cursor.fetchall()

    items = []
    for row in rows:
        items.append(
            {
                "task_id": row["task_id"],
                "task_type": row["task_type"],
                "task_status": row["task_status"],
                "task_payload": _json_loads(row["payload_json"], {}),
                "task_created_at": str(row["task_created_at"]),
                "task_updated_at": str(row["task_updated_at"]),
                "node_code": row["node_code"],
                "node_name": row["node_name"],
                "node_status": row["node_status"],
                "node_error_message": row["node_error_message"],
                "node_updated_at": str(row["node_updated_at"]),
                "output": _json_loads(row["output_json"], {}),
            }
        )
    return {"ok": True, "items": items}


def _set_task_status(
    conn: pymysql.connections.Connection,
    task_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    now = utc_now()
    finished_at = now if status in {TASK_DONE, TASK_FAILED, TASK_CANCELLED, TASK_NEED_REVIEW} else None
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE pipeline_task
            SET task_status = %s,
                error_message = %s,
                updated_at = %s,
                finished_at = COALESCE(%s, finished_at)
            WHERE id = %s
            """,
            (status, error_message, now, finished_at, task_id),
        )


def _set_node_status(
    conn: pymysql.connections.Connection,
    task_id: str,
    node_code: str,
    status: str,
    error_message: str | None = None,
) -> None:
    now = utc_now()
    started_at = now if status == NODE_RUNNING else None
    finished_at = now if status in {NODE_SUCCESS, NODE_FAILED, NODE_NEED_REVIEW, NODE_SKIPPED} else None
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE pipeline_task_node
            SET node_status = %s,
                error_message = %s,
                started_at = COALESCE(%s, started_at),
                finished_at = COALESCE(%s, finished_at),
                updated_at = %s
            WHERE task_id = %s AND node_code = %s
            """,
            (status, error_message, started_at, finished_at, now, task_id, node_code),
        )


def _delete_node_output(conn: pymysql.connections.Connection, task_id: str, node_code: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM pipeline_node_output WHERE task_id = %s AND node_code = %s",
            (task_id, node_code),
        )


def _save_node_output(
    conn: pymysql.connections.Connection,
    task_id: str,
    node_code: str,
    output: dict[str, Any],
) -> None:
    now = utc_now()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO pipeline_node_output
                (id, task_id, node_code, output_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                output_json = VALUES(output_json),
                updated_at = VALUES(updated_at)
            """,
            (
                f"{task_id}:{node_code}",
                task_id,
                node_code,
                _json_dumps(output),
                now,
                now,
            ),
        )


def _load_callable(callable_path: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    module_name, function_name = callable_path.rsplit(".", 1)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        module = importlib.import_module(f"app.{module_name}")
    return getattr(module, function_name)


def _output_for(task: dict[str, Any], node_code: str) -> dict[str, Any]:
    output = task.get("outputs", {}).get(node_code)
    return output if isinstance(output, dict) else {}


def build_node_input(task: dict[str, Any], node_def: NodeDefinition) -> dict[str, Any]:
    payload = dict(task.get("payload") or {})
    payload.update(node_def.payload_patch)
    payload.setdefault("orchestrator_task_id", task["id"])
    payload.setdefault("orchestrator_node_code", node_def.node_code)

    if task["task_type"] == "catfood_image_analysis":
        if node_def.node_code == "ocr_formula":
            image_path_value = payload.get("image_path")
            image_path = Path(str(image_path_value)).expanduser() if image_path_value else None
            return {
                "orchestrator_task_id": task["id"],
                "orchestrator_node_code": node_def.node_code,
                "image_path": str(image_path) if image_path else None,
                "image_dir": str(image_path.parent) if image_path else None,
                "image_glob": image_path.name if image_path else None,
                "image_id": payload.get("image_id"),
                "product_name": payload.get("product_name"),
                "original_filename": payload.get("original_filename"),
                "sha256": payload.get("sha256"),
                "incremental_only": True,
                "move_success_images": False,
            }
        if node_def.node_code == "ingredient_extract":
            ocr_output = _output_for(task, "ocr_formula")
            formula_output = _output_for(task, "formula_input_build")
            return {
                "orchestrator_task_id": task["id"],
                "orchestrator_node_code": node_def.node_code,
                "ocr_text": ocr_output.get("ocr_text"),
                "image_path": payload.get("image_path"),
                "product_name": payload.get("product_name"),
                "source_id": ocr_output.get("source_id"),
                "parsed_row_id": ocr_output.get("parsed_row_id"),
                "ingredient_composition": ocr_output.get("ingredient_composition"),
                "parsed": ocr_output.get("parsed"),
                "brand_id": formula_output.get("brand_id"),
                "product_id": formula_output.get("product_id"),
                "formula_id": formula_output.get("formula_id"),
            }
        if node_def.node_code == "brand_standardize":
            ocr_output = _output_for(task, "ocr_formula")
            return {
                "orchestrator_task_id": task["id"],
                "orchestrator_node_code": node_def.node_code,
                "source_id": ocr_output.get("source_id"),
                "parsed_row_id": ocr_output.get("parsed_row_id"),
                "file_sha256": ocr_output.get("file_sha256"),
                "image_id": payload.get("image_id"),
                "brand_name": payload.get("brand_name"),
            }
        if node_def.node_code == "product_standardize":
            brand_output = _output_for(task, "brand_standardize")
            return {
                "orchestrator_task_id": task["id"],
                "orchestrator_node_code": node_def.node_code,
                "source_id": brand_output.get("source_id"),
                "brand_id": brand_output.get("brand_id"),
            }
        if node_def.node_code == "formula_standardize":
            product_output = _output_for(task, "product_standardize")
            return {
                "orchestrator_task_id": task["id"],
                "orchestrator_node_code": node_def.node_code,
                "source_id": product_output.get("source_id"),
                "brand_id": product_output.get("brand_id"),
                "product_id": product_output.get("product_id"),
            }
        if node_def.node_code == "formula_input_build":
            formula_output = _output_for(task, "formula_standardize")
            return {
                "orchestrator_task_id": task["id"],
                "orchestrator_node_code": node_def.node_code,
                "formula_id": formula_output.get("formula_id"),
            }
        if node_def.node_code == "ingredient_standardize":
            extract_output = _output_for(task, "ingredient_extract")
            formula_input = _output_for(task, "formula_input_build")
            return {
                "orchestrator_task_id": task["id"],
                "orchestrator_node_code": node_def.node_code,
                "source_id": extract_output.get("source_id"),
                "parsed_row_id": extract_output.get("parsed_row_id"),
                "product_key": extract_output.get("product_key"),
                "brand": extract_output.get("brand"),
                "ingredient_composition": extract_output.get("ingredient_composition"),
                "ingredients": extract_output.get("ingredients"),
                "product_name": extract_output.get("product_name") or payload.get("product_name"),
                "brand_id": formula_input.get("brand_id"),
                "product_id": formula_input.get("product_id"),
                "formula_id": formula_input.get("formula_id"),
            }
        if node_def.node_code == "formula_profile":
            standardize_output = _output_for(task, "ingredient_standardize")
            formula_input = _output_for(task, "formula_input_build")
            return {
                "orchestrator_task_id": task["id"],
                "orchestrator_node_code": node_def.node_code,
                "source_id": standardize_output.get("source_id"),
                "product_key": standardize_output.get("product_key"),
                "brand": standardize_output.get("brand"),
                "standardized_ingredients": standardize_output,
                "product_name": standardize_output.get("product_name") or payload.get("product_name"),
                "brand_id": formula_input.get("brand_id"),
                "product_id": formula_input.get("product_id"),
                "formula_id": formula_input.get("formula_id"),
            }

    return payload


def _node_map(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["node_code"]: node for node in task["nodes"]}


def _downstream_node_codes(task_type: str, node_code: str) -> set[str]:
    definitions = get_pipeline_definition(task_type)
    dependents: dict[str, list[str]] = {}
    for node_def in definitions:
        for dep_code in node_def.depends_on:
            dependents.setdefault(dep_code, []).append(node_def.node_code)

    downstream: set[str] = set()
    stack = list(dependents.get(node_code, []))
    while stack:
        current = stack.pop()
        if current in downstream:
            continue
        downstream.add(current)
        stack.extend(dependents.get(current, []))
    return downstream


def _refresh_ready_nodes(task: dict[str, Any]) -> bool:
    changed = False
    payload = task["payload"]
    nodes_by_code = _node_map(task)
    definitions = get_pipeline_definition(task["task_type"])
    with _connect() as conn:
        for node_def in sorted(definitions, key=lambda item: item.priority):
            node = nodes_by_code.get(node_def.node_code)
            if not node or node["node_status"] not in {NODE_PENDING, NODE_BLOCKED}:
                continue

            missing_fields = [
                field_name
                for field_name in node_def.required_task_fields
                if payload.get(field_name) in (None, "", [])
            ]
            if missing_fields:
                if node_def.skippable:
                    _set_node_status(conn, task["id"], node_def.node_code, NODE_SKIPPED, "缺少可选输入，节点跳过")
                else:
                    _set_node_status(
                        conn,
                        task["id"],
                        node_def.node_code,
                        NODE_BLOCKED,
                        f"缺少任务输入字段: {', '.join(missing_fields)}",
                    )
                changed = True
                continue

            dependencies_ready = True
            for dep_code in node_def.depends_on:
                dep_node = nodes_by_code.get(dep_code)
                if not dep_node or dep_node["node_status"] != NODE_SUCCESS:
                    dependencies_ready = False
                    break
            if not dependencies_ready:
                continue

            _set_node_status(conn, task["id"], node_def.node_code, NODE_READY)
            changed = True
        conn.commit()
    return changed


def _call_node(task: dict[str, Any], node_def: NodeDefinition) -> dict[str, Any]:
    if not node_def.callable_path:
        raise ValueError(f"节点未配置 callable_path: {node_def.node_code}")
    payload = build_node_input(task, node_def)
    func = _load_callable(node_def.callable_path)
    result = func(payload)
    if not isinstance(result, dict):
        return {"ok": True, "result": result}
    return result


def _execute_ready_node(task: dict[str, Any], node_def: NodeDefinition) -> None:
    with _connect() as conn:
        _set_task_status(conn, task["id"], TASK_PROCESSING)
        _set_node_status(conn, task["id"], node_def.node_code, NODE_RUNNING)
        conn.commit()

    try:
        output = _call_node(task, node_def)
        ok = bool(output.get("ok", True))
    except Exception as exc:
        with _connect() as conn:
            _set_node_status(conn, task["id"], node_def.node_code, NODE_FAILED, str(exc))
            _set_task_status(conn, task["id"], TASK_FAILED, str(exc))
            conn.commit()
        return

    with _connect() as conn:
        _save_node_output(conn, task["id"], node_def.node_code, output)
        check = output_check(node_def.node_code, output, task["payload"])
        if ok and check.status == NODE_SUCCESS:
            _set_node_status(conn, task["id"], node_def.node_code, NODE_SUCCESS)
        elif ok and check.status == NODE_NEED_REVIEW:
            _set_node_status(conn, task["id"], node_def.node_code, NODE_NEED_REVIEW, check.reason)
            _set_task_status(conn, task["id"], TASK_NEED_REVIEW, check.reason)
        elif ok and check.status == NODE_WAITING_RESULT:
            _set_node_status(conn, task["id"], node_def.node_code, NODE_WAITING_RESULT, check.reason)
        else:
            _set_node_status(
                conn,
                task["id"],
                node_def.node_code,
                NODE_FAILED,
                str(output.get("error") or check.reason or "节点执行失败"),
            )
            _set_task_status(conn, task["id"], TASK_FAILED, str(output.get("error") or check.reason or "节点执行失败"))
        conn.commit()


def _finalize_task_if_possible(task_id: str) -> None:
    task = get_task(task_id)
    if not task:
        return
    statuses = [node["node_status"] for node in task["nodes"]]
    if any(status == NODE_FAILED for status in statuses):
        return
    if any(status == NODE_NEED_REVIEW for status in statuses):
        with _connect() as conn:
            _set_task_status(conn, task_id, TASK_NEED_REVIEW, "存在需要人工复核的节点")
            conn.commit()
        return
    with _connect() as conn:
        if all(status in {NODE_SUCCESS, NODE_SKIPPED} for status in statuses):
            _set_task_status(conn, task_id, TASK_DONE)
        elif any(status == NODE_BLOCKED for status in statuses):
            _set_task_status(conn, task_id, TASK_WAITING_INPUT)
        conn.commit()


def run_task(task_id: str, *, max_steps: int = 100) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError(f"task_id 不存在: {task_id}")
    if task["task_status"] in {TASK_DONE, TASK_FAILED, TASK_CANCELLED, TASK_NEED_REVIEW}:
        return task

    steps = 0
    while steps < max_steps:
        task = get_task(task_id)
        if not task or task["task_status"] in {TASK_DONE, TASK_FAILED, TASK_CANCELLED, TASK_NEED_REVIEW}:
            break
        _refresh_ready_nodes(task)
        task = get_task(task_id)
        if not task:
            break
        ready_nodes = [
            node
            for node in task["nodes"]
            if node["node_status"] == NODE_READY
        ]
        if not ready_nodes:
            break
        definitions = {node.node_code: node for node in get_pipeline_definition(task["task_type"])}
        ready_nodes.sort(key=lambda item: item["priority"])
        _execute_ready_node(task, definitions[ready_nodes[0]["node_code"]])
        steps += 1

    _finalize_task_if_possible(task_id)
    return get_task(task_id) or {"id": task_id}


def _set_if_missing(output: dict[str, Any], key: str, value: Any) -> None:
    if not _is_non_empty(output.get(key)) and _is_non_empty(value):
        output[key] = value


def _unwrap_node_output_envelope(output: dict[str, Any]) -> dict[str, Any]:
    """Flatten common API envelopes before saving and validating node output."""
    if not isinstance(output, dict):
        return {}

    for envelope_key in ("data", "result", "item"):
        nested = output.get(envelope_key)
        if not isinstance(nested, dict):
            continue
        if not nested:
            continue
        flattened = dict(nested)
        for key, value in output.items():
            if key == envelope_key:
                continue
            if key == "ok" or not _is_non_empty(flattened.get(key)):
                flattened[key] = value
        return flattened

    return dict(output)


def _merge_identity_fields(output: dict[str, Any], *sources: dict[str, Any]) -> None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in (
            "source_id",
            "parsed_row_id",
            "product_key",
            "brand_id",
            "product_id",
            "formula_id",
            "brand",
            "product_name",
            "ingredient_composition",
            "ingredients",
        ):
            _set_if_missing(output, key, source.get(key))
        parsed = source.get("parsed")
        if isinstance(parsed, dict):
            _set_if_missing(output, "parsed_row_id", parsed.get("id"))
            _set_if_missing(output, "source_id", parsed.get("source_id"))
            _set_if_missing(output, "brand", parsed.get("brand"))
            _set_if_missing(output, "product_name", parsed.get("product_name"))
            _set_if_missing(output, "ingredient_composition", parsed.get("ingredient_composition"))
            if not isinstance(output.get("parsed"), dict):
                output["parsed"] = parsed


def _augment_node_output(task: dict[str, Any], node_code: str, output: dict[str, Any]) -> dict[str, Any]:
    output = dict(output or {})
    if task.get("task_type") != "catfood_image_analysis":
        return output

    ocr_output = _output_for(task, "ocr_formula")
    extract_output = _output_for(task, "ingredient_extract")
    standardize_output = _output_for(task, "ingredient_standardize")

    if node_code == "ingredient_extract":
        _merge_identity_fields(output, ocr_output)
    elif node_code == "ingredient_standardize":
        _merge_identity_fields(output, extract_output, ocr_output)
    elif node_code == "formula_profile":
        _merge_identity_fields(output, standardize_output, extract_output, ocr_output)
        nested = output.get("standardized_ingredients")
        if isinstance(nested, dict):
            _merge_identity_fields(output, nested)

    return output


def apply_node_result(
    task_id: str,
    node_code: str,
    *,
    call_status: str,
    output: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError(f"task_id 不存在: {task_id}")
    nodes_by_code = _node_map(task)
    if node_code not in nodes_by_code:
        raise ValueError(f"节点不存在: {task_id}/{node_code}")

    output = _unwrap_node_output_envelope(dict(output or {}))
    output = _augment_node_output(task, node_code, output)
    normalized_status = str(call_status or "").strip().lower()
    with _connect() as conn:
        if output:
            _save_node_output(conn, task_id, node_code, output)

        if normalized_status in FAILURE_CALL_STATUSES:
            reason = (
                error_message
                or output.get("error_message")
                or output.get("error")
                or output.get("message")
                or "节点调用失败"
            )
            reason = str(reason)
            _set_node_status(conn, task_id, node_code, NODE_FAILED, reason)
            _set_task_status(conn, task_id, TASK_FAILED, reason)
            conn.commit()
            return get_task(task_id) or {"id": task_id}

        if normalized_status in WAITING_CALL_STATUSES:
            _set_node_status(conn, task_id, node_code, NODE_WAITING_RESULT, error_message or "等待节点结果")
            _set_task_status(conn, task_id, TASK_PROCESSING)
            conn.commit()
            return get_task(task_id) or {"id": task_id}

        if normalized_status not in SUCCESS_CALL_STATUSES:
            raise ValueError(f"unsupported call_status: {call_status}")

        check = output_check(node_code, output, task["payload"])
        _set_node_status(conn, task_id, node_code, check.status, None if check.passed else check.reason)
        if check.status == NODE_SUCCESS:
            _set_task_status(conn, task_id, TASK_PROCESSING)
        elif check.status == NODE_NEED_REVIEW:
            _set_task_status(conn, task_id, TASK_NEED_REVIEW, check.reason)
        elif check.status == NODE_WAITING_RESULT:
            _set_task_status(conn, task_id, TASK_PROCESSING)
        else:
            _set_task_status(conn, task_id, TASK_FAILED, check.reason)
        conn.commit()

    refreshed = get_task(task_id)
    if refreshed and refreshed["task_status"] not in {TASK_DONE, TASK_FAILED, TASK_CANCELLED, TASK_NEED_REVIEW}:
        _refresh_ready_nodes(refreshed)
    _finalize_task_if_possible(task_id)
    return get_task(task_id) or {"id": task_id}


def cancel_task(task_id: str, reason: str | None = None) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError(f"task_id 不存在: {task_id}")
    with _connect() as conn:
        _set_task_status(conn, task_id, TASK_CANCELLED, reason or "人工作废")
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pipeline_task_node
                SET node_status = %s,
                    error_message = COALESCE(%s, error_message),
                    updated_at = %s
                WHERE task_id = %s
                  AND node_status NOT IN (%s, %s)
                """,
                (NODE_SKIPPED, reason or "人工作废", utc_now(), task_id, NODE_SUCCESS, NODE_FAILED),
            )
        conn.commit()
    return get_task(task_id) or {"id": task_id}


def apply_manual_ocr_text(task_id: str, ocr_text: str, reviewer: str | None = None) -> dict[str, Any]:
    text = str(ocr_text or "").strip()
    if not text:
        raise ValueError("OCR 文本不能为空")
    output = {
        "ocr_text": text,
        "manual_review": True,
        "reviewer": reviewer,
        "reviewed_at": utc_now(),
    }
    return apply_node_result(task_id, "ocr_formula", call_status="success", output=output)


def reset_node_for_reextract(task_id: str, node_code: str, reason: str | None = None) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError(f"task_id 不存在: {task_id}")
    definitions = {node.node_code: node for node in get_pipeline_definition(task["task_type"])}
    if node_code not in definitions:
        raise ValueError(f"节点不存在: {task_id}/{node_code}")

    if task["task_type"] == "catfood_image_analysis" and node_code == "ingredient_extract":
        ocr_output = _output_for(task, "ocr_formula")
        extract_output = _output_for(task, "ingredient_extract")
        source_id = ocr_output.get("source_id") or extract_output.get("source_id")
        parsed_row_id = ocr_output.get("parsed_row_id") or extract_output.get("parsed_row_id")
        product_key = extract_output.get("product_key") or ocr_output.get("product_key")
        parsed_check = _check_parsed_ingredient_ready(
            source_id=source_id,
            parsed_row_id=parsed_row_id,
            product_key=product_key,
            output=ocr_output,
        )
        if parsed_check:
            raise ValueError(f"{parsed_check.reason}，请先重跑配料表 OCR 识别")

    reset_codes = {node_code, *_downstream_node_codes(task["task_type"], node_code)}
    nodes_by_code = _node_map(task)
    target_def = definitions[node_code]
    ready = True
    for dep_code in target_def.depends_on:
        dep_node = nodes_by_code.get(dep_code)
        if not dep_node or dep_node["node_status"] != NODE_SUCCESS:
            ready = False
            break

    with _connect() as conn:
        for reset_code in reset_codes:
            _delete_node_output(conn, task_id, reset_code)
            _set_node_status(
                conn,
                task_id,
                reset_code,
                NODE_READY if reset_code == node_code and ready else NODE_PENDING,
                reason or "人工触发重新抽取",
            )
        _set_task_status(conn, task_id, TASK_PROCESSING, None)
        conn.commit()
    return get_task(task_id) or {"id": task_id}


def _api_route_from_override(value: Any) -> ApiRoute | None:
    if not isinstance(value, dict):
        return None
    url = str(value.get("url") or "").strip()
    if not url:
        return None
    return ApiRoute(
        method=str(value.get("method") or "POST").upper(),
        url=url,
        timeout_seconds=int(value.get("timeout_seconds") or 30),
    )


def claim_ready_dispatch_jobs(
    *,
    limit: int = 10,
    task_id: str | None = None,
    task_type: str | None = None,
    node_codes: list[str] | None = None,
    api_overrides: dict[str, Any] | None = None,
    claim: bool = True,
) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 10), 100))
    allowed_node_codes = {str(item).strip() for item in (node_codes or []) if str(item).strip()}
    api_overrides = dict(api_overrides or {})

    if task_id:
        rows = [{"id": task_id}]
    else:
        with _connect() as conn:
            with conn.cursor() as cursor:
                if task_type:
                    cursor.execute(
                        """
                        SELECT id FROM pipeline_task
                        WHERE task_status IN (%s, %s, %s)
                          AND task_type = %s
                        ORDER BY updated_at ASC
                        LIMIT %s
                        """,
                        (TASK_CREATED, TASK_PROCESSING, TASK_WAITING_INPUT, task_type, limit * 5),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id FROM pipeline_task
                        WHERE task_status IN (%s, %s, %s)
                        ORDER BY updated_at ASC
                        LIMIT %s
                        """,
                        (TASK_CREATED, TASK_PROCESSING, TASK_WAITING_INPUT, limit * 5),
                    )
                rows = cursor.fetchall()

    jobs: list[dict[str, Any]] = []
    for row in rows:
        if len(jobs) >= limit:
            break
        task = get_task(row["id"])
        if not task or task["task_status"] in {TASK_DONE, TASK_FAILED, TASK_CANCELLED, TASK_NEED_REVIEW}:
            continue
        _refresh_ready_nodes(task)
        task = get_task(row["id"])
        if not task:
            continue

        definitions = {node.node_code: node for node in get_pipeline_definition(task["task_type"])}
        ready_nodes = [
            node
            for node in task["nodes"]
            if node["node_status"] == NODE_READY
            and (not allowed_node_codes or node["node_code"] in allowed_node_codes)
        ]
        ready_nodes.sort(key=lambda item: item["priority"])

        for node in ready_nodes:
            if len(jobs) >= limit:
                break
            node_def = definitions[node["node_code"]]
            override = api_overrides.get(node_def.node_code) or api_overrides.get(f"{task['task_type']}.{node_def.node_code}")
            api_route = _api_route_from_override(override) or node_def.api
            job = {
                "task_id": task["id"],
                "task_type": task["task_type"],
                "node_code": node_def.node_code,
                "node_name": node_def.node_name,
                "api": (
                    {
                        "method": api_route.method,
                        "url": api_route.url,
                        "timeout_seconds": api_route.timeout_seconds,
                    }
                    if api_route
                    else None
                ),
                "input": build_node_input(task, node_def),
            }
            if not api_route:
                job["dispatch_status"] = "api_not_configured"
                jobs.append(job)
                continue
            if claim:
                with _connect() as conn:
                    _set_task_status(conn, task["id"], TASK_PROCESSING)
                    _set_node_status(conn, task["id"], node_def.node_code, NODE_RUNNING)
                    conn.commit()
            job["dispatch_status"] = "claimed" if claim else "ready"
            jobs.append(job)

    return {"ok": True, "jobs": jobs}


def dispatch_scan(limit: int = 20, task_type: str | None = None) -> dict[str, Any]:
    init_db()
    limit = max(1, min(int(limit or 20), 200))
    with _connect() as conn:
        with conn.cursor() as cursor:
            if task_type:
                cursor.execute(
                    """
                    SELECT id FROM pipeline_task
                    WHERE task_status IN (%s, %s, %s) AND task_type = %s
                    ORDER BY updated_at ASC
                    LIMIT %s
                    """,
                    (TASK_CREATED, TASK_PROCESSING, TASK_WAITING_INPUT, task_type, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT id FROM pipeline_task
                    WHERE task_status IN (%s, %s, %s)
                    ORDER BY updated_at ASC
                    LIMIT %s
                    """,
                    (TASK_CREATED, TASK_PROCESSING, TASK_WAITING_INPUT, limit),
                )
            rows = cursor.fetchall()

    results = []
    for row in rows:
        results.append(run_task(row["id"]))
    return {"ok": True, "scanned": len(rows), "tasks": results}
