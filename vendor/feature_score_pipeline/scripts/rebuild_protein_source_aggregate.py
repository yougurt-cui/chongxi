#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from opencc import OpenCC

from brand_normalizer import build_product_key as build_corrected_product_key
from brand_normalizer import correct_brand


NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
SPLIT_SOURCE_RE = re.compile(r"[、,，/|]+")
ADDITIVE_SECTION_RE = re.compile(
    r"(?:添加剂组成|添加剂|营养添加剂|营养性添加剂|产品成分分析|营养分析|保证值)",
    re.I,
)
INGREDIENT_GROUP_MARKERS = (
    "及其制品",
    "等水生生物",
    "籽实及其制品",
)
# Premix category headers that group sub-ingredients (exact match only).
# e.g. "维生素(维生素E补充剂、硝酸硫胺、...)" should expand sub-items,
# but "维生素E补充剂(来源说明)" should NOT be treated as a group header.
PREMIX_GROUP_HEADERS = frozenset(("维生素", "矿物质"))
TRADITIONAL_TO_SIMPLIFIED = OpenCC("t2s")
BRACKET_OPENERS = frozenset("(（[［【{｛〔〈《「『")
BRACKET_CLOSERS = frozenset(")）]］】}｝〕〉》」』")
DOSAGE_FRAGMENT_RE = re.compile(
    r"(?i)(?<![a-z])(?:\d+(?:\.\d+)?\s*[×x]\s*)?\d+(?:\.\d+)?\s*"
    r"(?:mgkg|gkg|ugkg|mcgkg|iukg|cfukg|kcal(?:kg)?|"
    r"mg|kg|ug|mcg|iu|cfu|ppm|ml|g|%|％|"
    r"毫克|千克|公斤|微克|克|国际单位|菌落形成单位|千卡|卡路里|毫升|升)"
    r"(?:\s*[/／]\s*(?:kg|g|ml|l|千克|公斤|克|毫升|升|杯))?"
)
CSV_LABELING_PROJECT = Path(
    os.getenv(
        "CSV_LABELING_PROJECT",
        "/home/admin/projects/chongxi/vendor/csv_mysql_labeling",
    )
)

ANIMAL_SOURCE_LEVEL1_ORDER = ["禽类", "鱼类", "红肉类", "蛋类"]
ANIMAL_SOURCE_LEVEL2_TO_LEVEL1 = {
    "鸡": "禽类",
    "火鸡": "禽类",
    "鸭": "禽类",
    "鹌鹑": "禽类",
    "乳鸽": "禽类",
    "鲱鱼": "鱼类",
    "鳕鱼": "鱼类",
    "比目鱼": "鱼类",
    "岩鱼": "鱼类",
    "白鱼": "鱼类",
    "鱼": "鱼类",
    "牛": "红肉类",
    "羊": "红肉类",
    "鹿": "红肉类",
    "兔": "红肉类",
    "猪": "红肉类",
    "鸡蛋": "蛋类",
    "鸭蛋": "蛋类",
}
FISH_SUBTYPE_ORDER = [
    "鲱鱼",
    "鳕鱼",
    "比目鱼",
    "岩鱼",
    "鳟鱼",
    "海洋鱼",
    "金枪鱼",
    "三文鱼",
    "白鱼",
    "鲭鱼",
    "沙丁鱼",
    "马鲛鱼",
    "白斑狗鱼",
    "竹荚鱼",
    "鲻鱼",
    "凤尾鱼",
]


def _safe_name(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} cannot be empty")
    if not NAME_RE.match(value):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _fq(db_name: str, table_name: str) -> str:
    return f"`{db_name}`.`{table_name}`"


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _strip_bracketed_content(value: Any) -> str:
    """Remove balanced or half-open bracket groups while keeping outside text."""
    text_value = str(value or "")
    result: list[str] = []
    depth = 0
    for char in text_value:
        if char in BRACKET_OPENERS:
            depth += 1
            continue
        if char in BRACKET_CLOSERS:
            if depth > 0:
                depth -= 1
            continue
        if depth == 0:
            result.append(char)
    return "".join(result)


def _normalize_ingredient_display_name(value: Any) -> str:
    text_value = unicodedata.normalize("NFKC", str(value or "")).strip()
    text_value = _strip_bracketed_content(text_value)
    text_value = TRADITIONAL_TO_SIMPLIFIED.convert(text_value)
    return DOSAGE_FRAGMENT_RE.sub("", text_value).strip()


def _normalize_ingredient_key(value: Any) -> str:
    text_value = _normalize_ingredient_display_name(value).lower()
    return re.sub(r"[\s·•._\-—–/\\|,:：;；，。()（）\[\]【】'\"®™]+", "", text_value)


def _normalize_ingredient_literal_key(value: Any) -> str:
    """Normalize punctuation while retaining bracket contents for exact alias matching."""
    text_value = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text_value = TRADITIONAL_TO_SIMPLIFIED.convert(text_value)
    text_value = DOSAGE_FRAGMENT_RE.sub("", text_value)
    return re.sub(r"[\s·•._\-—–/\\|,:：;；，。()（）\[\]【】'\"®™]+", "", text_value)


def _split_grouped_alias_names(value: Any) -> list[str]:
    result: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in str(value or ""):
        if char in "([（【":
            depth += 1
        elif char in ")]）】" and depth:
            depth -= 1
        if char == "、" and depth == 0:
            item = "".join(buffer).strip()
            if item and item not in result:
                result.append(item)
            buffer = []
        else:
            buffer.append(char)
    item = "".join(buffer).strip()
    if item and item not in result:
        result.append(item)
    return result


def _ingredient_candidate_noise_reason(normalized_key: str) -> str | None:
    if not normalized_key:
        return "empty"
    if normalized_key.isdigit():
        return "pure_numeric"
    if re.fullmatch(r"[a-z]+", normalized_key, re.I):
        return "pure_latin_letters"
    if re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]", normalized_key):
        return "single_han_character"
    return None


def _strip_additive_section(value: Any) -> str:
    text_value = str(value or "")
    return ADDITIVE_SECTION_RE.split(text_value, maxsplit=1)[0]


def _clean_ingredient_token(value: Any) -> str:
    token = str(value or "").strip(" \t\r\n。. *＊")
    token = re.sub(r"\d+(?:\.\d+)?\s*[%％]", "", token)
    token = re.sub(r"\(\s*\)|（\s*）", "", token)
    token = re.sub(r"^[：:]+", "", token)
    return token.strip(" \t\r\n。.()（）[]【】*＊")


def _is_ingredient_group_header(value: Any) -> bool:
    text_value = _clean_ingredient_token(value)
    if not text_value:
        return False
    if text_value in PREMIX_GROUP_HEADERS:
        return True
    if any(marker in text_value for marker in INGREDIENT_GROUP_MARKERS):
        return True
    return bool(re.search(r"(?:鱼类|肉类|果蔬类|蔬果类|谷物类|豆类|油脂类)\s*$", text_value))


def _split_top_level_ingredient_tokens(value: Any) -> list[str]:
    text_value = str(value or "")
    tokens: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in text_value:
        if char in "(（[【":
            depth += 1
        elif char in ")）]】" and depth > 0:
            depth -= 1
        if char in "、,，;；\n" and depth == 0:
            token = "".join(buffer).strip()
            if token:
                tokens.append(token)
            buffer = []
            continue
        buffer.append(char)
    token = "".join(buffer).strip()
    if token:
        tokens.append(token)
    return tokens


def _expand_grouped_ingredient_token(token: str) -> list[str]:
    match = re.match(r"^\s*(?P<header>[^()（）]+?)(?:\d+(?:\.\d+)?\s*[%％])?\s*[（(](?P<inner>.*)[）)]\s*$", token)
    if match and _is_ingredient_group_header(match.group("header")):
        return [
            item
            for part in _split_top_level_ingredient_tokens(match.group("inner"))
            for item in _expand_grouped_ingredient_token(part)
        ]
    if _is_ingredient_group_header(token) and re.search(r"\d+(?:\.\d+)?\s*[%％]", str(token or "")):
        return []
    cleaned = _clean_ingredient_token(token)
    cleaned = re.sub(r"\([^)]*\)|（[^）]*）", "", cleaned).strip(" \t\r\n。.()（）[]【】")
    return [cleaned] if cleaned else []


def _split_source_tokens(value: Any) -> list[str]:
    text_value = _clean_text(value)
    if not text_value:
        return []
    return [token.strip() for token in SPLIT_SOURCE_RE.split(text_value) if token.strip()]


def _split_ingredient_tokens(value: Any) -> list[str]:
    text_value = _clean_text(_strip_additive_section(value))
    if not text_value:
        return []
    tokens: list[str] = []
    for token in _split_top_level_ingredient_tokens(text_value):
        tokens.extend(_expand_grouped_ingredient_token(token))
    return tokens


PROTEIN_EXPLICIT_MARKERS = (
    "蛋白粉",
    "蛋白质",
    "乳清蛋白",
    "血浆蛋白",
    "血粉",
    "水解蛋白",
    "水解动物蛋白",
    "水解植物蛋白",
)
PROTEIN_ANIMAL_MARKERS = (
    "鸡",
    "鸭",
    "火鸡",
    "鹅",
    "鹌鹑",
    "乳鸽",
    "鸽",
    "牛",
    "羊",
    "鹿",
    "兔",
    "猪",
    "鱼",
    "鳕",
    "鲑",
    "鲱",
    "鲭",
    "鲣",
    "鲔",
    "金枪",
    "三文",
    "沙丁",
    "凤尾",
    "鳀",
    "虾",
    "磷虾",
    "贝",
    "贻贝",
    "蛋",
)
PROTEIN_FORM_MARKERS = (
    "肉",
    "肉粉",
    "鲜肉",
    "冻肉",
    "冻干",
    "肝",
    "心",
    "胗",
    "肾",
    "脾",
    "肺",
    "内脏",
    "鱼粉",
    "虾粉",
    "磷虾粉",
    "全蛋",
    "蛋黄",
    "蛋粉",
    "水解",
)
DIRECT_AQUATIC_PROTEIN_MARKERS = (
    "鱼",
    "鳕",
    "鲑",
    "鲱",
    "鲭",
    "鲣",
    "鲔",
    "金枪",
    "三文",
    "沙丁",
    "凤尾",
    "鳀",
    "虾",
    "磷虾",
    "贝",
    "贻贝",
)
NON_PROTEIN_MARKERS = (
    "油",
    "脂肪",
    "淀粉",
    "甘薯",
    "红薯",
    "紫薯",
    "马铃薯",
    "木薯",
    "南瓜",
    "胡萝卜",
    "苹果",
    "梨",
    "蔓越莓",
    "蓝莓",
    "海带",
    "海藻",
    "纤维",
    "甜菜粕",
    "车前子",
    "菊苣",
    "酵母",
    "维生素",
    "矿物质",
    "牛磺酸",
    "氯化",
    "硫酸",
    "磷酸",
    "碳酸",
)

