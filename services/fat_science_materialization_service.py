"""Build formula-level fat, antioxidant, and micronutrient fields from science profiles."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


SCIENCE_PROFILE_VERSION = "science-v1"
POSITIVE_STRENGTHS = {"weak", "medium", "strong"}
FAT_TYPE_LABELS = {
    "animal": "动物脂肪",
    "marine": "动物脂肪",
    "plant": "植物脂肪",
    "mixed": "混合脂肪来源",
}
ANTIOXIDANT_TYPE_LABELS = {
    "natural_vitamin": "天然抗氧化物",
    "phytochemical": "植物抗氧化来源",
    "plant_extract": "天然抗氧化物",
    "synthetic": "人工抗氧化物",
    "other": "其他抗氧化来源",
}
MICRONUTRIENT_TYPE_LABELS = {
    "animal_organ": "动物内脏",
    "animal_tissue": "动物组织来源",
    "egg": "天然微量元素来源",
    "fortified": "微量元素强化",
    "mineral": "矿物质来源",
    "natural": "天然矿物质来源",
}
MICRONUTRIENT_SOURCE_LABELS = {
    "animal_organ": "动物内脏",
    "animal_tissue": "动物组织",
    "egg": "蛋类",
}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _append(values: list[str], value: Any) -> None:
    cleaned = str(value or "").strip()
    if cleaned and cleaned not in values:
        values.append(cleaned)


def _joined(values: list[str]) -> str | None:
    return "、".join(sorted(values)) if values else None


def build_science_features(
    items: Iterable[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = {
        "fat_sources": [],
        "fat_source_types": [],
        "antioxidant_sources": [],
        "antioxidant_types": [],
        "micronutrient_sources": [],
        "micronutrient_types": [],
        "omega6_sources": [],
        "omega3_sources": [],
    }
    missing: list[dict[str, Any]] = []
    used_versions: set[tuple[str, Any]] = set()

    for item in items:
        if item.get("is_ignored"):
            continue
        standard_id = str(item.get("standard_ingredient_id") or "")
        profile = profiles.get(standard_id)
        if not profile or profile.get("science_status") != "active":
            continue
        category = str(profile.get("nutrition_category") or "")
        domain = _json_object(profile.get("domain_attributes_json"))
        functions = _json_object(profile.get("function_attributes_json"))
        name = str(item.get("standard_name") or item.get("raw_name") or standard_id)
        used = False

        # Omega labels are deliberately restricted to active fat profiles. Fish
        # meat and fish meal remain protein and do not enter these fields.
        if category == "fat":
            fat_source = str(domain.get("fat_source") or "")
            fat_functions = set(domain.get("fat_functions") or [])
            if fat_source not in {"", "unknown", "none"}:
                _append(fields["fat_sources"], name)
                _append(fields["fat_source_types"], FAT_TYPE_LABELS.get(fat_source))
                used = True
            if "omega3" in fat_functions:
                _append(fields["omega3_sources"], name)
                used = True
            if "omega6" in fat_functions:
                _append(fields["omega6_sources"], name)
                used = True

        antioxidant_type = str(domain.get("antioxidant_type") or "")
        antioxidant_support = str(functions.get("antioxidant_support") or "")
        if category == "antioxidant" or antioxidant_support in POSITIVE_STRENGTHS:
            _append(fields["antioxidant_sources"], name)
            _append(
                fields["antioxidant_types"],
                ANTIOXIDANT_TYPE_LABELS.get(antioxidant_type)
                or ("天然抗氧化物" if category == "vitamin" else "其他抗氧化来源"),
            )
            used = True

        micronutrient_type = str(domain.get("micronutrient_source_type") or "")
        micronutrient_support = str(functions.get("micronutrient_support") or "")
        if category == "mineral" and micronutrient_type in {"", "unknown", "none"}:
            mineral_type = str(domain.get("mineral_type") or "")
            micronutrient_type = "natural" if mineral_type == "natural" else "fortified" if mineral_type in {"organic_salt", "chelated"} else "mineral"
        if micronutrient_type not in {"", "unknown", "none"} and (
            category == "mineral" or micronutrient_support in POSITIVE_STRENGTHS
        ):
            _append(
                fields["micronutrient_sources"],
                MICRONUTRIENT_SOURCE_LABELS.get(micronutrient_type) or name,
            )
            _append(fields["micronutrient_types"], MICRONUTRIENT_TYPE_LABELS.get(micronutrient_type))
            used = True

        if used:
            used_versions.add((standard_id, profile.get("profile_version")))

    # Relevant standardized ingredients with inactive profiles must be reviewed;
    # non-relevant protein/fiber ingredients are not treated as missing fat data.
    for item in items:
        if item.get("is_ignored") or not item.get("standard_ingredient_id"):
            continue
        standard_id = str(item["standard_ingredient_id"])
        profile = profiles.get(standard_id)
        if profile and profile.get("science_status") == "active":
            continue
        role = str(item.get("primary_nutrition_role") or "")
        if role in {"脂肪酸支持", "抗氧化支持", "矿物质补充"}:
            missing.append({
                "standard_ingredient_id": standard_id,
                "name": item.get("standard_name") or item.get("raw_name"),
                "reason": "missing_active_science_profile",
            })

    fingerprint = hashlib.sha256(
        json.dumps(sorted(used_versions), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    result = {key: _joined(value) for key, value in fields.items()}
    result.update({
        "profile_status": "ready" if not missing else "needs_review",
        "profile_version": SCIENCE_PROFILE_VERSION,
        "source_fingerprint": fingerprint,
        "missing_science_profiles": missing,
    })
    return result
