"""Minimal, non-blocking science profiles for standardized ingredients.

Science profiles explain an ingredient's nutrition category and functional
attributes.  They are deliberately separate from the production score tables:
the first rollout stores reviewed knowledge without changing current scores.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pymysql

from app_config import get_mysql_config


SCIENCE_PROFILE_TABLE = "catfood_ingredient_science_profile"
SCIENCE_SCORE_MAPPING_TABLE = "catfood_science_score_mapping"

NUTRITION_CATEGORIES = {
    "protein",
    "fat",
    "carbohydrate",
    "fiber",
    "prebiotic",
    "probiotic",
    "antioxidant",
    "mineral",
    "vitamin",
    "other",
}

NUTRITION_SUBTYPES = {
    "protein": {"fresh", "frozen", "meal", "hydrolyzed", "plant", "other"},
    "fat": {"animal", "marine", "plant", "mixed", "other"},
    "carbohydrate": {"grain", "tuber", "legume", "flour", "refined_starch", "other"},
    "fiber": {"soluble", "insoluble", "mixed", "other"},
    "prebiotic": {"oligosaccharide", "inulin", "yeast_derived", "other"},
    "probiotic": {"live", "inactivated", "unknown_form"},
    "antioxidant": {"other"},
    "mineral": {"other"},
    "vitamin": {"other"},
    "other": {"other"},
}

FUNCTION_ATTRIBUTE_KEYS = (
    "protein_supply",
    "starch_burden",
    "fat_supply",
    "forming_support",
    "bulk_support",
    "buffer_support",
    "microbiome_feed",
    "scfa_support",
    "omega3_support",
    "antioxidant_support",
    "micronutrient_support",
)
FUNCTION_STRENGTHS = {"unknown", "none", "weak", "medium", "strong"}
SCIENCE_STATUSES = {"draft", "active", "excluded", "deprecated"}
EVIDENCE_LEVELS = {"unknown", "low", "medium", "high"}

DOMAIN_ATTRIBUTE_DEFINITIONS = {
    "protein": {
        "protein_source": {"kind": "single", "values": ["unknown", "none", "animal", "plant", "mixed"]},
        "protein_form": {"kind": "single", "values": ["unknown", "none", "fresh", "frozen", "meal", "hydrolyzed", "concentrate", "isolate", "other"]},
        "source_specificity": {"kind": "single", "values": ["unknown", "generic", "category_clear", "specific"]},
        "animal_source": {"kind": "single", "values": ["unknown", "none", "chicken", "duck", "turkey", "fish", "beef", "lamb", "pork", "egg", "mixed", "other"]},
        "plant_protein_form": {"kind": "single", "values": ["none", "whole", "meal", "concentrate", "isolate", "hydrolyzed", "other", "unknown"]},
        "animal_source_category": {"kind": "single", "values": ["unknown", "none", "poultry", "livestock", "rabbit", "fish", "shellfish", "egg", "dairy", "other"]},
        "micronutrient_source_type": {"kind": "single", "values": ["unknown", "none", "animal_organ", "animal_tissue", "egg"]},
    },
    "carbohydrate": {
        "starch_category": {"kind": "single", "values": ["unknown", "none", "legume", "grain", "tuber", "flour", "refined_starch", "available_sugar"]},
    },
    "fiber": {
        "fiber_solubility": {"kind": "single", "values": ["unknown", "none", "insoluble", "mixed", "soluble"]},
        "fermentability": {"kind": "single", "values": ["unknown", "none", "low", "medium_low", "medium", "high"]},
        "fiber_functions": {"kind": "multi", "values": ["forming", "bulk", "buffer", "gel_forming"]},
        "prebiotic_functions": {"kind": "multi", "values": ["microbiome_feed", "scfa_support"]},
    },
    "prebiotic": {
        "fiber_solubility": {"kind": "single", "values": ["unknown", "none", "insoluble", "mixed", "soluble"]},
        "fermentability": {"kind": "single", "values": ["unknown", "none", "low", "medium_low", "medium", "high"]},
        "fiber_functions": {"kind": "multi", "values": ["forming", "bulk", "buffer", "gel_forming"]},
        "prebiotic_functions": {"kind": "multi", "values": ["microbiome_feed", "scfa_support"]},
    },
    "fat": {
        "fat_source": {"kind": "single", "values": ["unknown", "none", "animal", "marine", "plant", "mixed"]},
        "fat_functions": {"kind": "multi", "values": ["energy", "omega3", "omega6", "antioxidant_support"]},
    },
    "antioxidant": {
        "antioxidant_type": {"kind": "single", "values": ["unknown", "none", "natural_vitamin", "phytochemical", "plant_extract", "synthetic", "other"]},
        "antioxidant_functions": {"kind": "multi", "values": ["lipid_protection", "radical_scavenging", "synergist"]},
    },
    "mineral": {
        "mineral_type": {"kind": "single", "values": ["unknown", "none", "inorganic_salt", "organic_salt", "chelated", "natural", "other"]},
        "mineral_elements": {"kind": "multi", "values": ["calcium", "phosphorus", "sodium", "potassium", "chloride", "magnesium", "iron", "zinc", "copper", "manganese", "selenium", "iodine", "other"]},
        "micronutrient_source_type": {"kind": "single", "values": ["unknown", "none", "fortified", "mineral", "natural"]},
    },
}

DEFAULT_SCORE_MAPPINGS = (
    ("protein", "protein_form", "fresh", "digestibility", 0.85, "weighted_avg"),
    ("protein", "protein_form", "frozen", "digestibility", 0.80, "weighted_avg"),
    ("protein", "protein_form", "meal", "digestibility", 0.55, "weighted_avg"),
    ("protein", "protein_form", "hydrolyzed", "digestibility", 1.00, "weighted_avg"),
    ("protein", "source_specificity", "specific", "source_clarity", 1.00, "weighted_avg"),
    ("protein", "source_specificity", "category_clear", "source_clarity", 0.70, "weighted_avg"),
    ("protein", "source_specificity", "generic", "source_clarity", 0.30, "weighted_avg"),
    ("carbohydrate", "starch_category", "legume", "starch_burden", 1.20, "sum"),
    ("carbohydrate", "starch_category", "grain", "starch_burden", 1.30, "sum"),
    ("carbohydrate", "starch_category", "tuber", "starch_burden", 1.50, "sum"),
    ("carbohydrate", "starch_category", "flour", "starch_burden", 1.80, "sum"),
    ("carbohydrate", "starch_category", "refined_starch", "starch_burden", 2.00, "sum"),
    ("carbohydrate", "starch_category", "available_sugar", "starch_burden", 2.00, "sum"),
    ("fiber", "fiber_solubility", "insoluble", "p_bulk", 0.80, "sum"),
    ("fiber", "fiber_solubility", "insoluble", "p_form", 0.20, "sum"),
    ("fiber", "fiber_solubility", "mixed", "p_form", 0.40, "sum"),
    ("fiber", "fiber_solubility", "mixed", "p_bulk", 0.40, "sum"),
    ("fiber", "fiber_solubility", "mixed", "p_buffer", 0.20, "sum"),
    ("fiber", "fiber_solubility", "soluble", "p_form", 0.80, "sum"),
    ("fiber", "fermentability", "low", "q_feed", 0.10, "sum"),
    ("fiber", "fermentability", "medium_low", "q_feed", 0.30, "sum"),
    ("fiber", "fermentability", "medium_low", "q_scfa", 0.20, "sum"),
    ("fiber", "fermentability", "medium", "q_feed", 0.50, "sum"),
    ("fiber", "fermentability", "medium", "q_scfa", 0.40, "sum"),
    ("fiber", "fermentability", "high", "q_feed", 0.80, "sum"),
    ("fiber", "fermentability", "high", "q_scfa", 0.60, "sum"),
    ("fiber", "fiber_functions", "forming", "p_form", 1.00, "sum"),
    ("fiber", "fiber_functions", "bulk", "p_bulk", 1.00, "sum"),
    ("fiber", "fiber_functions", "buffer", "p_buffer", 1.00, "sum"),
    ("fiber", "fiber_functions", "gel_forming", "p_form", 1.00, "sum"),
    ("fiber", "prebiotic_functions", "microbiome_feed", "q_feed", 1.00, "sum"),
    ("fiber", "prebiotic_functions", "scfa_support", "q_scfa", 1.00, "sum"),
    ("fat", "fat_functions", "omega3", "omega3_support", 1.00, "sum"),
    ("fat", "fat_functions", "antioxidant_support", "antioxidant_support", 1.00, "sum"),
    ("antioxidant", "antioxidant_type", "natural_vitamin", "antioxidant_support", 1.00, "sum"),
    ("antioxidant", "antioxidant_type", "phytochemical", "antioxidant_support", 1.00, "sum"),
    ("antioxidant", "antioxidant_type", "plant_extract", "antioxidant_support", 1.00, "sum"),
    ("antioxidant", "antioxidant_type", "synthetic", "antioxidant_support", 1.00, "sum"),
    ("antioxidant", "antioxidant_type", "other", "antioxidant_support", 1.00, "sum"),
)

IDENTITY_OWNED_DOMAIN_KEYS = {
    "protein": {"protein_source", "animal_source"},
    "fat": {"fat_source"},
}
ANIMAL_SOURCE_ENUM_MAP = {
    "鸡": "chicken", "鸭": "duck", "火鸡": "turkey", "鱼": "fish",
    "牛": "beef", "羊": "lamb", "猪": "pork", "蛋": "egg",
    "混合": "mixed",
}

CODE_COMPOSITE_RULES = (
    ("fiber_buffer_score", "纤维缓冲", "基础组合指标", (
        ("fiber_score", "纤维总分（SKU，缺失时使用 p_total）", 0.30, "positive"),
        ("p_total_score", "纤维结构总分 p_total", 0.25, "positive"),
        ("p_buffer", "肠道缓冲 p_buffer", 0.45, "positive"),
    )),
    ("microbiome_support_score", "菌群支持", "基础组合指标", (
        ("prebiotic_score", "益生元分（缺失时使用 q_feed）", 0.35, "positive"),
        ("q_scfa", "SCFA 支持 q_scfa", 0.45, "positive"),
        ("q_feed", "菌群底物 q_feed", 0.20, "positive"),
    )),
    ("skin_protection_score", "皮肤保护", "基础组合指标", (
        ("antioxidant_score", "抗氧化分（缺失时使用脂肪调节分）", 0.55, "positive"),
        ("fat_regulation_score", "脂肪调节分", 0.45, "positive"),
    )),
    ("gut_friendly_score", "肠胃友好", "产品功能指标", (
        ("soft_stool_risk_reverse", "软便风险反向分", 0.30, "positive"),
        ("fiber_buffer_score", "纤维缓冲", 0.25, "positive"),
        ("microbiome_support_score", "菌群支持", 0.20, "positive"),
        ("protein_pressure_score", "蛋白压力", 0.15, "reverse"),
        ("carb_burden_score", "碳水负担", 0.10, "reverse"),
    )),
    ("black_chin_friendly_score", "黑下巴友好", "产品功能指标", (
        ("black_chin_risk_reverse", "黑下巴风险反向分", 0.35, "positive"),
        ("fat_burden_score", "脂肪负担", 0.25, "reverse"),
        ("skin_protection_score", "皮肤保护", 0.20, "positive"),
        ("omega3_support_score", "Omega-3 支持", 0.10, "positive"),
        ("black_chin_base_score", "黑下巴基础友好分", 0.10, "positive"),
    )),
    ("muscle_gain_score", "增肌长肉", "产品功能指标", (
        ("protein_quality_score", "蛋白质量", 0.35, "positive"),
        ("protein_pressure_score", "蛋白压力", 0.25, "reverse"),
        ("carb_burden_score", "碳水负担", 0.10, "reverse"),
        ("fat_balance_score", "脂肪负担距中值的平衡分", 0.15, "balance"),
        ("microbiome_support_score", "菌群支持", 0.15, "positive"),
    )),
    ("weight_control_score", "控重管理", "产品功能指标", (
        ("fat_burden_score", "脂肪负担", 0.30, "reverse"),
        ("calorie_density_score", "热量密度", 0.25, "reverse"),
        ("carb_burden_score", "碳水负担", 0.15, "reverse"),
        ("fiber_buffer_score", "纤维缓冲", 0.20, "positive"),
        ("protein_quality_score", "蛋白质量", 0.10, "positive"),
    )),
    ("skin_coat_score", "皮肤毛发", "产品功能指标", (
        ("skin_protection_score", "皮肤保护", 0.35, "positive"),
        ("omega3_support_score", "Omega-3 支持", 0.25, "positive"),
        ("fat_burden_score", "脂肪负担", 0.15, "reverse"),
        ("black_chin_risk_reverse", "黑下巴风险反向分", 0.15, "positive"),
        ("protein_quality_score", "蛋白质量", 0.10, "positive"),
    )),
)


def _connect(*, autocommit: bool = False):
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=autocommit,
    )


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def empty_function_attributes() -> dict[str, str]:
    return {key: "unknown" for key in FUNCTION_ATTRIBUTE_KEYS}


def empty_domain_attributes(category: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, definition in DOMAIN_ATTRIBUTE_DEFINITIONS.get(category, {}).items():
        result[key] = [] if definition["kind"] == "multi" else "unknown"
    return result


def normalize_domain_attributes(category: str, value: Any) -> dict[str, Any]:
    if value in (None, ""):
        raw: dict[str, Any] = {}
    elif isinstance(value, dict):
        raw = value
    elif isinstance(value, str):
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("domain_attributes 必须是对象")
    else:
        raise ValueError("domain_attributes 必须是对象")
    definitions = DOMAIN_ATTRIBUTE_DEFINITIONS.get(category, {})
    unknown_keys = sorted(set(raw) - set(definitions))
    if unknown_keys:
        raise ValueError(f"未知领域属性: {', '.join(unknown_keys)}")
    result = empty_domain_attributes(category)
    for key, raw_value in raw.items():
        definition = definitions[key]
        allowed = set(definition["values"])
        if definition["kind"] == "multi":
            values = raw_value if isinstance(raw_value, list) else []
            cleaned = list(dict.fromkeys(_clean(item).lower() for item in values if _clean(item)))
            invalid = sorted(set(cleaned) - allowed)
            if invalid:
                raise ValueError(f"{key} 包含无效值: {', '.join(invalid)}")
            result[key] = cleaned
        else:
            cleaned = _clean(raw_value).lower() or "unknown"
            if cleaned not in allowed:
                raise ValueError(f"{key} 的枚举值无效: {cleaned}")
            result[key] = cleaned
    return result


def inherited_domain_attributes(category: str, ingredient: dict[str, Any]) -> dict[str, Any]:
    source_type = _clean(ingredient.get("source_type")).lower()
    animal_source = _clean(ingredient.get("animal_source"))
    subtype = _clean(ingredient.get("nutrition_subtype")).lower()
    inherited: dict[str, Any] = {}
    if category == "protein":
        inherited["protein_source"] = source_type if source_type in {"animal", "plant", "mixed"} else "unknown"
        if source_type == "animal":
            inherited["animal_source"] = ANIMAL_SOURCE_ENUM_MAP.get(animal_source, "other" if animal_source else "unknown")
        else:
            inherited["animal_source"] = "none"
    elif category == "fat":
        if subtype == "marine":
            inherited["fat_source"] = "marine"
        else:
            inherited["fat_source"] = source_type if source_type in {"animal", "plant", "mixed"} else "unknown"
    return inherited


def strip_identity_owned_attributes(category: str, attributes: dict[str, Any]) -> dict[str, Any]:
    owned = IDENTITY_OWNED_DOMAIN_KEYS.get(category, set())
    return {key: value for key, value in attributes.items() if key not in owned}


def suggest_science_profile(ingredient: dict[str, Any]) -> dict[str, Any]:
    """Build a conservative draft from existing standardization metadata.

    Suggestions select a domain but never invent a positive function strength.
    A reviewer must explicitly activate scientifically meaningful attributes.
    """
    role = _clean(ingredient.get("primary_nutrition_role"))
    family = _clean(ingredient.get("ingredient_family"))
    source_type = _clean(ingredient.get("source_type"))
    name = _clean(ingredient.get("standard_name"))
    text = " ".join((role, family, source_type, name))

    category = "other"
    subtype = "other"
    if "益生菌" in text:
        category, subtype = "probiotic", "unknown_form"
    elif "益生元" in text:
        category = "prebiotic"
        subtype = "inulin" if any(word in text for word in ("菊粉", "菊糖", "菊苣")) else "other"
    elif "矿物" in role:
        # Reviewed identity wins over name matching: 蛋白锌/铜/锰 are
        # chelated minerals, not protein ingredients.
        category, subtype = "mineral", "other"
    elif "脂肪酸" in role or "脂肪供给" in role:
        category = "fat"
        if any(word in name for word in ("鱼油", "磷虾油", "藻油", "海洋")):
            subtype = "marine"
        elif source_type == "plant":
            subtype = "plant"
        elif source_type == "animal":
            subtype = "animal"
        else:
            subtype = "other"
    elif "纤维" in text:
        category, subtype = "fiber", "other"
    elif "蛋白" in text or any(word in name for word in ("肉", "鱼粉", "蛋白")):
        category = "protein"
        if any(word in name for word in ("水解", "酶解")):
            subtype = "hydrolyzed"
        elif "粉" in name and any(word in name for word in ("肉", "鱼")):
            subtype = "meal"
        elif source_type == "plant":
            subtype = "plant"
        elif "冻" in name and "冻干" not in name:
            subtype = "frozen"
        elif any(word in name for word in ("鲜", "肉", "鱼")):
            subtype = "fresh"
        else:
            subtype = "other"
    elif any(word in text for word in ("脂肪", "脂肪酸", "油脂")) or re.search(r"(?:油|脂)$", name):
        category = "fat"
        if any(word in name for word in ("鱼油", "磷虾油", "海洋")):
            subtype = "marine"
        elif source_type == "plant":
            subtype = "plant"
        elif source_type == "animal":
            subtype = "animal"
        else:
            subtype = "other"
    elif any(word in text for word in ("碳水", "淀粉")):
        category = "carbohydrate"
        subtype = "refined_starch" if "淀粉" in name else "other"
    elif "抗氧化" in text:
        category, subtype = "antioxidant", "other"
    elif "矿物" in text:
        category, subtype = "mineral", "other"
    elif "维生素" in text:
        category, subtype = "vitamin", "other"

    return {
        "nutrition_category": category,
        "nutrition_subtype": subtype,
        "domain_attributes": empty_domain_attributes(category),
        "function_attributes": empty_function_attributes(),
        "science_status": "draft",
        "evidence_level": "unknown",
    }


def normalize_function_attributes(value: Any) -> dict[str, str]:
    if value in (None, ""):
        raw: dict[str, Any] = {}
    elif isinstance(value, dict):
        raw = value
    elif isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("function_attributes 必须是对象")
        raw = parsed
    else:
        raise ValueError("function_attributes 必须是对象")

    unknown_keys = sorted(set(raw) - set(FUNCTION_ATTRIBUTE_KEYS))
    if unknown_keys:
        raise ValueError(f"未知功能属性: {', '.join(unknown_keys)}")
    normalized = empty_function_attributes()
    for key, strength in raw.items():
        strength = _clean(strength).lower()
        if strength not in FUNCTION_STRENGTHS:
            raise ValueError(f"{key} 的强度无效: {strength}")
        normalized[key] = strength
    return normalized


def validate_science_profile(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    category = _clean(payload.get("nutrition_category")).lower()
    if category or not partial:
        if category not in NUTRITION_CATEGORIES:
            raise ValueError("nutrition_category 无效")
        normalized["nutrition_category"] = category

    subtype = _clean(payload.get("nutrition_subtype")).lower()
    if subtype or not partial:
        effective_category = category or _clean(payload.get("current_nutrition_category")).lower()
        if effective_category not in NUTRITION_SUBTYPES or subtype not in NUTRITION_SUBTYPES[effective_category]:
            raise ValueError("nutrition_subtype 与营养类别不匹配")
        normalized["nutrition_subtype"] = subtype

    if "function_attributes" in payload or not partial:
        normalized["function_attributes"] = normalize_function_attributes(payload.get("function_attributes"))

    if "domain_attributes" in payload or not partial:
        effective_category = category or _clean(payload.get("current_nutrition_category")).lower()
        normalized["domain_attributes"] = normalize_domain_attributes(
            effective_category, payload.get("domain_attributes")
        )

    status = _clean(payload.get("science_status")).lower()
    if status or not partial:
        if status not in SCIENCE_STATUSES:
            raise ValueError("science_status 无效")
        normalized["science_status"] = status

    evidence = _clean(payload.get("evidence_level")).lower()
    if evidence or not partial:
        if evidence not in EVIDENCE_LEVELS:
            raise ValueError("evidence_level 无效")
        normalized["evidence_level"] = evidence

    if "review_note" in payload:
        normalized["review_note"] = _clean(payload.get("review_note")) or None
    if "reviewed_by" in payload:
        normalized["reviewed_by"] = _clean(payload.get("reviewed_by")) or None
    return normalized


def ensure_science_profile_table(cursor=None) -> None:
    own_connection = cursor is None
    conn = _connect() if own_connection else None
    target = conn.cursor() if own_connection else cursor
    try:
        target.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{SCIENCE_PROFILE_TABLE}` (
              profile_id BIGINT NOT NULL AUTO_INCREMENT,
              standard_ingredient_id VARCHAR(32) NOT NULL,
              nutrition_category VARCHAR(32) NOT NULL,
              nutrition_subtype VARCHAR(32) NOT NULL,
              domain_attributes_json JSON NULL,
              function_attributes_json JSON NOT NULL,
              science_status VARCHAR(24) NOT NULL DEFAULT 'draft',
              evidence_level VARCHAR(16) NOT NULL DEFAULT 'unknown',
              review_note TEXT NULL,
              profile_version INT NOT NULL DEFAULT 1,
              reviewed_by VARCHAR(128) NULL,
              reviewed_at DATETIME NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (profile_id),
              UNIQUE KEY uq_science_standard_ingredient (standard_ingredient_id),
              KEY idx_science_status (science_status),
              KEY idx_science_category (nutrition_category)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        target.execute(
            "SELECT COUNT(*) AS found FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s AND column_name='domain_attributes_json'",
            (SCIENCE_PROFILE_TABLE,),
        )
        if not target.fetchone()["found"]:
            target.execute(
                f"ALTER TABLE `{SCIENCE_PROFILE_TABLE}` ADD COLUMN domain_attributes_json JSON NULL "
                "AFTER nutrition_subtype"
            )
        target.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{SCIENCE_SCORE_MAPPING_TABLE}` (
              mapping_id BIGINT NOT NULL AUTO_INCREMENT,
              domain_code VARCHAR(32) NOT NULL,
              attribute_code VARCHAR(64) NOT NULL,
              attribute_value VARCHAR(64) NOT NULL,
              target_component VARCHAR(64) NOT NULL,
              contribution_value DECIMAL(12,6) NOT NULL,
              direction VARCHAR(16) NOT NULL DEFAULT 'positive',
              aggregation_method VARCHAR(24) NOT NULL DEFAULT 'sum',
              config_version VARCHAR(32) NOT NULL DEFAULT 'science_v1',
              active TINYINT(1) NOT NULL DEFAULT 1,
              evidence_note VARCHAR(500) NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (mapping_id),
              UNIQUE KEY uq_science_mapping (
                domain_code, attribute_code, attribute_value,
                target_component, config_version
              ),
              KEY idx_science_mapping_active (domain_code, active, config_version)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        target.executemany(
            f"""
            INSERT IGNORE INTO `{SCIENCE_SCORE_MAPPING_TABLE}`(
              domain_code,attribute_code,attribute_value,target_component,
              contribution_value,aggregation_method,config_version,evidence_note
            ) VALUES(%s,%s,%s,%s,%s,%s,'science_v1','由现行评分规则迁移；当前仅用于预览')
            """,
            DEFAULT_SCORE_MAPPINGS,
        )
        if own_connection:
            conn.commit()
    finally:
        if own_connection:
            target.close()
            conn.close()


def ensure_science_profile_draft(standard_ingredient_id: str) -> dict[str, Any]:
    """Idempotently create a conservative draft for one standard ingredient."""
    with _connect() as conn:
        with conn.cursor() as cursor:
            ensure_science_profile_table(cursor)
            cursor.execute(
                "SELECT * FROM catfood_standard_ingredient WHERE standard_ingredient_id=%s AND active=1",
                (_clean(standard_ingredient_id),),
            )
            ingredient = cursor.fetchone()
            if not ingredient:
                raise KeyError(f"标准原料不存在: {standard_ingredient_id}")
            suggestion = suggest_science_profile(ingredient)
            cursor.execute(
                f"""
                INSERT IGNORE INTO `{SCIENCE_PROFILE_TABLE}`(
                  standard_ingredient_id, nutrition_category, nutrition_subtype,
                  domain_attributes_json, function_attributes_json, science_status, evidence_level
                ) VALUES(%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    ingredient["standard_ingredient_id"],
                    suggestion["nutrition_category"],
                    suggestion["nutrition_subtype"],
                    json.dumps(suggestion["domain_attributes"], ensure_ascii=False),
                    json.dumps(suggestion["function_attributes"], ensure_ascii=False),
                    suggestion["science_status"],
                    suggestion["evidence_level"],
                ),
            )
            cursor.execute(
                f"""SELECT p.*,i.standard_name,i.ingredient_family,i.source_type,
                           i.animal_source,i.primary_nutrition_role
                    FROM `{SCIENCE_PROFILE_TABLE}` p
                    JOIN catfood_standard_ingredient i
                      ON i.standard_ingredient_id=p.standard_ingredient_id
                    WHERE p.standard_ingredient_id=%s""",
                (ingredient["standard_ingredient_id"],),
            )
            profile = cursor.fetchone()
        conn.commit()
    return _serialize_profile(profile)