PLANT_PROTEIN_MARKERS = (
    "豌豆蛋白",
    "马铃薯蛋白",
    "玉米蛋白",
    "玉米蛋白粉",
    "大米蛋白",
    "大米蛋白粉",
    "小麦蛋白",
    "谷朊粉",
    "大豆蛋白",
    "黄豆蛋白",
    "浓缩米蛋白",
    "濃縮米蛋白",
    "植物蛋白",
)
PROTEIN_FORM_SCIENCE_LABELS = {
    "fresh": "鲜肉", "frozen": "冻肉", "meal": "肉粉",
    "hydrolyzed": "水解蛋白", "concentrate": "植物浓缩蛋白",
    "isolate": "植物分离蛋白", "other": "其他蛋白形态",
}


def _load_standard_ingredient_lookup(
    engine: Engine,
    *,
    standard_db: str,
    ingredient_table: str,
    alias_table: str,
) -> dict[str, dict[str, Any]]:
    if not _table_exists(engine, standard_db, ingredient_table):
        return {}
    ingredient_fq = _fq(standard_db, ingredient_table)
    alias_fq = _fq(standard_db, alias_table)
    alias_exists = _table_exists(engine, standard_db, alias_table)
    science_table = "catfood_ingredient_science_profile"
    science_exists = _table_exists(engine, standard_db, science_table)
    science_join = (
        f"LEFT JOIN {_fq(standard_db, science_table)} sp "
        "ON sp.standard_ingredient_id=i.standard_ingredient_id AND sp.science_status='active'"
        if science_exists else ""
    )
    science_select = (
        "sp.domain_attributes_json,sp.profile_version AS science_profile_version,"
        "sp.science_status,sp.nutrition_category AS science_nutrition_category"
        if science_exists else
        "NULL AS domain_attributes_json,NULL AS science_profile_version,NULL AS science_status,"
        "NULL AS science_nutrition_category"
    )
    if alias_exists:
        sql = f"""
        SELECT
          a.alias_names,
          i.standard_ingredient_id,
          i.standard_name,
          i.ingredient_family,
          i.source_type,
          i.animal_source,
          i.primary_nutrition_role,
          {science_select}
        FROM {alias_fq} a
        JOIN {ingredient_fq} i
          ON i.standard_ingredient_id = a.standard_ingredient_id
        {science_join}
        WHERE i.active = 1
        """
    else:
        sql = f"""
        SELECT
          i.standard_name AS alias_name,
          NULL AS normalized_alias,
          1.0 AS confidence,
          i.standard_ingredient_id,
          i.standard_name,
          i.ingredient_family,
          i.source_type,
          i.animal_source,
          i.primary_nutrition_role,
          {science_select}
        FROM {ingredient_fq} i
        {science_join}
        WHERE i.active = 1
        """
    grouped: dict[str, list[dict[str, Any]]] = {}
    with engine.connect() as conn:
        for row in conn.execute(text(sql)).mappings().all():
            item = dict(row)
            raw_science = item.pop("domain_attributes_json", None)
            try:
                item["science_attributes"] = raw_science if isinstance(raw_science, dict) else json.loads(raw_science or "{}")
            except (TypeError, json.JSONDecodeError):
                item["science_attributes"] = {}
            aliases = _split_grouped_alias_names(item.pop("alias_names", None))
            if item.get("standard_name") not in aliases:
                aliases.append(str(item.get("standard_name") or ""))
            for alias_name in aliases:
                alias_key = _normalize_ingredient_key(alias_name)
                if alias_key:
                    grouped.setdefault(str(alias_key), []).append(item)
                literal_key = _normalize_ingredient_literal_key(alias_name)
                if literal_key:
                    grouped.setdefault(f"literal:{literal_key}", []).append(item)
    lookup: dict[str, dict[str, Any]] = {}
    for key, items in grouped.items():
        target_ids = {str(item.get("standard_ingredient_id") or "") for item in items}
        if len(target_ids) != 1:
            continue
        lookup[key] = items[0]
    return lookup


