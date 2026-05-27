# -*- coding: utf-8 -*-
"""Brand correction helpers for OCR-derived cat-food product rows."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional


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
    return configured + KNOWN_BRAND_TOKENS


def infer_brand_from_text(*values: Any) -> Optional[str]:
    haystack = normalize_text(" ".join(clean_text(value) for value in values if clean_text(value)))
    if not haystack:
        return None

    matches = []
    for brand in _configured_brand_tokens():
        normalized_brand = normalize_text(brand)
        if normalized_brand and normalized_brand in haystack:
            matches.append(brand)
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
    inferred = infer_brand_from_text(
        product_name,
        image_name,
        _path_name(image_path),
        image_path,
    )
    if inferred and (is_generic_brand(original) or normalize_text(inferred) not in normalize_text(original)):
        return inferred
    return original or (inferred or "")


def build_product_key(brand: Any, product_name: Any) -> str:
    brand_text = clean_text(brand)
    product_text = clean_text(product_name)
    if brand_text and product_text:
        return f"{brand_text}||{product_text}"
    return brand_text or product_text
