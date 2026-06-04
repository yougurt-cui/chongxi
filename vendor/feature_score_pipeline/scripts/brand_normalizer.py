# -*- coding: utf-8 -*-
"""Brand correction helpers for OCR-derived cat-food product rows."""


import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml


GENERIC_BRAND_VALUES = {
    "全价宠物食品成年期",
    "全价宠物食品幼年期",
    "全价宠物食品",
    "全价猫粮",
    "全价幼猫粮",
    "全价成猫粮",
    "成年期",
    "幼年期",
    "成猫粮",
    "幼猫粮",
    "猫粮",
}

KNOWN_BRAND_TOKENS = [
    "奇境本源",
]

BRAND_MASTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "csv_mysql_labeling"
    / "config"
    / "catfood_brand_master.yaml"
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", clean_text(value)).lower()


def _path_name(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return Path(text).stem


def _configured_brand_tokens() -> list[str]:
    raw = os.getenv("CATFOOD_KNOWN_BRANDS", "")
    configured = [item.strip() for item in re.split(r"[,，|;；\s]+", raw) if item.strip()]
    return configured + _brand_master_tokens() + KNOWN_BRAND_TOKENS


@lru_cache(maxsize=1)
def _load_brand_master() -> dict[str, Any]:
    if not BRAND_MASTER_PATH.exists():
        return {"brands": [], "alias_mappings": []}
    return yaml.safe_load(BRAND_MASTER_PATH.read_text(encoding="utf-8")) or {"brands": [], "alias_mappings": []}


@lru_cache(maxsize=1)
def _brand_alias_map() -> dict[str, str]:
    data = _load_brand_master()
    mapping: dict[str, str] = {}

    def add(alias: Any, standard: Any) -> None:
        alias_text = clean_text(alias)
        standard_text = clean_text(standard)
        if not alias_text or not standard_text:
            return
        mapping[normalize_text(alias_text)] = standard_text
        if "!" in alias_text:
            mapping[normalize_text(alias_text.replace("!", ""))] = standard_text

    for row in data.get("brands") or []:
        if clean_text(row.get("status") or "active") != "active":
            continue
        standard = row.get("standard_name")
        add(standard, standard)
        for alias in row.get("aliases") or []:
            add(alias, standard)

    for row in data.get("alias_mappings") or []:
        add(row.get("alias"), row.get("standard_name"))

    return mapping


def _brand_master_tokens() -> list[str]:
    data = _load_brand_master()
    tokens: list[str] = []
    seen = set()

    def add(value: Any) -> None:
        text = clean_text(value)
        key = normalize_text(text)
        if text and key not in seen:
            seen.add(key)
            tokens.append(text)
        if "!" in text:
            no_bang = text.replace("!", "")
            no_bang_key = normalize_text(no_bang)
            if no_bang and no_bang_key not in seen:
                seen.add(no_bang_key)
                tokens.append(no_bang)

    for row in data.get("brands") or []:
        if clean_text(row.get("status") or "active") != "active":
            continue
        add(row.get("standard_name"))
        for alias in row.get("aliases") or []:
            add(alias)
    for row in data.get("alias_mappings") or []:
        add(row.get("alias"))
    return tokens


def canonicalize_brand(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return _brand_alias_map().get(normalize_text(text), text)


def infer_brand_from_text(*values: Any) -> Optional[str]:
    haystack = normalize_text(" ".join(clean_text(value) for value in values if clean_text(value)))
    if not haystack:
        return None

    matches = []
    for brand in _configured_brand_tokens():
        normalized_brand = normalize_text(brand)
        if normalized_brand and normalized_brand in haystack:
            matches.append(canonicalize_brand(brand))
    if not matches:
        return None
    return max(matches, key=lambda item: len(normalize_text(item)))


def is_generic_brand(value: Any) -> bool:
    text = clean_text(value)
    if not text:
        return True
    normalized = normalize_text(text)
    return normalized in {normalize_text(item) for item in GENERIC_BRAND_VALUES}


def correct_brand(
    brand: Any,
    product_name: Any = None,
    image_name: Any = None,
    image_path: Any = None,
) -> str:
    original = clean_text(brand)
    canonical_original = canonicalize_brand(original)
    if canonical_original and normalize_text(canonical_original) != normalize_text(original):
        return canonical_original
    inferred = infer_brand_from_text(
        product_name,
        image_name,
        _path_name(image_path),
        image_path,
    )
    if inferred and (is_generic_brand(original) or normalize_text(inferred) not in normalize_text(original)):
        return inferred
    return canonical_original or original or (inferred or "")


def build_product_key(brand: Any, product_name: Any) -> str:
    brand_text = canonicalize_brand(brand)
    product_text = clean_text(product_name)
    if brand_text and product_text:
        return f"{brand_text}||{product_text}"
    return brand_text or product_text