def _match_standard_ingredient(
    raw_name: str,
    lookup: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not lookup:
        return None
    literal_key = _normalize_ingredient_literal_key(raw_name)
    if f"literal:{literal_key}" in lookup:
        return lookup[f"literal:{literal_key}"]
    key = _normalize_ingredient_key(raw_name)
    if key in lookup:
        return lookup[key]
    no_paren = re.sub(r"\([^)]*\)|（[^）]*）", "", raw_name)
    key = _normalize_ingredient_key(no_paren)
    return lookup.get(key)


def _split_concatenated_standard_ingredients(
    raw_name: str,
    lookup: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """Split an unmatched OCR token only when aliases uniquely cover it end-to-end."""
    token = _normalize_ingredient_literal_key(raw_name)
    if len(token) < 4 or not lookup:
        return []

    aliases: dict[str, dict[str, Any]] = {}
    ambiguous_aliases: set[str] = set()
    for lookup_key, ingredient in lookup.items():
        alias = lookup_key.removeprefix("literal:")
        if len(alias) < 2 or alias == token:
            continue
        ingredient_id = str(ingredient.get("standard_ingredient_id") or "")
        existing = aliases.get(alias)
        if existing and str(existing.get("standard_ingredient_id") or "") != ingredient_id:
            ambiguous_aliases.add(alias)
        else:
            aliases[alias] = ingredient
    for alias in ambiguous_aliases:
        aliases.pop(alias, None)

    candidates: list[list[tuple[str, dict[str, Any]]]] = []

    def walk(offset: int, path: list[tuple[str, dict[str, Any]]]) -> None:
        if len(candidates) > 1:
            return
        if offset == len(token):
            if len(path) >= 2:
                candidates.append(path[:])
            return
        for alias in sorted(aliases, key=len, reverse=True):
            if token.startswith(alias, offset):
                path.append((alias, aliases[alias]))
                walk(offset + len(alias), path)
                path.pop()

    walk(0, [])
    return candidates[0] if len(candidates) == 1 else []


def _infer_protein_form(raw_name: Any, standard_name: Any = None) -> Optional[str]:
    text_pool = f"{raw_name or ''} {standard_name or ''}"
    if "水解" in text_pool or "酶解" in text_pool:
        return "水解蛋白"
    if "肉粉" in text_pool or "鱼粉" in text_pool or "虾粉" in text_pool or "磷虾粉" in text_pool:
        return "肉粉"
    if "冻" in text_pool and "冻干" not in text_pool:
        return "冻肉"
    if "鲜" in text_pool or "新鲜" in text_pool:
        return "鲜肉"
    if "蛋" in text_pool:
        return "鲜肉"
    if "肉" in text_pool or "鱼" in text_pool or "虾" in text_pool:
        return "鲜肉"
    return None


def _is_standard_protein_item(item: dict[str, Any]) -> bool:
    raw_name = str(item.get("raw_name") or "")
    standard_name = str(item.get("standard_name") or "")
    source_type = str(item.get("source_type") or "")
    family = str(item.get("ingredient_family") or "")
    role = str(item.get("primary_nutrition_role") or "")
    text_pool = f"{raw_name} {standard_name} {family} {role}"
    explicit_name_pool = f"{raw_name} {standard_name}"
    if any(marker in role for marker in ("碳水", "纤维")) and not any(
        marker in explicit_name_pool for marker in PLANT_PROTEIN_MARKERS
    ):
        return False
    if any(marker in text_pool for marker in PLANT_PROTEIN_MARKERS):
        return True
    if source_type == "animal" and ("蛋白" in role or any(marker in text_pool for marker in PROTEIN_ANIMAL_MARKERS)):
        return _is_protein_ingredient(raw_name) or _is_protein_ingredient(standard_name)
    return _is_protein_ingredient(raw_name) or _is_protein_ingredient(standard_name)


def _is_plant_protein_item(item: dict[str, Any]) -> bool:
    role = str(item.get("primary_nutrition_role") or "")
    explicit_name_pool = " ".join(
        str(item.get(key) or "") for key in ("raw_name", "standard_name")
    )
    if any(marker in role for marker in ("碳水", "纤维")) and not any(
        marker in explicit_name_pool for marker in PLANT_PROTEIN_MARKERS
    ):
        return False
    text_pool = " ".join(
        str(item.get(key) or "")
        for key in ("raw_name", "standard_name", "ingredient_family", "primary_nutrition_role")
    )
    return any(marker in text_pool for marker in PLANT_PROTEIN_MARKERS)


def _feature_rule_matches(item: dict[str, Any], rule: dict[str, Any]) -> bool:
    """Match one DB-backed ingredient feature rule against a standardized item."""
    scope = str(rule.get("match_scope") or "").strip()
    expected = str(rule.get("match_value") or "").strip()
    if not scope or not expected:
        return False

    actual = str(item.get(scope) or "").strip()
    if not actual:
        return False
    excluded = str(rule.get("exclude_value") or "").strip()
    if excluded and excluded.lower() in actual.lower():
        return False
    operator = str(rule.get("match_operator") or "").strip().lower()
    if not operator:
        operator = "contains" if scope in {"raw_name", "standard_name", "primary_nutrition_role"} else "exact"
    if operator == "contains":
        return expected.lower() in actual.lower()
    if operator == "exact":
        return actual.lower() == expected.lower()
    if operator == "regex":
        return re.search(expected, actual, re.I) is not None
    raise ValueError(f"unsupported ingredient feature match operator: {operator!r}")


def _parse_rule_bool(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "是"}:
        return True
    if normalized in {"0", "false", "no", "n", "否"}:
        return False
    raise ValueError(f"invalid boolean ingredient feature rule value: {value!r}")


def _apply_protein_feature_rules(
    item: dict[str, Any],
    feature_rules: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Apply highest-priority protein rules while retaining heuristic fallback values."""
    if not feature_rules:
        return item

    matched_by_key: dict[str, dict[str, Any]] = {}
    ordered_rules = sorted(
        feature_rules,
        key=lambda rule: (-int(rule.get("priority") or 0), int(rule.get("rule_id") or 0)),
    )
    for rule in ordered_rules:
        if str(rule.get("feature_domain") or "") != "protein":
            continue
        feature_key = str(rule.get("dimension_code") or rule.get("feature_key") or "").strip()
        if not feature_key or feature_key in matched_by_key:
            continue
        if _feature_rule_matches(item, rule):
            matched_by_key[feature_key] = rule

    if not matched_by_key:
        return item

    is_protein_rule = matched_by_key.get("is_protein")
    is_plant_rule = matched_by_key.get("is_plant_protein")
    plant_level_rule = matched_by_key.get("plant_protein")
    plant_class_rule = matched_by_key.get("ingredient_plant_protein_class")
    if is_protein_rule:
        item["is_protein"] = _parse_rule_bool(is_protein_rule.get("feature_value"))
    if is_plant_rule:
        item["is_plant_protein"] = _parse_rule_bool(is_plant_rule.get("feature_value"))
    if plant_level_rule:
        item["is_protein"] = True
        item["is_plant_protein"] = True
        item["plant_protein_level"] = plant_level_rule.get("feature_value")
    if plant_class_rule:
        item["is_protein"] = True
        item["is_plant_protein"] = True
        item["plant_protein_class"] = plant_class_rule.get("value_name") or plant_class_rule.get("feature_value")

    form_rule = matched_by_key.get("ingredient_protein_form") or matched_by_key.get("form")
    if form_rule and item.get("is_protein"):
        item["protein_form"] = form_rule.get("value_name") or form_rule.get("feature_value")
    animal_source_rule = matched_by_key.get("animal_source")
    if animal_source_rule and item.get("is_protein"):
        item["animal_source"] = animal_source_rule.get("feature_value")
    if not item.get("is_protein"):
        item["protein_form"] = None
        item["is_plant_protein"] = False

    item["protein_rule_features"] = {
        f"protein.{key}": rule.get("value_name") or rule.get("feature_value")
        for key, rule in matched_by_key.items()
    }
    item["protein_rule_ids"] = [
        int(rule["rule_id"])
        for rule in matched_by_key.values()
        if rule.get("rule_id") is not None
    ]
    return item


def _standardize_ingredient_items(
    ingredient_composition: Any,
    lookup: dict[str, dict[str, Any]] | None = None,
    feature_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    lookup = lookup or {}
    items: list[dict[str, Any]] = []
    expanded_tokens: list[tuple[str, dict[str, Any] | None, bool]] = []
    for token in _split_ingredient_tokens(ingredient_composition):
        matched = _match_standard_ingredient(token, lookup)
        split_items = [] if matched else _split_concatenated_standard_ingredients(token, lookup)
        if split_items:
            expanded_tokens.extend((alias, ingredient, True) for alias, ingredient in split_items)
        else:
            expanded_tokens.append((token, matched, False))

    for index, (token, matched, was_split) in enumerate(expanded_tokens, start=1):
        standard_name = matched.get("standard_name") if matched else None
        science_attributes = dict(matched.get("science_attributes") or {}) if matched else {}
        science_form = str(science_attributes.get("protein_form") or "").strip().lower()
        science_form_label = PROTEIN_FORM_SCIENCE_LABELS.get(science_form)
        item = {
            "position": index,
            "raw_name": token,
            "standard_ingredient_id": matched.get("standard_ingredient_id") if matched else None,
            "standard_name": standard_name,
            "ingredient_family": matched.get("ingredient_family") if matched else None,
            "source_type": matched.get("source_type") if matched else None,
            "animal_source": matched.get("animal_source") if matched else _normalize_source_token(token),
            "primary_nutrition_role": matched.get("primary_nutrition_role") if matched else None,
            "protein_form": science_form_label or _infer_protein_form(token, standard_name),
            "plant_protein_form": science_attributes.get("plant_protein_form"),
            "science_profile_version": matched.get("science_profile_version") if matched else None,
            "science_profile_active": bool(matched and matched.get("science_status") == "active"),
            "science_nutrition_category": matched.get("science_nutrition_category") if matched else None,
            "protein_form_origin": "science_profile" if science_form_label else "legacy_rule",
            "match_method": "standard_alias_compound_split" if was_split else "standard_alias" if matched else "rule_fallback",
            "confidence": min(float(matched.get("confidence") or 1.0), 0.98) if was_split else float(matched.get("confidence") or 1.0) if matched else 0.0,
            "is_protein": False,
            "is_plant_protein": False,
        }
        item["is_protein"] = _is_standard_protein_item(item)
        if item["science_profile_active"]:
            # 生效后的科学属性具有最终裁决权，避免“蛋白锌”等非营养蛋白
            # 因名称命中旧规则而被错误纳入蛋白来源。
            item["is_protein"] = item["science_nutrition_category"] == "protein"
        item["is_plant_protein"] = _is_plant_protein_item(item)
        if not item["is_protein"]:
            item["protein_form"] = None
        _apply_protein_feature_rules(item, feature_rules)
        items.append(item)
    return items


def _join_unique(values: list[Any]) -> Optional[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text_value = _clean_text(value)
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        result.append(text_value)
    return "、".join(result) if result else None


def _infer_main_form(forms: list[Any]) -> Optional[str]:
    cleaned = [_clean_text(form) for form in forms if _clean_text(form)]
    if not cleaned:
        return None
    head_forms = set(cleaned[:2])
    if {"鲜肉", "冻肉"} <= head_forms:
        return "鲜肉/冻肉"
    return cleaned[0]


def _infer_meat_source_complexity(animal_items: list[dict[str, Any]]) -> Optional[str]:
    if not animal_items:
        return None
    source_count = len(
        {
            _clean_text(item.get("animal_source"))
            for item in animal_items
            if _clean_text(item.get("animal_source"))
        }
    )
    category_count = len(
        {
            ANIMAL_SOURCE_LEVEL2_TO_LEVEL1.get(str(item.get("animal_source") or ""))
            for item in animal_items
            if ANIMAL_SOURCE_LEVEL2_TO_LEVEL1.get(str(item.get("animal_source") or ""))
        }
    )
    item_count = len(animal_items)
    if source_count <= 1:
        if item_count <= 1:
            return "单一来源"
        if item_count == 2:
            return "同类双源"
        return "同类多源"
    if category_count <= 1:
        return "同类双源" if source_count == 2 else "同类多源"
    return "跨类双源" if source_count == 2 else "跨类多源"


def _protein_labels_from_standard_items(
    ingredient_composition: Any,
    lookup: dict[str, dict[str, Any]] | None = None,
    feature_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items = _standardize_ingredient_items(ingredient_composition, lookup, feature_rules)
    protein_items = [item for item in items if item["is_protein"]]
    animal_items = [
        item
        for item in protein_items
        if not item["is_plant_protein"] and _clean_text(item.get("animal_source"))
    ]
    plant_items = [item for item in protein_items if item["is_plant_protein"]]
    science_eligible = [item for item in protein_items if item.get("standard_ingredient_id")]
    science_used = [
        item for item in science_eligible
        if item.get("science_profile_active") and item.get("science_nutrition_category") == "protein"
    ]
    science_missing = [
        {"standard_ingredient_id": item.get("standard_ingredient_id"), "name": item.get("standard_name") or item.get("raw_name")}
        for item in science_eligible
        if not (item.get("science_profile_active") and item.get("science_nutrition_category") == "protein")
    ]
    animal_sources = _join_unique([item.get("animal_source") for item in animal_items])
    level1, level2 = _classify_animal_sources(
        animal_sources,
        _join_unique([item.get("raw_name") for item in animal_items]),
    )
    forms = [item.get("protein_form") for item in animal_items if item.get("protein_form")]
    primary_form = _infer_main_form(forms)
    secondary_start = 2 if primary_form == "鲜肉/冻肉" else 1
    secondary_form = _join_unique(forms[secondary_start:]) if len(forms) > secondary_start else None
    primary_species = _clean_text(animal_items[0].get("animal_source")) if animal_items else None
    secondary_species = _join_unique([item.get("animal_source") for item in animal_items[1:]]) if len(animal_items) > 1 else None
    return {
        "standardized_ingredient_items": items,
        "protein_source_details": _join_unique([item.get("raw_name") for item in protein_items]),
        "animal_sources": animal_sources,
        "animal_source_level1_categories": level1,
        "animal_source_level2_sources": level2,
        "primary_meat_source_species": primary_species,
        "secondary_meat_source_species": secondary_species,
        "primary_meat_source_type": primary_form,
        "secondary_meat_source_type": secondary_form,
        "primary_meat_source_count": 1 if primary_species else None,
        "secondary_meat_source_count": len(_split_source_tokens(secondary_species)) if secondary_species else None,
        "meat_source_complexity": _infer_meat_source_complexity(animal_items),
        "plant_protein_labels": _join_unique([item.get("raw_name") for item in plant_items]),
        "protein_source_origin": "science_profile" if science_eligible and len(science_used) == len(science_eligible) else "science_profile_with_legacy_fallback" if science_used else "legacy_rule",
        "science_profile_coverage": round(len(science_used) / len(science_eligible), 4) if science_eligible else 0.0,
        "science_profile_used_count": len(science_used),
        "science_profile_missing": science_missing,
    }


def _is_protein_ingredient(token: str) -> bool:
    compact = re.sub(r"\s+", "", token)
    if not compact:
        return False
    if any(marker in compact for marker in PROTEIN_EXPLICIT_MARKERS):
        return True
    if any(marker in compact for marker in NON_PROTEIN_MARKERS):
        return False
    if any(marker in compact for marker in DIRECT_AQUATIC_PROTEIN_MARKERS):
        return True
    has_animal = any(marker in compact for marker in PROTEIN_ANIMAL_MARKERS)
    has_protein_form = any(marker in compact for marker in PROTEIN_FORM_MARKERS)
    return has_animal and has_protein_form


def _normalize_protein_source_details(ingredient_composition: Any, existing_details: Any) -> Optional[str]:
    ingredient_tokens = _split_ingredient_tokens(ingredient_composition)
    protein_tokens: list[str] = []
    seen: set[str] = set()
    for token in ingredient_tokens:
        if not _is_protein_ingredient(token):
            continue
        dedupe_key = re.sub(r"[\s%％()（）,，、;；]+", "", token).lower()
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        protein_tokens.append(token)
    if protein_tokens:
        return "、".join(protein_tokens)

    fallback_tokens = [
        token
        for token in _split_ingredient_tokens(existing_details)
        if _is_protein_ingredient(token)
    ]
    return "、".join(dict.fromkeys(fallback_tokens)) or None


def _normalize_source_token(token: str) -> Optional[str]:
    cleaned = token.strip()
    if not cleaned:
        return None

    if "鸡蛋" in cleaned:
        return "鸡蛋"
    if "鸭蛋" in cleaned:
        return "鸭蛋"

    if "火鸡" in cleaned:
        return "火鸡"
    if "鹌鹑" in cleaned:
        return "鹌鹑"
    if "乳鸽" in cleaned:
        return "乳鸽"
    if "鸽" in cleaned:
        return "乳鸽"
    if "鸭" in cleaned and "蛋" not in cleaned:
        return "鸭"
    if "鸡" in cleaned and "蛋" not in cleaned:
        return "鸡"

    if "鱼" in cleaned:
        return "鱼"

    if "牛" in cleaned:
        return "牛"
    if "山羊" in cleaned:
        return "羊"
    if "羊" in cleaned:
        return "羊"
    if "鹿" in cleaned:
        return "鹿"
    if "兔" in cleaned:
        return "兔"
    if "猪" in cleaned:
        return "猪"

    return None


def _extract_fish_subtypes(*, animal_sources: Any, protein_source_details: Any) -> list[str]:
    text_pool = " ".join(
        [
            _clean_text(animal_sources) or "",
            _clean_text(protein_source_details) or "",
        ]
    )
    found: list[str] = []
    if "鲱" in text_pool:
        found.append("鲱鱼")
    if "鳕" in text_pool:
        found.append("鳕鱼")
    if "比目" in text_pool or "鳎" in text_pool:
        found.append("比目鱼")
    if "岩鱼" in text_pool:
        found.append("岩鱼")
    if "鳟" in text_pool:
        found.append("鳟鱼")
    if "海洋鱼" in text_pool:
        found.append("海洋鱼")
    if "金枪" in text_pool or "吞拿" in text_pool:
        found.append("金枪鱼")
    if "三文鱼粉" in text_pool or "三文鱼" in text_pool or "三文" in text_pool or ("鲑鱼" in text_pool and "白鲑" not in text_pool):
        found.append("三文鱼")
    if "白鱼粉" in text_pool or "白鱼" in text_pool or "白鲑" in text_pool:
        found.append("白鱼")
    if "鲭鱼粉" in text_pool or "鲭鱼" in text_pool:
        found.append("鲭鱼")
    if "沙丁鱼粉" in text_pool or "沙丁鱼" in text_pool:
        found.append("沙丁鱼")
    if "马鲛鱼" in text_pool:
        found.append("马鲛鱼")
    if "白斑狗鱼" in text_pool:
        found.append("白斑狗鱼")
    if "竹荚鱼" in text_pool:
        found.append("竹荚鱼")
    if "鲻鱼" in text_pool:
        found.append("鲻鱼")
    if "凤尾鱼" in text_pool or "鳀鱼" in text_pool:
        found.append("凤尾鱼")
    unique: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return sorted(unique, key=lambda item: FISH_SUBTYPE_ORDER.index(item))


def _classify_animal_sources(animal_sources: Any, protein_source_details: Any) -> tuple[Optional[str], Optional[str]]:
    level2_tokens: list[str] = []
    seen_level2: set[str] = set()
    level1_set: set[str] = set()
    has_fish_generic = False

    source_tokens = _split_source_tokens(protein_source_details)
    if not source_tokens:
        source_tokens = _split_source_tokens(animal_sources)

    for raw_token in source_tokens:
        normalized = _normalize_source_token(raw_token)
        if not normalized:
            continue
        if normalized == "鱼":
            has_fish_generic = True
            continue
        if normalized not in seen_level2:
            seen_level2.add(normalized)
            level2_tokens.append(normalized)
        level1 = ANIMAL_SOURCE_LEVEL2_TO_LEVEL1.get(normalized)
        if level1:
            level1_set.add(level1)

    fish_subtypes = _extract_fish_subtypes(
        animal_sources=animal_sources,
        protein_source_details=protein_source_details,
    )
    if fish_subtypes:
        level1_set.add("鱼类")
        for fish in fish_subtypes:
            if fish not in seen_level2:
                seen_level2.add(fish)
                level2_tokens.append(fish)
    elif has_fish_generic:
        if "鱼" not in seen_level2:
            seen_level2.add("鱼")
            level2_tokens.append("鱼")
        level1_set.add("鱼类")

    ordered_level1 = [category for category in ANIMAL_SOURCE_LEVEL1_ORDER if category in level1_set]
    level1_text = "、".join(ordered_level1) if ordered_level1 else None
    level2_text = "、".join(level2_tokens) if level2_tokens else None
    return level1_text, level2_text


def _count_multi_values(value: Any) -> Optional[int]:
    text_value = _clean_text(value)
    if not text_value:
        return None
    parts = [part.strip() for part in re.split(r"\s*\|\s*", text_value) if part.strip()]
    if not parts:
        return None
    return len(dict.fromkeys(parts))


def _limit_table_name(base: str, suffix: str, batch_id: str) -> str:
    max_len = 64
    reserved = len(suffix) + len(batch_id) + 2
    trimmed = base[: max_len - reserved]
    return f"{trimmed}__{suffix}_{batch_id}"


def make_engine(args: argparse.Namespace) -> Engine:
    if args.dsn:
        return create_engine(args.dsn, future=True)

    project_root = Path(__file__).resolve().parents[3]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        from app_config import get_feature_mysql_config

        project_cfg = get_feature_mysql_config()
    except Exception:
        project_cfg = {}

    host = args.host or os.getenv("MYSQL_HOST") or project_cfg.get("host") or "127.0.0.1"
    port = int(args.port or os.getenv("MYSQL_PORT") or project_cfg.get("port") or 3306)
    user = args.user or os.getenv("MYSQL_USER") or project_cfg.get("user")
    password = args.password if args.password is not None else os.getenv("MYSQL_PASSWORD", project_cfg.get("password"))
    charset = args.charset or os.getenv("MYSQL_CHARSET") or project_cfg.get("charset") or "utf8mb4"

    if not user:
        raise ValueError("missing MySQL user; pass --user or --dsn")

    url = f"mysql+pymysql://{user}:{password or ''}@{host}:{port}/?charset={charset}"
    return create_engine(url, future=True)


def _table_exists(engine: Engine, db_name: str, table_name: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                    """
                ),
                {"schema_name": db_name, "table_name": table_name},
            ).scalar()
        )


