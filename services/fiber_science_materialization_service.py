"""Build formula-level fiber/starch materializations from ingredient science profiles."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Iterable


SCIENCE_PROFILE_VERSION = "science-v1"
RELEVANT_CATEGORIES = {"carbohydrate", "fiber", "prebiotic"}
RELEVANT_ROLES = {"碳水供给", "膳食纤维支持", "益生元支持"}

STARCH_CATEGORY_MAP = {
    "legume": ("豆类碳水来源", 1.2),
    "grain": ("谷物淀粉来源", 1.3),
    "tuber": ("薯类淀粉来源", 1.5),
    "flour": ("高淀粉粉类", 1.8),
    "refined_starch": ("精制淀粉/纯淀粉", 2.0),
    "available_sugar": ("非淀粉可利用碳水", 2.0),
}
SOLUBILITY_LABELS = {"mixed": "混合", "soluble": "可溶", "insoluble": "不可溶"}
FERMENTABILITY_LABELS = {"low": "低", "medium_low": "中低", "medium": "中", "high": "高"}
FIBER_FUNCTION_LABELS = {
    "forming": "吸水成形",
    "bulk": "增加粪便骨架",
    "buffer": "缓冲刺激",
    "gel_forming": "胶质成形",
}
PREBIOTIC_FUNCTION_LABELS = {"microbiome_feed": "供菌", "scfa_support": "SCFA支持"}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _position_weight(position: Any) -> float:
    try:
        rank = int(position)
    except (TypeError, ValueError):
        return 0.2
    if rank == 1:
        return 1.2
    if rank <= 3:
        return 1.0
    if rank <= 5:
        return 0.8
    if rank <= 8:
        return 0.6
    if rank <= 12:
        return 0.4
    return 0.2


def build_science_payload(
    items: Iterable[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    details: dict[str, dict[str, Any]] = {}
    subtype_tags: dict[str, list[str]] = {}
    starch_items: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    used_ids: list[str] = []
    eligible_count = 0

    for item in items:
        if item.get("is_ignored"):
            continue
        standard_id = str(item.get("standard_ingredient_id") or "")
        profile = profiles.get(standard_id)
        profile_category = str((profile or {}).get("nutrition_category") or "")
        role = str(item.get("primary_nutrition_role") or "")
        relevant = profile_category in RELEVANT_CATEGORIES or role in RELEVANT_ROLES
        if not relevant:
            continue
        eligible_count += 1
        if not profile or profile.get("science_status") != "active":
            missing.append(
                {
                    "standard_ingredient_id": standard_id or None,
                    "name": item.get("standard_name") or item.get("raw_name"),
                    "reason": "missing_active_science_profile",
                }
            )
            continue

        used_ids.append(standard_id)
        attributes = _json_object(profile.get("domain_attributes_json"))
        name = str(item.get("standard_name") or item.get("raw_name") or standard_id)
        category = profile_category
        if category in {"fiber", "prebiotic"}:
            category_label = "益生元" if category == "prebiotic" else "膳食纤维"
            detail = {
                "ingredient_category": category_label,
                "fiber_solubility": SOLUBILITY_LABELS.get(attributes.get("fiber_solubility")),
                "fermentability": FERMENTABILITY_LABELS.get(attributes.get("fermentability")),
                "fiber_functions": [
                    FIBER_FUNCTION_LABELS[value]
                    for value in attributes.get("fiber_functions") or []
                    if value in FIBER_FUNCTION_LABELS
                ],
                "prebiotic_functions": [
                    PREBIOTIC_FUNCTION_LABELS[value]
                    for value in attributes.get("prebiotic_functions") or []
                    if value in PREBIOTIC_FUNCTION_LABELS
                ],
            }
            details.setdefault(name, detail)
            subtype_tags.setdefault(category_label, [])
            if name not in subtype_tags[category_label]:
                subtype_tags[category_label].append(name)

        if category == "carbohydrate":
            starch_category = str(attributes.get("starch_category") or "")
            mapped = STARCH_CATEGORY_MAP.get(starch_category)
            if mapped:
                category_label, base_score = mapped
                weight = _position_weight(item.get("position"))
                starch_items.append(
                    {
                        "ingredient_name": name,
                        "standard_ingredient_id": standard_id,
                        "rank": int(item.get("position") or 0),
                        "weight": weight,
                        "category": category_label,
                        "category_code": starch_category,
                        "base_score": base_score,
                        "weighted_score": round(base_score * weight, 6),
                        "matched_keywords": [],
                        "attribute_origin": "science_profile",
                        "science_profile_version": profile.get("profile_version"),
                    }
                )

    category_tags = list(subtype_tags)
    feature_json = {
        "ingredient_category_tags": category_tags,
        "ingredient_subtype_tags": subtype_tags,
        "ingredient_tag_detail": details,
    }
    versions = sorted(
        {
            (standard_id, profiles[standard_id].get("profile_version"))
            for standard_id in used_ids
            if standard_id in profiles
        }
    )
    fingerprint = hashlib.sha256(
        json.dumps(versions, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return {
        "ingredient_feature_json": feature_json,
        "starch_ingredients_json": starch_items,
        "science_profile_eligible_count": eligible_count,
        "science_profile_used_count": len(used_ids),
        "science_profile_coverage": round(len(used_ids) / eligible_count, 6) if eligible_count else 1.0,
        "missing_science_profiles": missing,
        "profile_status": "ready" if not missing else "needs_review",
        "profile_version": SCIENCE_PROFILE_VERSION,
        "science_source_fingerprint": fingerprint,
    }


def structure_labels(feature_json: dict[str, Any], starch_items: list[dict[str, Any]]) -> dict[str, list[str]]:
    starch_labels: list[str] = []
    if not starch_items:
        starch_labels.append("低淀粉结构")
    else:
        main = max(starch_items, key=lambda row: float(row.get("weighted_score") or 0))
        category = str(main.get("category_code") or "")
        label = {
            "grain": "谷物膨化结构",
            "tuber": "薯类膨化结构",
            "legume": "豆类淀粉结构",
        }.get(category)
        starch_labels.append(label or "低淀粉结构")

    details = feature_json.get("ingredient_tag_detail") or {}
    functions: Counter[str] = Counter()
    has_fiber = False
    has_prebiotic = False
    for info in details.values():
        category = str(info.get("ingredient_category") or "")
        fiber_functions = info.get("fiber_functions") or []
        prebiotic_functions = info.get("prebiotic_functions") or []
        functions.update(fiber_functions)
        has_fiber = has_fiber or category == "膳食纤维" or bool(fiber_functions)
        has_prebiotic = has_prebiotic or category == "益生元" or bool(prebiotic_functions)

    fiber_labels: list[str] = []
    high_absorb = functions["吸水成形"] >= 2
    if high_absorb:
        fiber_labels.append("高吸水纤维结构")
    if functions["胶质成形"] >= 1:
        fiber_labels.append("胶质纤维结构")
    insoluble = functions["增加粪便骨架"] >= 1 or functions["缓冲刺激"] >= 2
    if insoluble and not high_absorb:
        fiber_labels.append("不溶性纤维结构")

    gut_labels: list[str] = []
    if has_prebiotic and has_fiber:
        gut_labels.append("益生元+纤维复合结构")
    elif has_prebiotic:
        gut_labels.append("益生元发酵结构")
    return {"starch": starch_labels, "fiber": fiber_labels, "gut": gut_labels}

