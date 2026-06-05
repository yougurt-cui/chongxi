"""Product function positioning service."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from adapters.product_function_adapter import fetch_product_function_source


RISK_SCORE_MAP = {
    "低": 90,
    "中低": 75,
    "中": 55,
    "中高": 35,
    "高": 15,
    None: 50,
    "": 50,
    "暂无": 50,
}

SCORE_SCALE_MAX = {
    "protein_quality_score": 1.0,
    "protein_structure_score": 1.0,
    "protein_score": 1.0,
    "fat_score": 1.0,
    "fat_regulation_score": 1.0,
    "fat_oily_score": 1.0,
    "omega_imbalance_score": 1.0,
    "fat_mix_complexity_score": 1.0,
    "antioxidant_score": 1.0,
    "starch_burden_score": 5.0,
    "carb_score": 5.0,
    "fiber_score": 5.0,
    "p_form_score": 5.0,
    "p_bulk_score": 5.0,
    "p_buffer": 5.0,
    "p_total_score": 5.0,
    "prebiotic_score": 5.0,
    "q_feed": 5.0,
    "q_scfa": 5.0,
    "q_total_score": 5.0,
}


def clamp(value: float, min_value: float = 0, max_value: float = 100) -> float:
    return max(min_value, min(max_value, value))


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() == "nan"


def normalize_score(value: Any, field: str | None = None) -> float | None:
    if _is_missing(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    scale_max = SCORE_SCALE_MAX.get(field or "")
    if scale_max:
        return round(clamp(numeric / scale_max * 100), 2)
    if 0 <= numeric <= 1:
        return round(numeric * 100, 2)
    return round(clamp(numeric), 2)


def get_score(row: dict[str, Any], field: str, default: float = 50) -> float:
    value = row.get(field, default)
    if _is_missing(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_risk_reverse_score(row: dict[str, Any], field: str) -> float:
    value = row.get(field)
    if _is_missing(value):
        value = None
    return RISK_SCORE_MAP.get(value, 50)


def reverse_score(score: float) -> float:
    return 100 - score


def weighted_score(items: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in items)
    if total_weight == 0:
        return 0
    score = sum(score * weight for score, weight in items) / total_weight
    return round(clamp(score), 2)


def weighted_avg_valid(items: list[tuple[float | None, float]], default: float = 50) -> float:
    valid = [
        (float(score), float(weight))
        for score, weight in items
        if score is not None and not _is_missing(score) and weight > 0
    ]
    if not valid:
        return default
    total_weight = sum(weight for _, weight in valid)
    return round(clamp(sum(score * weight for score, weight in valid) / total_weight), 2)


def calc_function_scores(row: dict[str, Any]) -> dict[str, float]:
    protein_quality = get_score(row, "protein_quality_score")
    protein_pressure = get_score(row, "protein_pressure_score")
    carb_burden = get_score(row, "carb_burden_score")
    fat_burden = get_score(row, "fat_burden_score")
    fiber_buffer = get_score(row, "fiber_buffer_score")
    microbiome_support = get_score(row, "microbiome_support_score")
    skin_protection = get_score(row, "skin_protection_score")
    omega3_support = get_score(row, "omega3_support_score")
    black_chin_friendly = get_score(row, "black_chin_friendly_score")
    calorie_density = get_score(row, "calorie_density_score")

    soft_stool_risk_reverse = get_risk_reverse_score(row, "soft_stool_risk_level")
    black_chin_risk_reverse = get_risk_reverse_score(row, "black_chin_risk_level")

    gut_friendly_score = weighted_score([
        (soft_stool_risk_reverse, 0.30),
        (fiber_buffer, 0.25),
        (microbiome_support, 0.20),
        (reverse_score(protein_pressure), 0.15),
        (reverse_score(carb_burden), 0.10),
    ])

    black_chin_friendly_score = weighted_score([
        (black_chin_risk_reverse, 0.35),
        (reverse_score(fat_burden), 0.25),
        (skin_protection, 0.20),
        (omega3_support, 0.10),
        (black_chin_friendly, 0.10),
    ])

    muscle_gain_score = weighted_score([
        (protein_quality, 0.35),
        (reverse_score(protein_pressure), 0.25),
        (reverse_score(carb_burden), 0.10),
        (100 - abs(fat_burden - 50), 0.15),
        (microbiome_support, 0.15),
    ])

    weight_control_score = weighted_score([
        (reverse_score(fat_burden), 0.30),
        (reverse_score(calorie_density), 0.25),
        (reverse_score(carb_burden), 0.15),
        (fiber_buffer, 0.20),
        (protein_quality, 0.10),
    ])

    skin_coat_score = weighted_score([
        (skin_protection, 0.35),
        (omega3_support, 0.25),
        (reverse_score(fat_burden), 0.15),
        (black_chin_risk_reverse, 0.15),
        (protein_quality, 0.10),
    ])

    return {
        "肠胃友好": gut_friendly_score,
        "黑下巴友好": black_chin_friendly_score,
        "增肌长肉": muscle_gain_score,
        "控重管理": weight_control_score,
        "皮肤毛发": skin_coat_score,
    }


def build_evidence(row: dict[str, Any], tag: str, score: float) -> list[str]:
    evidence: list[str] = []

    protein_quality = get_score(row, "protein_quality_score")
    protein_pressure = get_score(row, "protein_pressure_score")
    carb_burden = get_score(row, "carb_burden_score")
    fat_burden = get_score(row, "fat_burden_score")
    fiber_buffer = get_score(row, "fiber_buffer_score")
    microbiome_support = get_score(row, "microbiome_support_score")
    skin_protection = get_score(row, "skin_protection_score")
    omega3_support = get_score(row, "omega3_support_score")

    soft_stool_risk = row.get("soft_stool_risk_level")
    black_chin_risk = row.get("black_chin_risk_level")

    if tag == "肠胃友好":
        if soft_stool_risk in ["低", "中低"]:
            evidence.append(f"软便风险为{soft_stool_risk}")
        if fiber_buffer >= 60:
            evidence.append("纤维缓冲较好")
        if microbiome_support >= 60:
            evidence.append("菌群支持较好")
        if protein_pressure <= 45:
            evidence.append("蛋白消化压力不高")
        if carb_burden <= 45:
            evidence.append("碳水负担不高")
    elif tag == "黑下巴友好":
        if black_chin_risk in ["低", "中低"]:
            evidence.append(f"黑下巴风险为{black_chin_risk}")
        if fat_burden <= 45:
            evidence.append("脂肪负担不高")
        if skin_protection >= 50:
            evidence.append("皮肤保护支持尚可")
        if omega3_support >= 50:
            evidence.append("Omega-3 支持尚可")
    elif tag == "增肌长肉":
        if protein_quality >= 70:
            evidence.append("蛋白质量较高")
        if protein_pressure <= 45:
            evidence.append("蛋白压力较低")
        if carb_burden <= 50:
            evidence.append("碳水负担不高")
        if 35 <= fat_burden <= 65:
            evidence.append("脂肪/能量支持处于可接受区间")
    elif tag == "控重管理":
        if fat_burden <= 45:
            evidence.append("脂肪负担较低")
        if carb_burden <= 45:
            evidence.append("碳水负担较低")
        if fiber_buffer >= 60:
            evidence.append("纤维缓冲较好")
        if protein_quality >= 65:
            evidence.append("蛋白质量有一定支持")
    elif tag == "皮肤毛发":
        if skin_protection >= 60:
            evidence.append("皮肤保护支持较好")
        if omega3_support >= 60:
            evidence.append("Omega-3 支持较好")
        if protein_quality >= 65:
            evidence.append("蛋白质量有一定支持")
        if black_chin_risk in ["低", "中低"]:
            evidence.append(f"黑下巴风险为{black_chin_risk}")

    if not evidence:
        evidence.append(f"{tag}综合得分为{score}")

    return evidence


def build_warning_tags(row: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []

    soft_stool_risk = row.get("soft_stool_risk_level")
    black_chin_risk = row.get("black_chin_risk_level")
    protein_pressure = get_score(row, "protein_pressure_score")
    fat_burden = get_score(row, "fat_burden_score")
    carb_burden = get_score(row, "carb_burden_score")
    fiber_buffer = get_score(row, "fiber_buffer_score")

    if soft_stool_risk in ["中高", "高"]:
        warnings.append({
            "tag": "肠胃敏感猫慎选",
            "level": "strong" if soft_stool_risk == "高" else "medium",
            "evidence": [f"软便风险为{soft_stool_risk}"],
        })

    if black_chin_risk in ["中高", "高"] or fat_burden >= 65:
        evidence = []
        if black_chin_risk in ["中高", "高"]:
            evidence.append(f"黑下巴风险为{black_chin_risk}")
        if fat_burden >= 65:
            evidence.append("脂肪负担偏高")
        warnings.append({
            "tag": "黑下巴猫慎选",
            "level": "strong" if black_chin_risk == "高" or fat_burden >= 75 else "medium",
            "evidence": evidence,
        })

    if fat_burden >= 70 or carb_burden >= 70:
        evidence = []
        if fat_burden >= 70:
            evidence.append("脂肪负担偏高")
        if carb_burden >= 70:
            evidence.append("碳水负担偏高")
        warnings.append({"tag": "肥胖猫慎选", "level": "medium", "evidence": evidence})

    if protein_pressure >= 65:
        warnings.append({"tag": "消化压力偏高", "level": "medium", "evidence": ["蛋白压力偏高"]})

    if fiber_buffer < 25 and soft_stool_risk in ["中", "中高", "高"]:
        warnings.append({
            "tag": "便便成形支持偏弱",
            "level": "medium",
            "evidence": ["纤维缓冲较弱", f"软便风险为{soft_stool_risk}"],
        })

    return warnings


def apply_conflict_rules(row: dict[str, Any], function_tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    soft_stool_risk = row.get("soft_stool_risk_level")
    black_chin_risk = row.get("black_chin_risk_level")
    protein_pressure = get_score(row, "protein_pressure_score")
    fat_burden = get_score(row, "fat_burden_score")

    filtered: list[dict[str, Any]] = []
    for item in function_tags:
        tag = item["tag"]

        if tag == "肠胃友好" and soft_stool_risk in ["中高", "高"]:
            continue
        if tag == "黑下巴友好" and black_chin_risk in ["中高", "高"]:
            continue
        if tag == "增肌长肉" and protein_pressure >= 70:
            item["tag"] = "高蛋白观察型"
            item["evidence"].append("蛋白压力偏高，不建议直接标为增肌友好")
        if tag == "控重管理" and fat_burden >= 65:
            continue
        if tag == "皮肤毛发" and black_chin_risk == "高":
            item["tag"] = "皮肤毛发观察型"
            item["evidence"].append("黑下巴风险高，不建议直接标为皮肤友好")

        filtered.append(item)

    return filtered


def score_to_level(score: float) -> str:
    if score >= 75:
        return "strong"
    if score >= 60:
        return "weak"
    return "none"


def level_to_text(tag: str, level: str) -> str:
    if level == "strong":
        return tag
    weak_map = {
        "肠胃友好": "肠胃相对友好",
        "黑下巴友好": "黑下巴相对友好",
        "增肌长肉": "偏长肉",
        "控重管理": "控重观察型",
        "皮肤毛发": "皮肤毛发支持",
    }
    return weak_map.get(tag, tag)


def generate_display_text(
    base_positioning: str,
    function_tags: list[dict[str, Any]],
    warning_tags: list[dict[str, Any]],
) -> str:
    parts = []
    if base_positioning:
        parts.append(base_positioning)
    if function_tags:
        parts.append(function_tags[0]["display_tag"])
    if warning_tags:
        parts.append(warning_tags[0]["tag"])
    return "｜".join(parts)


def infer_function_positioning(row: dict[str, Any]) -> dict[str, Any]:
    base_positioning = row.get("base_positioning", "日常口粮")
    function_scores = calc_function_scores(row)

    function_tags = []
    for tag, score in function_scores.items():
        level = score_to_level(score)
        if level == "none":
            continue
        function_tags.append({
            "tag": tag,
            "display_tag": level_to_text(tag, level),
            "score": score,
            "level": level,
            "confidence": round(score / 100, 2),
            "evidence": build_evidence(row, tag, score),
        })

    function_tags = sorted(function_tags, key=lambda x: x["score"], reverse=True)[:2]
    warning_tags = build_warning_tags(row)
    function_tags = apply_conflict_rules(row, function_tags)
    display_text = generate_display_text(base_positioning, function_tags, warning_tags)

    return {
        "product_name": row.get("product_name"),
        "brand": row.get("brand"),
        "product_key": row.get("product_key"),
        "source_id": row.get("source_id"),
        "base_positioning": base_positioning,
        "function_scores": function_scores,
        "function_tags": function_tags,
        "warning_tags": warning_tags,
        "display_text": display_text,
    }


def _first_score(*items: tuple[dict[str, Any], str]) -> float | None:
    for source, field in items:
        score = normalize_score(source.get(field), field)
        if score is not None:
            return score
    return None


def _build_calculation_row(source: dict[str, Any]) -> dict[str, Any]:
    wide = source.get("score_wide") or {}
    sku = source.get("sku_feature") or {}
    black_risk = source.get("black_chin_risk") or {}
    soft_risk = source.get("soft_stool_risk") or {}

    fat_burden = _first_score((sku, "fat_score"), (wide, "fat_score"))
    q_feed = _first_score((sku, "q_feed"), (wide, "q_feed"))
    q_scfa = _first_score((sku, "q_scfa"), (wide, "q_scfa"))

    return {
        "source_id": wide.get("source_id"),
        "product_key": wide.get("product_key") or sku.get("sku_id"),
        "brand": wide.get("brand") or sku.get("brand_name"),
        "product_name": wide.get("product_name") or sku.get("sku_name"),
        "base_positioning": "日常口粮",
        "protein_quality_score": _first_score((wide, "protein_quality_score")),
        "protein_pressure_score": _first_score((sku, "protein_score"), (wide, "protein_structure_score")),
        "carb_burden_score": _first_score((sku, "carb_score"), (wide, "starch_burden_score")),
        "fat_burden_score": fat_burden,
        "fiber_buffer_score": weighted_avg_valid([
            (_first_score((sku, "fiber_score"), (wide, "p_total_score")), 0.30),
            (_first_score((wide, "p_total_score")), 0.25),
            (_first_score((sku, "p_buffer"), (wide, "p_buffer")), 0.45),
        ]),
        "microbiome_support_score": weighted_avg_valid([
            (_first_score((sku, "prebiotic_score"), (wide, "q_feed")), 0.35),
            (q_scfa, 0.45),
            (q_feed, 0.20),
        ]),
        "skin_protection_score": weighted_avg_valid([
            (_first_score((sku, "antioxidant_score"), (wide, "fat_regulation_score")), 0.55),
            (_first_score((wide, "fat_regulation_score")), 0.45),
        ]),
        "omega3_support_score": (
            reverse_score(_first_score((wide, "omega_imbalance_score")) or 50)
        ),
        "black_chin_friendly_score": _first_score((wide, "fat_regulation_score"), (sku, "antioxidant_score")),
        "calorie_density_score": fat_burden,
        "black_chin_risk_level": black_risk.get("current_pool_risk_level") or "暂无",
        "soft_stool_risk_level": soft_risk.get("current_pool_risk_level") or "暂无",
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def get_product_function_positioning(payload: dict[str, Any]) -> dict[str, Any]:
    source = fetch_product_function_source(
        source_id=payload.get("source_id"),
        product_key=payload.get("product_key") or payload.get("sku_id"),
        brand=payload.get("brand") or payload.get("brand_name"),
        product_name=payload.get("product_name") or payload.get("sku_name"),
    )
    if not source:
        return {"ok": False, "error": "未找到产品评分记录"}

    calculation_row = _build_calculation_row(source)
    result = infer_function_positioning(calculation_row)
    response = {
        "ok": True,
        "item": result,
        "input_scores": calculation_row,
        "tables": {
            "score_wide": "catfood_protein_fat_fiber_score_wide",
            "sku_feature": "sku_feature_input",
            "risk_result": "sku_risk_score_result",
        },
    }
    if bool(payload.get("include_raw")):
        response["raw"] = source
    return _json_safe(response)


def batch_product_function_positioning(payload: dict[str, Any]) -> dict[str, Any]:
    products = payload.get("products") or payload.get("items") or []
    if not isinstance(products, list):
        raise ValueError("products 必须是数组")
    limit = max(1, min(int(payload.get("limit") or len(products) or 1), 100))
    items = []
    for product in products[:limit]:
        if not isinstance(product, dict):
            items.append({"ok": False, "error": "产品查询项必须是对象", "query": product})
            continue
        query = {**product, "include_raw": bool(payload.get("include_raw"))}
        items.append(get_product_function_positioning(query))
    return {"ok": True, "items": items}