def _target_table_ddl(table_name: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS `{table_name}` (
      `source_id` BIGINT NOT NULL,
      `formula_id` BIGINT UNSIGNED DEFAULT NULL,
      `product_key` VARCHAR(255) DEFAULT NULL,
      `guarantee_product_id` BIGINT DEFAULT NULL,
      `brand_name` VARCHAR(255) DEFAULT NULL,
      `product_name` VARCHAR(255) DEFAULT NULL,
      `animal_sources` TEXT,
      `animal_source_level1_categories` VARCHAR(255) DEFAULT NULL,
      `animal_source_level2_sources` TEXT,
      `protein_source_details` TEXT,
      `primary_meat_source_species` VARCHAR(255) DEFAULT NULL,
      `secondary_meat_source_species` VARCHAR(255) DEFAULT NULL,
      `primary_meat_source_type` VARCHAR(255) DEFAULT NULL,
      `secondary_meat_source_type` VARCHAR(255) DEFAULT NULL,
      `meat_source_complexity` VARCHAR(255) DEFAULT NULL,
      `primary_meat_source_count` INT DEFAULT NULL,
      `secondary_meat_source_count` INT DEFAULT NULL,
      `protein_source_origin` VARCHAR(255) DEFAULT NULL,
      `plant_protein_labels` TEXT,
      `guarantee_crude_protein_metric_name` VARCHAR(100) DEFAULT NULL,
      `guarantee_crude_protein_value` DECIMAL(8,2) DEFAULT NULL,
      `guarantee_crude_protein_unit` VARCHAR(50) DEFAULT NULL,
      `aggregated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (`source_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """


def _ensure_target_columns(conn, *, target_db: str, target_table: str) -> None:
    existing = {
        row["COLUMN_NAME"]
        for row in conn.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                """
            ),
            {"schema_name": target_db, "table_name": target_table},
        ).mappings().all()
    }
    if "meat_source_complexity" not in existing:
        conn.execute(
            text(
                f"ALTER TABLE {_fq(target_db, target_table)} "
                "ADD COLUMN `meat_source_complexity` VARCHAR(255) DEFAULT NULL "
                "AFTER `secondary_meat_source_type`"
            )
        )


