"""Conservative, auditable science proxies for unmatched ingredients."""

from __future__ import annotations

import re
from typing import Any


VERSION = "coarse-proxy-v1"


def classify(raw_name: Any) -> dict[str, Any]:
    text = str(raw_name or "").strip()
    category = "other"
    family = "其他"
    role = "其他"
    source_type = "other"
    domain: dict[str, Any] = {}
    confidence = 0.45

    if re.search(r"油|脂肪", text):
        category, family, role = "fat", "油脂", "脂肪酸支持"
        source_type = "animal" if re.search(r"鱼|鸡|鸭|牛|羊|猪|磷虾", text) else "plant"
        domain = {"fat_source": "marine" if re.search(r"鱼|磷虾", text) else source_type}
        confidence = 0.7
    elif re.search(r"菊粉|果寡糖|甘露寡糖|低聚|益生元", text):
        category, family, role, source_type = "prebiotic", "益生元", "益生元支持", "plant"
        domain = {"fiber_solubility": "soluble", "fermentability": "high", "prebiotic_functions": ["microbiome_feed", "scfa_support"]}
        confidence = 0.75
    elif re.search(r"纤维|甜菜粕|车前子|纤维素", text):
        category, family, role, source_type = "fiber", "膳食纤维", "膳食纤维支持", "plant"
        domain = {"fiber_solubility": "mixed", "fermentability": "medium", "fiber_functions": ["bulk"]}
        confidence = 0.7
    elif re.search(r"淀粉|薯|米|麦|玉米|豌豆|豆粉|面粉", text) and "蛋白" not in text:
        category, family, role, source_type = "carbohydrate", "碳水原料", "碳水供给", "plant"
        starch = "tuber" if re.search(r"薯", text) else "legume" if re.search(r"豆", text) else "flour" if re.search(r"粉", text) else "grain"
        domain = {"starch_category": starch}
        confidence = 0.65
    elif re.search(r"肉|鱼|蛋|肝|心|肾|蛋白", text):
        category, family, role = "protein", "动物蛋白", "蛋白质供给"
        source_type = "plant" if re.search(r"豌豆|大豆|植物|谷朊", text) else "animal"
        form = "hydrolyzed" if "水解" in text else "meal" if re.search(r"粉|肉骨", text) else "fresh" if "鲜" in text else "frozen" if "冻" in text else "other"
        domain = {"protein_form": form, "plant_protein_form": form if source_type == "plant" else None}
        confidence = 0.7

    return {
        "is_proxy": True,
        "version": VERSION,
        "confidence": confidence,
        "category": category,
        "ingredient_family": family,
        "source_type": source_type,
        "primary_nutrition_role": role,
        "science_profile": {
            "science_status": "active",
            "profile_version": VERSION,
            "nutrition_category": category,
            "domain_attributes_json": domain,
            "function_attributes_json": {},
        },
    }


def apply_proxy(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("standard_ingredient_id") or item.get("is_ignored"):
        return item
    proxy = classify(item.get("raw_name"))
    item["coarse_proxy"] = proxy
    item["ingredient_family"] = proxy["ingredient_family"]
    item["source_type"] = proxy["source_type"]
    item["primary_nutrition_role"] = proxy["primary_nutrition_role"]
    if proxy["category"] == "protein":
        item["is_protein"] = True
        item["is_plant_protein"] = proxy["source_type"] == "plant"
        item["protein_form"] = proxy["science_profile"]["domain_attributes_json"].get("protein_form")
    return item


def proxy_profile(item: dict[str, Any]) -> dict[str, Any] | None:
    value = item.get("features_json")
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            value = {}
    proxy = (value or {}).get("coarse_proxy") or item.get("coarse_proxy")
    return proxy.get("science_profile") if isinstance(proxy, dict) else None