def _serialize_profile(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    result = dict(row)
    domain_value = result.pop("domain_attributes_json", None)
    try:
        result["domain_attributes"] = (
            domain_value if isinstance(domain_value, dict) else json.loads(domain_value or "{}")
        )
    except (TypeError, json.JSONDecodeError):
        result["domain_attributes"] = {}
    category = result.get("nutrition_category", "")
    defaults = strip_identity_owned_attributes(category, empty_domain_attributes(category))
    defaults.update(strip_identity_owned_attributes(category, result["domain_attributes"]))
    result["domain_attributes"] = defaults
    identity = {
        "standard_name": result.get("standard_name"),
        "ingredient_family": result.get("ingredient_family"),
        "source_type": result.get("source_type"),
        "animal_source": result.get("animal_source"),
        "primary_nutrition_role": result.get("primary_nutrition_role"),
    }
    inherited = inherited_domain_attributes(category, result)
    effective = empty_domain_attributes(category)
    effective.update(defaults)
    effective.update(inherited)
    result["identity_snapshot"] = identity
    result["inherited_domain_attributes"] = inherited
    result["effective_domain_attributes"] = effective
    value = result.pop("function_attributes_json", None)
    if isinstance(value, dict):
        result["function_attributes"] = value
    else:
        try:
            result["function_attributes"] = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            result["function_attributes"] = empty_function_attributes()
    for key, value in list(result.items()):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat(sep=" ")
    return result


def get_science_profile(standard_ingredient_id: str, *, create_draft: bool = False) -> dict[str, Any] | None:
    if create_draft:
        return ensure_science_profile_draft(standard_ingredient_id)
    with _connect() as conn:
        with conn.cursor() as cursor:
            ensure_science_profile_table(cursor)
            cursor.execute(
                f"""SELECT p.*,i.standard_name,i.ingredient_family,i.source_type,
                           i.animal_source,i.primary_nutrition_role
                    FROM `{SCIENCE_PROFILE_TABLE}` p
                    JOIN catfood_standard_ingredient i
                      ON i.standard_ingredient_id=p.standard_ingredient_id
                    WHERE p.standard_ingredient_id=%s""",
                (_clean(standard_ingredient_id),),
            )
            return _serialize_profile(cursor.fetchone())


def list_science_profiles(*, status: str = "", query: str = "", limit: int = 200) -> dict[str, Any]:
    where = ["1=1"]
    params: list[Any] = []
    if _clean(status):
        if _clean(status).lower() not in SCIENCE_STATUSES:
            raise ValueError("science_status 无效")
        where.append("p.science_status=%s")
        params.append(_clean(status).lower())
    if _clean(query):
        where.append("i.standard_name LIKE %s")
        params.append(f"%{_clean(query)}%")
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        with conn.cursor() as cursor:
            ensure_science_profile_table(cursor)
            cursor.execute(
                f"""
                SELECT p.*, i.standard_name, i.ingredient_family,
                       i.source_type, i.animal_source, i.primary_nutrition_role
                FROM `{SCIENCE_PROFILE_TABLE}` p
                JOIN catfood_standard_ingredient i
                  ON i.standard_ingredient_id=p.standard_ingredient_id
                WHERE {' AND '.join(where)}
                ORDER BY p.updated_at DESC
                LIMIT %s
                """,
                params,
            )
            items = [_serialize_profile(row) for row in cursor.fetchall()]
    return {"ok": True, "items": items, "count": len(items)}


def update_science_profile(standard_ingredient_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_science_profile(standard_ingredient_id)
    if not existing:
        # profile 不存在时才走 ensure（要求原料 active）
        existing = ensure_science_profile_draft(standard_ingredient_id)
    identity_suggestion = suggest_science_profile(existing)
    merged_payload = dict(payload)
    merged_payload["nutrition_category"] = identity_suggestion["nutrition_category"]
    merged_payload["nutrition_subtype"] = identity_suggestion["nutrition_subtype"]
    merged_payload["current_nutrition_category"] = identity_suggestion["nutrition_category"]
    normalized = validate_science_profile(merged_payload, partial=True)
    if not normalized:
        return existing

    category = identity_suggestion["nutrition_category"]
    subtype = identity_suggestion["nutrition_subtype"]
    if subtype not in NUTRITION_SUBTYPES[category]:
        raise ValueError("nutrition_subtype 与营养类别不匹配")
    functions = normalized.get("function_attributes", existing["function_attributes"])
    if "nutrition_category" in normalized and category != existing["nutrition_category"]:
        domain_attributes = normalized.get("domain_attributes", empty_domain_attributes(category))
    else:
        domain_attributes = normalized.get("domain_attributes", existing.get("domain_attributes", {}))
    domain_attributes = strip_identity_owned_attributes(category, domain_attributes)
    inherited_attributes = inherited_domain_attributes(category, existing)
    if category == "protein":
        if inherited_attributes.get("protein_source") == "plant":
            # 植物蛋白不适用动物来源分类和动物蛋白形态。
            domain_attributes["protein_form"] = "none"
            domain_attributes["animal_source_category"] = "none"
        else:
            # 非植物蛋白不适用植物蛋白形态。
            domain_attributes["plant_protein_form"] = "none"
    status = normalized.get("science_status", existing["science_status"])
    evidence = normalized.get("evidence_level", existing["evidence_level"])
    review_note = normalized.get("review_note", existing.get("review_note"))
    reviewed_by = normalized.get("reviewed_by", existing.get("reviewed_by"))

    if status == "active":
        if evidence == "unknown":
            raise ValueError("科学属性生效前必须选择证据等级")

    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE `{SCIENCE_PROFILE_TABLE}`
                SET nutrition_category=%s, nutrition_subtype=%s,
                    domain_attributes_json=%s, function_attributes_json=%s, science_status=%s,
                    evidence_level=%s, review_note=%s,
                    reviewed_by=%s,
                    reviewed_at=IF(%s IN ('active','excluded'), NOW(), reviewed_at),
                    profile_version=profile_version+1
                WHERE standard_ingredient_id=%s
                """,
                (
                    category,
                    subtype,
                    json.dumps(domain_attributes, ensure_ascii=False),
                    json.dumps(functions, ensure_ascii=False),
                    status,
                    evidence,
                    review_note,
                    reviewed_by,
                    status,
                    _clean(standard_ingredient_id),
                ),
            )
        conn.commit()
    return get_science_profile(standard_ingredient_id) or {}


def science_profile_options() -> dict[str, Any]:
    return {
        "nutrition_categories": sorted(NUTRITION_CATEGORIES),
        "nutrition_subtypes": {key: sorted(values) for key, values in NUTRITION_SUBTYPES.items()},
        "function_attribute_keys": list(FUNCTION_ATTRIBUTE_KEYS),
        "function_strengths": sorted(FUNCTION_STRENGTHS),
        "science_statuses": sorted(SCIENCE_STATUSES),
        "evidence_levels": sorted(EVIDENCE_LEVELS),
        "domain_attribute_definitions": DOMAIN_ATTRIBUTE_DEFINITIONS,
    }


def preview_base_contributions(category: str, domain_attributes: Any) -> dict[str, Any]:
    category = _clean(category).lower()
    normalized = normalize_domain_attributes(category, domain_attributes)
    mapping_domain = "fiber" if category == "prebiotic" else category
    with _connect() as conn:
        with conn.cursor() as cursor:
            ensure_science_profile_table(cursor)
            cursor.execute(
                f"""
                SELECT attribute_code,attribute_value,target_component,
                       contribution_value,direction,aggregation_method
                FROM `{SCIENCE_SCORE_MAPPING_TABLE}`
                WHERE domain_code=%s AND config_version='science_v1' AND active=1
                ORDER BY mapping_id
                """,
                (mapping_domain,),
            )
            mappings = cursor.fetchall()
        conn.commit()
    contributions: dict[str, float] = {}
    explanations: list[dict[str, Any]] = []
    for mapping in mappings:
        selected = normalized.get(mapping["attribute_code"])
        matched = mapping["attribute_value"] in selected if isinstance(selected, list) else selected == mapping["attribute_value"]
        if not matched:
            continue
        value = float(mapping["contribution_value"])
        if mapping["direction"] == "negative":
            value = -value
        target = mapping["target_component"]
        contributions[target] = round(contributions.get(target, 0.0) + value, 6)
        explanations.append({
            "attribute_code": mapping["attribute_code"],
            "attribute_value": mapping["attribute_value"],
            "target_component": target,
            "contribution_value": value,
            "aggregation_method": mapping["aggregation_method"],
        })
    return {
        "ok": True,
        "config_version": "science_v1",
        "official_score_affected": False,
        "domain_attributes": normalized,
        "contributions": contributions,
        "explanations": explanations,
    }


def list_score_rules() -> dict[str, Any]:
    """Return read-only rule metadata; this endpoint never runs scoring."""
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT score_code,component_code,component_name,weight,direction,
                       config_version,rule_note
                FROM catfood_score_weight_config
                WHERE active=1
                ORDER BY score_code,weight DESC,component_code
                """
            )
            weight_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT rank_start,rank_end,position_weight,is_unknown,
                       config_version,note
                FROM catfood_score_position_weight_config
                WHERE active=1
                ORDER BY is_unknown,rank_start
                """
            )
            position_rows = cursor.fetchall()
    database_rules: dict[str, dict[str, Any]] = {}
    score_names = {
        "protein_quality_score": "蛋白质量",
        "protein_structure_score": "蛋白结构压力",
    }
    for row in weight_rows:
        rule = database_rules.setdefault(row["score_code"], {
            "score_code": row["score_code"],
            "score_name": score_names.get(row["score_code"], row["score_code"]),
            "rule_group": "领域大指标",
            "source": "database",
            "config_version": row["config_version"],
            "components": [],
        })
        rule["components"].append({
            "component_code": row["component_code"],
            "component_name": row["component_name"],
            "weight": float(row["weight"]),
            "direction": row["direction"],
            "note": (
                "独立判断动物来源；植物蛋白干扰仅在低植物蛋白干扰组件中计算"
                if row["component_code"] == "animal_protein_dominance"
                else row.get("rule_note")
            ),
        })
    code_rules = []
    for score_code, score_name, rule_group, components in CODE_COMPOSITE_RULES:
        code_rules.append({
            "score_code": score_code,
            "score_name": score_name,
            "rule_group": rule_group,
            "source": "code",
            "config_version": "current_code",
            "components": [
                {"component_code": code, "component_name": name, "weight": weight, "direction": direction}
                for code, name, weight, direction in components
            ],
        })
    return {
        "ok": True,
        "official_score_affected": False,
        "rules": list(database_rules.values()) + code_rules,
        "position_weights": [
            {
                **row,
                "position_weight": float(row["position_weight"]),
            }
            for row in position_rows
        ],
    }