def load_rows(
    engine: Engine,
    *,
    source_db: str,
    source_table: str,
    parsed_db: str,
    parsed_table: str,
    feature_db: str,
    feature_table: str,
    guarantee_db: str,
    product_info_table: str,
    product_guarantee_table: str,
) -> list[dict[str, Any]]:
    source_fq = _fq(source_db, source_table)
    parsed_fq = _fq(parsed_db, parsed_table)
    product_info_fq = _fq(guarantee_db, product_info_table)
    product_guarantee_fq = _fq(guarantee_db, product_guarantee_table)

    with engine.connect() as conn:
        feature_table_exists = bool(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                    """
                ),
                {"schema_name": feature_db, "table_name": feature_table},
            ).scalar()
        )

    if feature_table_exists:
        feature_join_sql = f"""
        LEFT JOIN {_fq(feature_db, feature_table)} f
          ON f.product_id = p.product_key
        """
        feature_select_sql = """
          f.brand AS feature_brand,
          f.product_name AS feature_product_name,
          f.primary_meat_source_count AS feature_primary_meat_source_count,
          f.secondary_meat_source_count AS feature_secondary_meat_source_count,
          f.primary_meat_source_type AS feature_primary_meat_source_type,
          f.secondary_meat_source_type AS feature_secondary_meat_source_type,
          f.crude_protein_pct AS feature_crude_protein_pct,
        """
    else:
        print(
            f"feature table {feature_db}.{feature_table} not found; "
            "using source labels and guarantee tables only"
        )
        feature_join_sql = ""
        feature_select_sql = """
          NULL AS feature_brand,
          NULL AS feature_product_name,
          NULL AS feature_primary_meat_source_count,
          NULL AS feature_secondary_meat_source_count,
          NULL AS feature_primary_meat_source_type,
          NULL AS feature_secondary_meat_source_type,
          NULL AS feature_crude_protein_pct,
        """

    sql = f"""
    WITH ranked_guarantee AS (
      SELECT
        g.source_id,
        g.product_id,
        g.metric_name,
        g.metric_value,
        g.metric_unit,
        ROW_NUMBER() OVER (
          PARTITION BY g.source_id
          ORDER BY
            CASE WHEN g.basis = '干物质' THEN 1 ELSE 0 END DESC,
            g.id DESC
        ) AS rn
      FROM {product_guarantee_fq} g
      WHERE g.metric_name = '粗蛋白'
    )
    SELECT
      p.source_id,
      p.product_key,
      p.brand,
      p.product_name,
      p.animal_sources,
      p.protein_source_details,
      parsed.ingredient_composition,
      p.primary_meat_source_species,
      p.secondary_meat_source_species,
      p.primary_meat_source_type,
      p.secondary_meat_source_type,
      p.protein_source_origin,
      p.plant_protein_labels,
      {feature_select_sql}
      i.id AS product_info_id,
      rg.product_id AS guarantee_product_id,
      rg.metric_name AS guarantee_metric_name,
      rg.metric_value AS guarantee_metric_value,
      rg.metric_unit AS guarantee_metric_unit
    FROM {source_fq} p
    INNER JOIN {parsed_fq} parsed
      ON parsed.id = p.parsed_row_id
     AND parsed.ingredient_composition IS NOT NULL
     AND TRIM(parsed.ingredient_composition) <> ''
    {feature_join_sql}
    LEFT JOIN {product_info_fq} i
      ON i.source_id = p.source_id
    LEFT JOIN ranked_guarantee rg
      ON rg.source_id = p.source_id
     AND rg.rn = 1
    ORDER BY p.source_id ASC
    """
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql)).mappings().all()]


def load_existing_aggregate_rows(
    engine: Engine,
    *,
    parsed_db: str,
    parsed_table: str,
    target_db: str,
    target_table: str,
) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        existing_columns = {
            row["COLUMN_NAME"]
            for row in conn.execute(text("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA=:schema_name AND TABLE_NAME=:table_name
            """), {"schema_name": target_db, "table_name": target_table}).mappings().all()
        }

    def target_column(name: str, alias: str | None = None) -> str:
        output_name = alias or name
        return f"t.`{name}` AS `{output_name}`" if name in existing_columns else f"NULL AS `{output_name}`"

    sql = f"""
    SELECT
      t.source_id,
      {target_column('formula_id')},
      t.product_key,
      {target_column('guarantee_product_id')},
      t.brand_name AS brand,
      t.product_name,
      t.animal_sources,
      t.protein_source_details,
      parsed.ingredient_composition,
      {target_column('primary_meat_source_species')},
      {target_column('secondary_meat_source_species')},
      t.primary_meat_source_type,
      t.secondary_meat_source_type,
      {target_column('primary_meat_source_count', 'feature_primary_meat_source_count')},
      {target_column('secondary_meat_source_count', 'feature_secondary_meat_source_count')},
      {target_column('protein_source_origin')},
      t.plant_protein_labels,
      {target_column('guarantee_crude_protein_metric_name', 'guarantee_metric_name')},
      {target_column('guarantee_crude_protein_value', 'guarantee_metric_value')},
      {target_column('guarantee_crude_protein_unit', 'guarantee_metric_unit')}
    FROM {_fq(target_db, target_table)} t
    INNER JOIN {_fq(parsed_db, parsed_table)} parsed
      ON parsed.source_id = t.source_id
     AND parsed.ingredient_composition IS NOT NULL
     AND TRIM(parsed.ingredient_composition) <> ''
    ORDER BY t.source_id ASC
    """
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql)).mappings().all()]


def _load_labeling_helpers():
    if not (CSV_LABELING_PROJECT / "src").exists():
        raise FileNotFoundError(f"missing csv labeling project: {CSV_LABELING_PROJECT}")
    project_text = str(CSV_LABELING_PROJECT)
    if project_text not in sys.path:
        sys.path.insert(0, project_text)

    try:
        from src.parse_catfood_ingredient_types import (  # type: ignore
            _build_product_key,
            _build_protein_detail_rows,
            _build_protein_label_row,
            _classify_ingredient_composition,
            _composition_cache_key,
            _resolve_openai_cfg,
        )
    except Exception as exc:  # pragma: no cover - depends on sibling project imports
        return _fallback_labeling_helpers(exc)

    return {
        "build_product_key": _build_product_key,
        "build_protein_detail_rows": _build_protein_detail_rows,
        "build_protein_label_row": _build_protein_label_row,
        "classify_ingredient_composition": _classify_ingredient_composition,
        "composition_cache_key": _composition_cache_key,
        "resolve_openai_cfg": _resolve_openai_cfg,
    }


def _fallback_labeling_helpers(import_error: Exception) -> dict[str, Any]:
    def build_product_key(brand: Any, product_name: Any, source_id: Any) -> str:
        return build_corrected_product_key(_clean_text(brand), _clean_text(product_name)) or f"source:{source_id}"

    def composition_cache_key(ingredient_composition: Any) -> str:
        return re.sub(r"\s+", "", str(ingredient_composition or "")).lower()

    def resolve_openai_cfg(_: Any = None) -> dict[str, Any]:
        return {"fallback_reason": str(import_error)}

    def classify_ingredient_composition(ingredient_composition: str, _: dict[str, Any]) -> dict[str, Any]:
        text_value = str(ingredient_composition or "")
        animal_tokens: list[str] = []
        detail_tokens: list[str] = []
        plant_tokens: list[str] = []
        for token in re.split(r"[、,，;；\n]+", text_value):
            cleaned = token.strip(" \t\r\n。.")
            if not cleaned:
                continue
            source = _normalize_source_token(cleaned)
            if source and source not in animal_tokens:
                animal_tokens.append(source)
            if source and cleaned not in detail_tokens:
                detail_tokens.append(cleaned)
            if any(marker in cleaned for marker in ("豌豆", "大豆", "玉米蛋白", "小麦蛋白", "马铃薯蛋白")):
                plant_tokens.append(cleaned)
        primary = animal_tokens[0] if animal_tokens else None
        secondary = animal_tokens[1] if len(animal_tokens) > 1 else None
        return {
            "animal_sources": "、".join(animal_tokens) or None,
            "protein_source_details": "、".join(detail_tokens) or None,
            "primary_meat_source_species": primary,
            "secondary_meat_source_species": secondary,
            "primary_meat_source_type": "鲜肉" if primary and "鲜" in text_value else None,
            "secondary_meat_source_type": None,
            "protein_source_origin": "ingredient_rule_fallback",
            "plant_protein_labels": "、".join(plant_tokens) or None,
        }

    def build_protein_detail_rows(
        row: dict[str, Any],
        ingredient_composition: str,
        classified: dict[str, Any],
        batch_id: str,
    ) -> list[dict[str, Any]]:
        details = _split_source_tokens(classified.get("protein_source_details"))
        return [
            {
                "source_id": row.get("source_id"),
                "ingredient_name": detail,
                "batch_id": batch_id,
            }
            for detail in details
        ]

    def build_protein_label_row(
        *,
        row: dict[str, Any],
        ingredient_composition: str,
        classified: dict[str, Any],
        batch_id: str,
        protein_feature_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "source_id": row.get("source_id"),
            "image_name": row.get("image_name"),
            "image_path": row.get("image_path"),
            "brand": row.get("brand"),
            "product_name": row.get("product_name"),
            "product_key": build_product_key(row.get("brand"), row.get("product_name"), row.get("source_id")),
            "ingredient_composition": ingredient_composition,
            "animal_sources": classified.get("animal_sources"),
            "protein_source_details": classified.get("protein_source_details"),
            "primary_meat_source_species": classified.get("primary_meat_source_species"),
            "secondary_meat_source_species": classified.get("secondary_meat_source_species"),
            "primary_meat_source_type": classified.get("primary_meat_source_type"),
            "secondary_meat_source_type": classified.get("secondary_meat_source_type"),
            "protein_source_origin": classified.get("protein_source_origin"),
            "plant_protein_labels": classified.get("plant_protein_labels"),
            "batch_id": batch_id,
        }

    return {
        "build_product_key": build_product_key,
        "build_protein_detail_rows": build_protein_detail_rows,
        "build_protein_label_row": build_protein_label_row,
        "classify_ingredient_composition": classify_ingredient_composition,
        "composition_cache_key": composition_cache_key,
        "resolve_openai_cfg": resolve_openai_cfg,
    }


def load_rows_from_parsed_incremental(
    engine: Engine,
    *,
    parsed_db: str,
    parsed_table: str,
    feature_db: str,
    feature_table: str,
    guarantee_db: str,
    product_info_table: str,
    product_guarantee_table: str,
    target_db: str,
    target_table: str,
    limit: int,
    concurrency: int,
) -> list[dict[str, Any]]:
    helpers = _load_labeling_helpers()
    parsed_fq = _fq(parsed_db, parsed_table)
    target_fq = _fq(target_db, target_table)
    product_info_fq = _fq(guarantee_db, product_info_table)
    product_guarantee_fq = _fq(guarantee_db, product_guarantee_table)
    target_exists = _table_exists(engine, target_db, target_table)
    feature_table_exists = _table_exists(engine, feature_db, feature_table)

    target_filter = (
        f"""
      AND (
        NOT EXISTS (
          SELECT 1
          FROM {target_fq} t
          WHERE t.source_id = p.source_id
        )
        OR EXISTS (
          SELECT 1
          FROM {target_fq} t
          WHERE t.source_id = p.source_id
            AND (
              t.aggregated_at IS NULL
              OR p.updated_ts > t.aggregated_at
            )
        )
        OR EXISTS (
          SELECT 1
          FROM {target_fq} t
          INNER JOIN {product_guarantee_fq} g
            ON (
              g.source_id = p.source_id
              OR (
                p.merged_source_ids IS NOT NULL
                AND FIND_IN_SET(CAST(g.source_id AS CHAR), p.merged_source_ids) > 0
              )
            )
           AND g.metric_name = '粗蛋白'
          WHERE t.source_id = p.source_id
            AND (
              t.guarantee_crude_protein_value IS NULL
              OR t.aggregated_at IS NULL
              OR g.updated_at > t.aggregated_at
            )
        )
      )
        """
        if target_exists
        else ""
    )
    limit_sql = "LIMIT :limit_value" if limit > 0 else ""
    if feature_table_exists:
        feature_join_sql = f"""
        LEFT JOIN {_fq(feature_db, feature_table)} f
          ON f.product_id = :product_key
        """
        feature_select_sql = """
          f.brand AS feature_brand,
          f.product_name AS feature_product_name,
          f.primary_meat_source_count AS feature_primary_meat_source_count,
          f.secondary_meat_source_count AS feature_secondary_meat_source_count,
          f.primary_meat_source_type AS feature_primary_meat_source_type,
          f.secondary_meat_source_type AS feature_secondary_meat_source_type,
          f.crude_protein_pct AS feature_crude_protein_pct,
        """
    else:
        feature_join_sql = ""
        feature_select_sql = """
          NULL AS feature_brand,
          NULL AS feature_product_name,
          NULL AS feature_primary_meat_source_count,
          NULL AS feature_secondary_meat_source_count,
          NULL AS feature_primary_meat_source_type,
          NULL AS feature_secondary_meat_source_type,
          NULL AS feature_crude_protein_pct,
        """

    parsed_sql = f"""
    SELECT p.id, p.formula_id, p.source_id, p.image_name, p.image_path, p.brand, p.product_name, p.ingredient_composition, p.merged_source_ids
    FROM {parsed_fq} p
    WHERE p.source_id IS NOT NULL
      AND p.ingredient_composition IS NOT NULL
      AND TRIM(p.ingredient_composition) <> ''
      {target_filter}
    ORDER BY p.id ASC
    {limit_sql}
    """
    params = {"limit_value": int(limit)} if limit > 0 else {}
    with engine.connect() as conn:
        parsed_rows = [dict(row) for row in conn.execute(text(parsed_sql), params).mappings().all()]

    if not parsed_rows:
        return []

    grouped_rows: dict[str, dict[str, Any]] = {}
    for row in parsed_rows:
        ingredient_composition = str(row.get("ingredient_composition") or "").strip()
        cache_key = helpers["composition_cache_key"](ingredient_composition)
        grouped_rows.setdefault(
            cache_key,
            {"ingredient_composition": ingredient_composition, "rows": []},
        )["rows"].append(row)

    batch_id = uuid.uuid4().hex[:12]
    resolved_openai_cfg = helpers["resolve_openai_cfg"](None)
    label_rows: list[dict[str, Any]] = []
    max_workers = min(max(1, int(concurrency)), len(grouped_rows))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                helpers["classify_ingredient_composition"],
                str(group["ingredient_composition"] or ""),
                resolved_openai_cfg,
            ): cache_key
            for cache_key, group in grouped_rows.items()
        }
        for future in concurrent.futures.as_completed(future_map):
            cache_key = future_map[future]
            classified = future.result()
            group = grouped_rows[cache_key]
            ingredient_composition = str(group["ingredient_composition"] or "")
            for row in group["rows"]:
                protein_feature_items = helpers["build_protein_detail_rows"](
                    row,
                    ingredient_composition,
                    classified,
                    batch_id,
                )
                label_row = helpers["build_protein_label_row"](
                    row=row,
                    ingredient_composition=ingredient_composition,
                    classified=classified,
                    batch_id=batch_id,
                    protein_feature_items=protein_feature_items,
                )
                label_row["formula_id"] = row.get("formula_id")
                label_row["merged_source_ids"] = row.get("merged_source_ids")
                label_rows.append(label_row)

    enrich_sql = f"""
    WITH ranked_guarantee AS (
      SELECT
        g.source_id,
        g.product_id,
        g.metric_name,
        g.metric_value,
        g.metric_unit,
        ROW_NUMBER() OVER (
          PARTITION BY g.source_id
          ORDER BY
            CASE WHEN g.basis = '干物质' THEN 1 ELSE 0 END DESC,
            g.id DESC
        ) AS rn
      FROM {product_guarantee_fq} g
      WHERE g.metric_name = '粗蛋白'
    ),
    ranked_info AS (
      SELECT
        i.id,
        i.source_id,
        ROW_NUMBER() OVER (
          ORDER BY
            CASE WHEN rg.product_id IS NOT NULL THEN 1 ELSE 0 END DESC,
            CASE WHEN i.source_id = :source_id THEN 1 ELSE 0 END DESC,
            i.id DESC
        ) AS rn
      FROM {product_info_fq} i
      LEFT JOIN ranked_guarantee rg
        ON rg.source_id = i.source_id
       AND rg.rn = 1
      WHERE i.source_id = :source_id
         OR (
           :merged_source_ids IS NOT NULL
           AND FIND_IN_SET(CAST(i.source_id AS CHAR), :merged_source_ids) > 0
         )
    )
    SELECT
      {feature_select_sql}
      ri.id AS product_info_id,
      rg.product_id AS guarantee_product_id,
      rg.metric_name AS guarantee_metric_name,
      rg.metric_value AS guarantee_metric_value,
      rg.metric_unit AS guarantee_metric_unit
    FROM (SELECT :source_id AS source_id, :product_key AS product_key, :merged_source_ids AS merged_source_ids) p
    {feature_join_sql}
    LEFT JOIN ranked_info ri
      ON ri.rn = 1
    LEFT JOIN ranked_guarantee rg
      ON (
        rg.source_id = p.source_id
        OR (
          p.merged_source_ids IS NOT NULL
          AND FIND_IN_SET(CAST(rg.source_id AS CHAR), p.merged_source_ids) > 0
        )
      )
     AND rg.rn = 1
    ORDER BY
      CASE WHEN rg.product_id IS NOT NULL THEN 1 ELSE 0 END DESC,
      CASE WHEN rg.source_id = p.source_id THEN 1 ELSE 0 END DESC
    LIMIT 1
    """

    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for label_row in label_rows:
            product_key = label_row.get("product_key") or helpers["build_product_key"](
                label_row.get("brand"),
                label_row.get("product_name"),
                label_row.get("source_id"),
            )
            enriched = dict(
                conn.execute(
                    text(enrich_sql),
                    {
                        "source_id": label_row["source_id"],
                        "product_key": product_key,
                        "merged_source_ids": _clean_text(label_row.get("merged_source_ids")),
                    },
                ).mappings().first()
                or {}
            )
            out.append({**label_row, **enriched})
    return out


def transform_rows(
    rows: list[dict[str, Any]],
    standard_lookup: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        standard_labels = (
            _protein_labels_from_standard_items(row.get("ingredient_composition"), standard_lookup)
            if standard_lookup
            else {}
        )
        protein_source_details = _normalize_protein_source_details(
            row.get("ingredient_composition"),
            row.get("protein_source_details"),
        )
        protein_source_details = standard_labels.get("protein_source_details") or protein_source_details
        level1_categories, level2_sources = _classify_animal_sources(
            row.get("animal_sources"),
            protein_source_details,
        )
        level1_categories = standard_labels.get("animal_source_level1_categories") or level1_categories
        level2_sources = standard_labels.get("animal_source_level2_sources") or level2_sources
        primary_species = (
            _clean_text(standard_labels.get("primary_meat_source_species"))
            or _clean_text(row.get("primary_meat_source_species"))
        )
        secondary_species = (
            _clean_text(standard_labels.get("secondary_meat_source_species"))
            or _clean_text(row.get("secondary_meat_source_species"))
        )
        primary_type = (
            _clean_text(standard_labels.get("primary_meat_source_type"))
            or _clean_text(row.get("primary_meat_source_type"))
            or _clean_text(row.get("feature_primary_meat_source_type"))
        )
        secondary_type = (
            _clean_text(standard_labels.get("secondary_meat_source_type"))
            or _clean_text(row.get("secondary_meat_source_type"))
            or _clean_text(row.get("feature_secondary_meat_source_type"))
        )

        primary_count = standard_labels.get("primary_meat_source_count") or row.get("feature_primary_meat_source_count")
        if primary_count is None:
            primary_count = _count_multi_values(primary_species)

        secondary_count = standard_labels.get("secondary_meat_source_count") or row.get("feature_secondary_meat_source_count")
        if secondary_count is None:
            secondary_count = _count_multi_values(secondary_species)

        guarantee_value = row.get("guarantee_metric_value")
        guarantee_metric_name = _clean_text(row.get("guarantee_metric_name"))
        guarantee_metric_unit = _clean_text(row.get("guarantee_metric_unit"))
        if guarantee_value is None and row.get("feature_crude_protein_pct") is not None:
            guarantee_value = row.get("feature_crude_protein_pct")
            guarantee_metric_name = "粗蛋白"
            guarantee_metric_unit = "%"

        brand_name = correct_brand(
            _clean_text(row.get("brand")) or _clean_text(row.get("feature_brand")),
            _clean_text(row.get("product_name")) or _clean_text(row.get("feature_product_name")),
            row.get("image_name"),
            row.get("image_path"),
        )
        product_name = _clean_text(row.get("product_name")) or _clean_text(row.get("feature_product_name"))

        out.append(
            {
                "source_id": int(row["source_id"]),
                "formula_id": int(row["formula_id"]) if row.get("formula_id") is not None else None,
                "product_key": build_corrected_product_key(brand_name, product_name),
                "guarantee_product_id": row.get("product_info_id") or row.get("guarantee_product_id"),
                "brand_name": brand_name,
                "product_name": product_name,
                "animal_sources": standard_labels.get("animal_sources") or level2_sources or _clean_text(row.get("animal_sources")),
                "animal_source_level1_categories": level1_categories,
                "animal_source_level2_sources": level2_sources,
                "protein_source_details": protein_source_details,
                "primary_meat_source_species": primary_species,
                "secondary_meat_source_species": secondary_species,
                "primary_meat_source_type": primary_type,
                "secondary_meat_source_type": secondary_type,
                "meat_source_complexity": standard_labels.get("meat_source_complexity") or _clean_text(row.get("meat_source_complexity")) or _clean_text(row.get("source_complexity_label")),
                "primary_meat_source_count": primary_count,
                "secondary_meat_source_count": secondary_count,
                "protein_source_origin": standard_labels.get("protein_source_origin") or _clean_text(row.get("protein_source_origin")),
                "plant_protein_labels": standard_labels.get("plant_protein_labels") or _clean_text(row.get("plant_protein_labels")),
                "science_profile_coverage": standard_labels.get("science_profile_coverage", 0.0),
                "science_profile_used_count": standard_labels.get("science_profile_used_count", 0),
                "science_profile_missing": standard_labels.get("science_profile_missing", []),
                "guarantee_crude_protein_metric_name": guarantee_metric_name,
                "guarantee_crude_protein_value": guarantee_value,
                "guarantee_crude_protein_unit": guarantee_metric_unit,
            }
        )
    return out


def write_rows(
    engine: Engine,
    *,
    target_db: str,
    target_table: str,
    rows: list[dict[str, Any]],
    keep_backup: bool,
    replace_existing: bool = True,
) -> tuple[str, Optional[str]]:
    batch_id = uuid.uuid4().hex[:12]
    target_fq = _fq(target_db, target_table)

    insert_stmt = text(
        f"""
        INSERT INTO {target_fq} (
          source_id,
          formula_id,
          product_key,
          guarantee_product_id,
          brand_name,
          product_name,
          animal_sources,
          animal_source_level1_categories,
          animal_source_level2_sources,
          protein_source_details,
          primary_meat_source_species,
          secondary_meat_source_species,
          primary_meat_source_type,
          secondary_meat_source_type,
          meat_source_complexity,
          primary_meat_source_count,
          secondary_meat_source_count,
          protein_source_origin,
          plant_protein_labels,
          guarantee_crude_protein_metric_name,
          guarantee_crude_protein_value,
          guarantee_crude_protein_unit
        ) VALUES (
          :source_id,
          :formula_id,
          :product_key,
          :guarantee_product_id,
          :brand_name,
          :product_name,
          :animal_sources,
          :animal_source_level1_categories,
          :animal_source_level2_sources,
          :protein_source_details,
          :primary_meat_source_species,
          :secondary_meat_source_species,
          :primary_meat_source_type,
          :secondary_meat_source_type,
          :meat_source_complexity,
          :primary_meat_source_count,
          :secondary_meat_source_count,
          :protein_source_origin,
          :plant_protein_labels,
          :guarantee_crude_protein_metric_name,
          :guarantee_crude_protein_value,
          :guarantee_crude_protein_unit
        )
        ON DUPLICATE KEY UPDATE
          formula_id = VALUES(formula_id),
          product_key = VALUES(product_key),
          guarantee_product_id = VALUES(guarantee_product_id),
          brand_name = VALUES(brand_name),
          product_name = VALUES(product_name),
          animal_sources = VALUES(animal_sources),
          animal_source_level1_categories = VALUES(animal_source_level1_categories),
          animal_source_level2_sources = VALUES(animal_source_level2_sources),
          protein_source_details = VALUES(protein_source_details),
          primary_meat_source_species = VALUES(primary_meat_source_species),
          secondary_meat_source_species = VALUES(secondary_meat_source_species),
          primary_meat_source_type = VALUES(primary_meat_source_type),
          secondary_meat_source_type = VALUES(secondary_meat_source_type),
          meat_source_complexity = VALUES(meat_source_complexity),
          primary_meat_source_count = VALUES(primary_meat_source_count),
          secondary_meat_source_count = VALUES(secondary_meat_source_count),
          protein_source_origin = VALUES(protein_source_origin),
          plant_protein_labels = VALUES(plant_protein_labels),
          guarantee_crude_protein_metric_name = VALUES(guarantee_crude_protein_metric_name),
          guarantee_crude_protein_value = VALUES(guarantee_crude_protein_value),
          guarantee_crude_protein_unit = VALUES(guarantee_crude_protein_unit),
          aggregated_at = CURRENT_TIMESTAMP
        """
    )

    with engine.begin() as conn:
        conn.execute(text(_target_table_ddl(target_table).replace(f"`{target_table}`", f"{target_fq}", 1)))
        _ensure_target_columns(conn, target_db=target_db, target_table=target_table)
        if keep_backup:
            backup_table = _limit_table_name(target_table, "bak", batch_id)
            backup_fq = _fq(target_db, backup_table)
            conn.execute(text(f"DROP TABLE IF EXISTS {backup_fq}"))
            conn.execute(text(f"CREATE TABLE {backup_fq} LIKE {target_fq}"))
            conn.execute(text(f"INSERT INTO {backup_fq} SELECT * FROM {target_fq}"))
        if replace_existing:
            conn.execute(text(f"DELETE FROM {target_fq}"))
        if rows:
            conn.execute(insert_stmt, rows)

    return batch_id, backup_table if keep_backup else None


COMPARISON_FIELDS = (
    "animal_sources", "animal_source_level1_categories", "animal_source_level2_sources",
    "protein_source_details", "primary_meat_source_type", "secondary_meat_source_type",
    "meat_source_complexity", "plant_protein_labels",
)


def write_science_migration_comparison(
    engine: Engine, *, target_db: str, current_table: str,
    comparison_table: str, science_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    comparison_fq = _fq(target_db, comparison_table)
    current_fq = _fq(target_db, current_table)
    with engine.begin() as conn:
        current_rows = {
            int(row["formula_id"]): dict(row)
            for row in conn.execute(text(f"SELECT * FROM {current_fq}")).mappings().all()
            if row.get("formula_id") is not None
        }
        conn.execute(text(f"DROP TABLE IF EXISTS {comparison_fq}"))
        conn.execute(text(f"""
            CREATE TABLE {comparison_fq} (
              comparison_id BIGINT NOT NULL AUTO_INCREMENT,
              source_id BIGINT NOT NULL,
              formula_id BIGINT NULL,
              product_key VARCHAR(255) NULL,
              brand_name VARCHAR(255) NULL,
              product_name VARCHAR(255) NULL,
              science_profile_coverage DECIMAL(8,4) NOT NULL DEFAULT 0,
              science_profile_used_count INT NOT NULL DEFAULT 0,
              missing_science_attributes_json JSON NULL,
              old_labels_json JSON NOT NULL,
              science_labels_json JSON NOT NULL,
              changed_fields_json JSON NOT NULL,
              comparison_status VARCHAR(32) NOT NULL,
              compared_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (comparison_id),
              KEY idx_science_compare_formula (formula_id),
              KEY idx_science_compare_source (source_id),
              KEY idx_science_compare_status (comparison_status),
              KEY idx_science_compare_coverage (science_profile_coverage)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
        insert = text(f"""
            INSERT INTO {comparison_fq}(
              source_id,formula_id,product_key,brand_name,product_name,
              science_profile_coverage,science_profile_used_count,
              missing_science_attributes_json,old_labels_json,science_labels_json,
              changed_fields_json,comparison_status
            ) VALUES(
              :source_id,:formula_id,:product_key,:brand_name,:product_name,
              :science_profile_coverage,:science_profile_used_count,
              :missing_science_attributes_json,:old_labels_json,:science_labels_json,
              :changed_fields_json,:comparison_status
            )
        """)
        records = []
        for new in science_rows:
            old = current_rows.get(int(new["formula_id"]), {}) if new.get("formula_id") is not None else {}
            old_labels = {field: old.get(field) for field in COMPARISON_FIELDS}
            new_labels = {field: new.get(field) for field in COMPARISON_FIELDS}
            changed = [field for field in COMPARISON_FIELDS if _clean_text(old_labels.get(field)) != _clean_text(new_labels.get(field))]
            coverage = float(new.get("science_profile_coverage") or 0.0)
            status = "no_baseline" if not old else "missing_science" if coverage < 1.0 else "changed" if changed else "matched"
            records.append({
                "source_id": int(new["source_id"]), "formula_id": new.get("formula_id"),
                "product_key": new.get("product_key"), "brand_name": new.get("brand_name"),
                "product_name": new.get("product_name"), "science_profile_coverage": coverage,
                "science_profile_used_count": int(new.get("science_profile_used_count") or 0),
                "missing_science_attributes_json": json.dumps(new.get("science_profile_missing") or [], ensure_ascii=False),
                "old_labels_json": json.dumps(old_labels, ensure_ascii=False),
                "science_labels_json": json.dumps(new_labels, ensure_ascii=False),
                "changed_fields_json": json.dumps(changed, ensure_ascii=False),
                "comparison_status": status,
            })
        if records:
            conn.execute(insert, records)
    counts: dict[str, int] = {}
    for record in records:
        counts[record["comparison_status"]] = counts.get(record["comparison_status"], 0) + 1
    return {"comparison_table": f"{target_db}.{comparison_table}", "row_count": len(records), "status_counts": counts}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "row_count": len(rows),
        "with_product_info": sum(1 for row in rows if row.get("guarantee_product_id") is not None),
        "with_guarantee_value": sum(1 for row in rows if row.get("guarantee_crude_protein_value") is not None),
        "with_animal_sources": sum(1 for row in rows if row.get("animal_sources")),
        "with_level1_categories": sum(1 for row in rows if row.get("animal_source_level1_categories")),
        "with_level2_sources": sum(1 for row in rows if row.get("animal_source_level2_sources")),
        "with_plant_protein_labels": sum(1 for row in rows if row.get("plant_protein_labels")),
    }


def print_preview(rows: list[dict[str, Any]], limit: int) -> None:
    for row in rows[:limit]:
        print(
            json.dumps(
                {
                    "source_id": row["source_id"],
                    "product_key": row["product_key"],
                    "brand_name": row["brand_name"],
                    "product_name": row["product_name"],
                    "animal_sources": row["animal_sources"],
                    "animal_source_level1_categories": row["animal_source_level1_categories"],
                    "animal_source_level2_sources": row["animal_source_level2_sources"],
                    "protein_source_details": row["protein_source_details"],
                    "primary_meat_source_species": row["primary_meat_source_species"],
                    "secondary_meat_source_species": row["secondary_meat_source_species"],
                    "primary_meat_source_count": row["primary_meat_source_count"],
                    "secondary_meat_source_count": row["secondary_meat_source_count"],
                    "protein_source_origin": row["protein_source_origin"],
                    "guarantee_crude_protein_value": str(row["guarantee_crude_protein_value"]) if row["guarantee_crude_protein_value"] is not None else None,
                },
                ensure_ascii=False,
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build protein_feature_platform.protein_source_aggregate from protein labels or parsed OCR rows."
    )
    parser.add_argument("--dsn", help="full SQLAlchemy DSN")
    parser.add_argument("--host", default=None, help="MySQL host")
    parser.add_argument("--port", default=None, help="MySQL port")
    parser.add_argument("--user", default=None, help="MySQL user")
    parser.add_argument("--password", default=None, help="MySQL password")
    parser.add_argument("--charset", default="utf8mb4", help="MySQL charset")
    parser.add_argument("--source-db", default="csv_labeling", help="database containing catfood_feature_protein_labels")
    parser.add_argument("--source-table", default="catfood_feature_protein_labels", help="source protein detail table")
    parser.add_argument(
        "--input-mode",
        choices=["direct", "legacy-label-table", "normalize-existing"],
        default="direct",
        help=(
            "direct reads new OCR rows; legacy-label-table replaces from the old label table; "
            "normalize-existing safely updates current aggregate rows from OCR ingredients"
        ),
    )
    parser.add_argument("--parsed-db", default="csv_labeling", help="database containing catfood_ingredient_ocr_parsed")
    parser.add_argument("--parsed-table", default="catfood_formula_feature_input", help="formula-keyed input table used to build protein labels")
    parser.add_argument("--feature-db", default="protein_feature_platform", help="database containing protein_source_feature")
    parser.add_argument("--feature-table", default="protein_source_feature", help="feature summary table")
    parser.add_argument("--guarantee-db", default="csv_labeling", help="database containing product_info/product_guarantee")
    parser.add_argument("--product-info-table", default="product_info", help="product info table")
    parser.add_argument("--product-guarantee-table", default="product_guarantee", help="product guarantee table")
    parser.add_argument("--standard-db", default="csv_labeling", help="database containing standard ingredient master")
    parser.add_argument("--standard-ingredient-table", default="catfood_standard_ingredient", help="standard ingredient table")
    parser.add_argument("--standard-alias-table", default="catfood_standard_ingredient_alias", help="standard ingredient alias table")
    parser.add_argument("--target-db", default="protein_feature_platform", help="target database")
    parser.add_argument("--target-table", default="protein_source_aggregate", help="target aggregate table")
    parser.add_argument("--comparison-table", default="protein_source_aggregate_science_comparison", help="auxiliary science migration comparison table")
    parser.add_argument("--comparison-only", action="store_true", help="write only the auxiliary comparison table; never update the target aggregate")
    parser.add_argument("--limit", type=int, default=0, help="max parsed OCR rows to process in direct mode, 0 means all new rows")
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("CATFOOD_PROTEIN_LABEL_CONCURRENCY", "4")), help="parallel LLM requests in direct mode")
    parser.add_argument("--preview-limit", type=int, default=10, help="preview rows in dry-run mode")
    parser.add_argument("--dry-run", action="store_true", help="print summary and preview without writing")
    parser.add_argument("--keep-backup", action="store_true", help="keep previous target table after swap")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_db = _safe_name(args.source_db, "source db")
    source_table = _safe_name(args.source_table, "source table")
    parsed_db = _safe_name(args.parsed_db, "parsed db")
    parsed_table = _safe_name(args.parsed_table, "parsed table")
    feature_db = _safe_name(args.feature_db, "feature db")
    feature_table = _safe_name(args.feature_table, "feature table")
    guarantee_db = _safe_name(args.guarantee_db, "guarantee db")
    product_info_table = _safe_name(args.product_info_table, "product info table")
    product_guarantee_table = _safe_name(args.product_guarantee_table, "product guarantee table")
    standard_db = _safe_name(args.standard_db, "standard db")
    standard_ingredient_table = _safe_name(args.standard_ingredient_table, "standard ingredient table")
    standard_alias_table = _safe_name(args.standard_alias_table, "standard alias table")
    target_db = _safe_name(args.target_db, "target db")
    target_table = _safe_name(args.target_table, "target table")
    comparison_table = _safe_name(args.comparison_table, "comparison table")

    engine = make_engine(args)
    if args.input_mode == "direct":
        raw_rows = load_rows_from_parsed_incremental(
            engine,
            parsed_db=parsed_db,
            parsed_table=parsed_table,
            feature_db=feature_db,
            feature_table=feature_table,
            guarantee_db=guarantee_db,
            product_info_table=product_info_table,
            product_guarantee_table=product_guarantee_table,
            target_db=target_db,
            target_table=target_table,
            limit=max(0, int(args.limit)),
            concurrency=max(1, int(args.concurrency)),
        )
    elif args.input_mode == "legacy-label-table":
        raw_rows = load_rows(
            engine,
            source_db=source_db,
            source_table=source_table,
            parsed_db=parsed_db,
            parsed_table=parsed_table,
            feature_db=feature_db,
            feature_table=feature_table,
            guarantee_db=guarantee_db,
            product_info_table=product_info_table,
            product_guarantee_table=product_guarantee_table,
        )
    else:
        raw_rows = load_existing_aggregate_rows(
            engine,
            parsed_db=parsed_db,
            parsed_table=parsed_table,
            target_db=target_db,
            target_table=target_table,
        )
    standard_lookup = _load_standard_ingredient_lookup(
        engine,
        standard_db=standard_db,
        ingredient_table=standard_ingredient_table,
        alias_table=standard_alias_table,
    )
    rows = transform_rows(raw_rows, standard_lookup=standard_lookup)
    summary = summarize(rows)
    summary.update(
        {
            "input_mode": args.input_mode,
            "source": (
                f"{parsed_db}.{parsed_table}"
                if args.input_mode == "direct"
                else f"{target_db}.{target_table}+{parsed_db}.{parsed_table}"
                if args.input_mode == "normalize-existing"
                else f"{source_db}.{source_table}"
            ),
            "parsed_filter": f"{parsed_db}.{parsed_table}",
            "feature": f"{feature_db}.{feature_table}",
            "standard_ingredient": f"{standard_db}.{standard_ingredient_table}",
            "standard_alias": f"{standard_db}.{standard_alias_table}",
            "standard_alias_count": len(standard_lookup),
            "target": f"{target_db}.{target_table}",
            "write_mode": (
                "replace_all"
                if args.input_mode == "legacy-label-table"
                else "upsert_without_delete"
            ),
            "dry_run": bool(args.dry_run),
        }
    )
    print(json.dumps(summary, ensure_ascii=False))

    if args.dry_run:
        print_preview(rows, max(1, int(args.preview_limit)))
        return 0

    if args.comparison_only:
        result = write_science_migration_comparison(
            engine,
            target_db=target_db,
            current_table=target_table,
            comparison_table=comparison_table,
            science_rows=rows,
        )
        print(json.dumps({"status": "ok", "target_aggregate_updated": False, **result}, ensure_ascii=False))
        return 0

    batch_id, backup_table = write_rows(
        engine,
        target_db=target_db,
        target_table=target_table,
        rows=rows,
        keep_backup=bool(args.keep_backup),
        replace_existing=args.input_mode == "legacy-label-table",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "target": f"{target_db}.{target_table}",
                "written_rows": len(rows),
                "batch_id": batch_id,
                "backup_table": backup_table,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
