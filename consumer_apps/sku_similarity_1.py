# -*- coding: utf-8 -*-
"""
SKU 三维相似推荐脚本
------------------------------------------------
功能：
1. 输入 target_sku_id
2. 从 sku_feature_input 读取 SKU 特征
3. 计算：
   - 原料类别相似度 ingredient_category_similarity
   - 营养结构相似度 nutrition_structure_similarity
   - 工艺结构相似度 process_structure_similarity
4. 三路召回：原料 TopN ∪ 营养 TopN ∪ 工艺 TopN
5. 按不同模式精排：
   - default: 默认找综合相似 SKU
   - black_chin: 黑下巴相似问题 SKU
   - soft_stool: 软便相似问题 SKU
   - process_risk: 工艺风险相似 SKU
6. 输出组合标签、业务解释、共同特征、关键差异、重点观察项

依赖：
pip install pandas sqlalchemy pymysql numpy
"""

import json
import math
import argparse
import os
import re
import unicodedata
from typing import Dict, List, Any, Tuple, Optional

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text


# ============================================================
# 1. 数据库配置
# ============================================================

DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", os.getenv("DB_HOST", "127.0.0.1")),
    "port": int(os.getenv("MYSQL_PORT", os.getenv("DB_PORT", "3306"))),
    "user": os.getenv("MYSQL_USER", os.getenv("DB_USER", "root")),
    "password": os.getenv("MYSQL_PASSWORD", os.getenv("DB_PASSWORD", "")),
    "database": os.getenv("MYSQL_DATABASE", os.getenv("DB_NAME", "protein_feature_platform")),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
}

TABLE_CONFIG = {
    # 你的 SKU 特征输入表
    "feature_table": "sku_feature_input",

    # 可选：输出结果表
    "result_table": "sku_similarity_recommendation_result",
}


# ============================================================
# 2. 字段配置：如果你数据库字段名不同，主要改这里
# ============================================================

COLUMN_CONFIG = {
    "sku_id": "sku_id",
    "brand_name": "brand_name",
    "product_name": "product_name",

    # 建议这三个字段存 JSON
    # 例如：{"禽肉蛋白": 1.0, "肉粉蛋白": 0.8, "豆类碳水": 0.6}
    "ingredient_category_vector": "ingredient_category_vector_json",

    # 例如：{"protein_score": 0.78, "fat_score": 0.66, "carb_score": 0.42}
    "nutrition_feature_vector": "nutrition_feature_vector_json",

    # 例如：{"膨化": 1.0, "油脂后喷": 0.8, "表面油脂残留风险": 0.7}
    "process_structure_vector": "process_structure_vector_json",
}


# ============================================================
# 3. 默认权重配置
# ============================================================

FINAL_WEIGHTS = {
    # 默认：找综合相似 SKU
    "default": {
        "ingredient": 0.45,
        "nutrition": 0.35,
        "process": 0.20,
    },

    # 黑下巴：脂肪结构 + 工艺表油/后喷都重要
    "black_chin": {
        "ingredient": 0.35,
        "nutrition": 0.30,
        "process": 0.35,
    },

    # 软便：营养压力，尤其碳水、蛋白、脂肪、纤维、益生元更重要
    "soft_stool": {
        "ingredient": 0.35,
        "nutrition": 0.45,
        "process": 0.20,
    },

    # 工艺风险：工艺结构优先
    "process_risk": {
        "ingredient": 0.25,
        "nutrition": 0.30,
        "process": 0.45,
    },

    # 8503 新定义：按营养结构相似项召回和排序
    "nutrition_structure": {
        "ingredient": 0.00,
        "nutrition": 1.00,
        "process": 0.00,
    },
}


# 营养结构相似度内部权重
NUTRITION_WEIGHTS = {
    "default": {
        "protein_score": 0.20,
        "fat_score": 0.20,
        "carb_score": 0.20,
        "fiber_score": 0.15,
        "prebiotic_score": 0.15,
        "antioxidant_score": 0.10,
    },

    "black_chin": {
        "protein_score": 0.15,
        "fat_score": 0.35,
        "carb_score": 0.10,
        "fiber_score": 0.05,
        "prebiotic_score": 0.15,
        "antioxidant_score": 0.20,
    },

    "soft_stool": {
        "protein_score": 0.20,
        "fat_score": 0.20,
        "carb_score": 0.25,
        "fiber_score": 0.20,
        "prebiotic_score": 0.15,
        "antioxidant_score": 0.00,
    },

    "process_risk": {
        "protein_score": 0.20,
        "fat_score": 0.30,
        "carb_score": 0.20,
        "fiber_score": 0.10,
        "prebiotic_score": 0.10,
        "antioxidant_score": 0.10,
    },

    "nutrition_structure": {
        "protein_score": 0.18,
        "fat_score": 0.18,
        "carb_score": 0.18,
        "fiber_score": 0.16,
        "prebiotic_score": 0.15,
        "antioxidant_score": 0.15,
    },
}


# 三路召回数量
RECALL_TOP_N = 50


# 最终返回 Top N
DEFAULT_TOP_N = 20


NUTRITION_FIELD_LABELS = {
    "protein_score": "蛋白质",
    "fat_score": "脂肪",
    "carb_score": "碳水/淀粉",
    "fiber_score": "纤维",
    "prebiotic_score": "益生元",
    "antioxidant_score": "抗氧化",
}

NUTRITION_FIELD_ORDER = list(NUTRITION_FIELD_LABELS.keys())


INGREDIENT_ROLE_RULES = [
    {
        "role": "动物蛋白-鲜肉/冻肉",
        "base": 1.00,
        "keywords": ["鲜鸡肉", "鲜鸭肉", "鲜牛肉", "鲜羊肉", "鲜鱼", "鸡肉", "鸭肉", "火鸡肉", "牛肉", "羊肉", "鱼肉", "三文鱼", "鲣鱼", "金枪鱼", "鲭鱼", "鳕鱼", "兔肉", "鹿肉"],
        "exclude": ["粉", "水解", "油"],
    },
    {
        "role": "动物蛋白-肉粉/鱼粉",
        "base": 0.95,
        "keywords": ["鸡肉粉", "鸭肉粉", "火鸡肉粉", "牛肉粉", "羊肉粉", "鱼粉", "三文鱼粉", "鲱鱼粉", "鳀鱼粉", "肉骨粉", "禽肉粉", "肉粉"],
        "exclude": [],
    },
    {
        "role": "动物蛋白-内脏/水解",
        "base": 0.85,
        "keywords": ["肝", "心", "肺", "内脏", "水解", "酶解", "动物水解物", "鸡肝", "鸭肝", "牛肝", "鱼水解物"],
        "exclude": [],
    },
    {
        "role": "植物蛋白",
        "base": 0.85,
        "keywords": ["大豆分离蛋白", "豌豆蛋白", "小麦蛋白", "玉米蛋白", "马铃薯蛋白", "植物蛋白", "谷朊粉"],
        "exclude": [],
    },
    {
        "role": "碳水-谷物/米类",
        "base": 0.85,
        "keywords": ["碎米", "大米", "白米", "糙米", "米粉", "大米粉", "酿酒米", "酿造米", "小麦", "小麦粉", "玉米", "玉米粉", "燕麦", "高粱", "大麦"],
        "exclude": ["蛋白", "油", "纤维", "提取物"],
    },
    {
        "role": "碳水-薯类/淀粉",
        "base": 0.85,
        "keywords": ["马铃薯", "土豆", "甘薯", "红薯", "木薯", "木薯粉", "马铃薯淀粉", "豌豆淀粉", "玉米淀粉", "淀粉"],
        "exclude": ["蛋白", "纤维"],
    },
    {
        "role": "碳水-豆类",
        "base": 0.80,
        "keywords": ["豌豆", "鹰嘴豆", "扁豆", "绿豆", "黄豆", "豆粉"],
        "exclude": ["蛋白", "纤维", "油"],
    },
    {
        "role": "脂肪-动物脂肪",
        "base": 0.90,
        "keywords": ["鸡油", "鸭油", "牛脂", "羊脂", "猪油", "动物脂肪", "禽脂肪"],
        "exclude": [],
    },
    {
        "role": "脂肪-功能油脂",
        "base": 0.85,
        "keywords": ["鱼油", "三文鱼油", "亚麻籽油", "葵花籽油", "椰子油", "菜籽油", "大豆油", "油脂", "植物油"],
        "exclude": [],
    },
    {
        "role": "纤维/粪便成形",
        "base": 0.80,
        "keywords": ["纤维素", "甜菜粕", "菊苣根", "车前子", "苹果纤维", "豌豆纤维", "木质纤维", "燕麦纤维", "南瓜", "果胶"],
        "exclude": [],
    },
    {
        "role": "益生元/发酵底物",
        "base": 0.75,
        "keywords": ["低聚果糖", "低聚半乳糖", "低聚木糖", "甘露寡糖", "果寡糖", "菊粉", "FOS", "MOS", "益生元"],
        "exclude": [],
    },
    {
        "role": "益生菌",
        "base": 0.70,
        "keywords": ["益生菌", "乳酸菌", "芽孢杆菌", "双歧杆菌", "肠球菌"],
        "exclude": [],
    },
    {
        "role": "抗氧化/植化支持",
        "base": 0.70,
        "keywords": ["迷迭香", "茶多酚", "维生素E", "生育酚", "蔓越莓", "蓝莓", "丝兰", "姜黄", "绿茶", "抗氧化"],
        "exclude": [],
    },
    {
        "role": "矿物/维生素",
        "base": 0.60,
        "keywords": ["磷酸氢钙", "碳酸钙", "氯化钾", "氯化钠", "硫酸锌", "硫酸铜", "维生素", "矿物质", "牛磺酸", "蛋氨酸", "赖氨酸"],
        "exclude": [],
    },
    {
        "role": "适口/调味",
        "base": 0.65,
        "keywords": ["诱食剂", "调味", "风味", "酵母抽提物", "啤酒酵母", "水解蛋白"],
        "exclude": [],
    },
]


INGREDIENT_LABEL_ROLE_FIELDS = [
    ("animal_source_level1_categories", "动物蛋白来源", 1.00),
    ("animal_source_level2_sources", "动物蛋白细分", 0.95),
    ("protein_source_details", "蛋白来源", 0.90),
    ("plant_protein_labels", "植物蛋白", 0.85),
    ("carb_type", "碳水类型", 0.90),
    ("carb_details", "碳水来源", 0.85),
    ("fiber_source_details", "纤维来源", 0.85),
    ("p_main_ingredients", "纤维结构", 0.90),
    ("q_main_ingredients", "发酵底物", 0.85),
    ("prebiotic_details", "益生元", 0.85),
    ("probiotic_details", "益生菌", 0.80),
    ("fat_sources", "脂肪来源", 0.85),
    ("fat_source_types", "脂肪类型", 0.85),
    ("antioxidant_sources", "抗氧化来源", 0.80),
    ("antioxidant_types", "抗氧化类型", 0.75),
    ("micronutrient_sources", "微量营养来源", 0.70),
    ("starch_ingredients_json", "淀粉来源", 0.85),
]


# ============================================================
# 4. 业务解释配置
# ============================================================

OBSERVATION_RULES = {
    "black_chin": {
        "default_observe": [
            "脂肪负担是否偏高",
            "动物脂肪顺位是否靠前",
            "Omega-6压力是否偏高",
            "Omega-3支持是否不足",
            "抗氧化支持是否偏弱",
            "益生元/纤维调节支持是否不足",
            "是否存在油脂后喷或表面油脂残留风险",
            "是否需要关注过氧化值、酸价、喷涂均匀度",
        ],
        "process_tags": [
            "油脂后喷",
            "表面油脂残留风险",
            "氧化控制压力",
            "适口喷涂依赖",
            "膨化",
        ],
        "nutrition_tags": [
            "fat_score",
            "antioxidant_score",
            "prebiotic_score",
            "protein_score",
        ],
    },

    "soft_stool": {
        "default_observe": [
            "碳水/淀粉压力是否偏高",
            "豆类、薯类、精制淀粉是否靠前",
            "蛋白结构是否复杂",
            "脂肪消化负担是否偏高",
            "便便成形支持是否不足",
            "菌群代谢支持是否不足",
            "供菌底物是否相对过量",
            "是否需要关注粪便成形、消化率、肠道耐受反馈",
        ],
        "process_tags": [
            "高淀粉成型依赖",
            "膨化",
            "颗粒成型稳定性压力",
            "高纤维结构",
        ],
        "nutrition_tags": [
            "carb_score",
            "protein_score",
            "fat_score",
            "fiber_score",
            "prebiotic_score",
        ],
    },

    "process_risk": {
        "default_observe": [
            "是否存在相同工艺路径",
            "是否存在油脂后喷压力",
            "是否存在表面油脂残留风险",
            "是否存在淀粉膨化/糊化依赖",
            "是否存在颗粒成型稳定性压力",
            "是否存在冻干后混复杂度",
            "建议关注过氧化值、酸价、水分活度、颗粒硬度、喷涂均匀度",
        ],
        "process_tags": [
            "膨化",
            "低温烘焙",
            "风干",
            "冻干",
            "油脂后喷",
            "冻干后混",
            "表面油脂残留风险",
            "氧化控制压力",
            "高淀粉成型依赖",
            "颗粒成型稳定性压力",
        ],
        "nutrition_tags": [
            "fat_score",
            "carb_score",
            "fiber_score",
        ],
    },

    "default": {
        "default_observe": [
            "共同原料类别是否集中",
            "营养压力是否一致",
            "工艺路径是否一致",
            "若反馈相同，观察是否存在结构性共因",
            "若反馈不同，观察是否来自配比、批次、原料品质或工艺控制差异",
        ],
        "process_tags": [
            "膨化",
            "油脂后喷",
            "表面油脂残留风险",
            "高淀粉成型依赖",
            "氧化控制压力",
        ],
        "nutrition_tags": [
            "protein_score",
            "fat_score",
            "carb_score",
            "fiber_score",
            "prebiotic_score",
            "antioxidant_score",
        ],
    },
}


# ============================================================
# 5. 工具函数
# ============================================================

def get_engine():
    url = (
        f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
        f"?charset={DB_CONFIG['charset']}"
    )
    return create_engine(url)


def safe_json_loads(value: Any) -> Dict[str, float]:
    """
    安全解析 JSON。
    支持：
    1. JSON 字符串
    2. dict
    3. 空值
    """
    if value is None:
        return {}

    if isinstance(value, float) and math.isnan(value):
        return {}

    if isinstance(value, dict):
        return {str(k): float(v) for k, v in value.items() if is_number(v)}

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        try:
            data = json.loads(value)
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items() if is_number(v)}
            return {}
        except Exception:
            return {}

    return {}


def safe_list_loads(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, float) and math.isnan(value):
        return []

    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]

    if isinstance(value, dict):
        result: List[str] = []
        for key in ("tags", "main_reason_tags", "support_reason_tags", "fat_detail_tags", "all_reason_tags"):
            tags = value.get(key)
            if isinstance(tags, list):
                result.extend(str(x).strip() for x in tags if str(x).strip())
        if result:
            return result
        return [str(k).strip() for k, v in value.items() if str(k).strip() and is_number(v) and float(v) > 0]

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return safe_list_loads(parsed)
        except Exception:
            normalized = (
                value.replace("，", ",")
                .replace("、", ",")
                .replace("；", ",")
                .replace(";", ",")
                .replace("|", ",")
            )
            return [x.strip() for x in normalized.split(",") if x.strip()]

    return [str(value).strip()]


def list_vector(*values: Any) -> Dict[str, float]:
    vector: Dict[str, float] = {}
    for value in values:
        for tag in safe_list_loads(value):
            vector[tag] = max(vector.get(tag, 0.0), 1.0)
    return vector


def split_ingredient_items(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, float) and math.isnan(value):
        return []
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        text_value = str(value).strip()
        if not text_value:
            return []
        try:
            parsed = json.loads(text_value)
            if isinstance(parsed, list):
                raw_items = [str(item) for item in parsed]
            elif isinstance(parsed, dict):
                raw_items = [str(item) for item in parsed.values()]
            else:
                raw_items = [text_value]
        except Exception:
            raw_items = re.split(r"[，,、;；\n\r]+", text_value)

    items = []
    for item in raw_items:
        cleaned = re.sub(r"^[\s\d一二三四五六七八九十]+[\.、)]?", "", str(item)).strip()
        cleaned = re.sub(r"[（(].*?[）)]", "", cleaned).strip()
        if cleaned:
            items.append(cleaned)
    return items


def ingredient_rank_weight(rank: int) -> float:
    if rank <= 1:
        return 1.0
    if rank <= 3:
        return 0.85
    if rank <= 6:
        return 0.65
    if rank <= 10:
        return 0.45
    return 0.25


def classify_ingredient_role(ingredient: str) -> Optional[Tuple[str, float]]:
    text_value = str(ingredient or "").strip()
    if not text_value:
        return None
    for rule in INGREDIENT_ROLE_RULES:
        if any(keyword and keyword in text_value for keyword in rule["keywords"]):
            if any(keyword and keyword in text_value for keyword in rule.get("exclude", [])):
                continue
            return str(rule["role"]), float(rule["base"])
    return None


def build_ingredient_role_vector(ingredient_text: Any) -> Dict[str, float]:
    vector: Dict[str, float] = {}
    for idx, ingredient in enumerate(split_ingredient_items(ingredient_text), start=1):
        matched = classify_ingredient_role(ingredient)
        if not matched:
            continue
        role, base_score = matched
        score = round(base_score * ingredient_rank_weight(idx), 6)
        vector[role] = clamp01(vector.get(role, 0.0) + score)
    return vector


def normalize_role_item(value: Any) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    text_value = re.sub(r"[（(].*?[）)]", "", text_value).strip()
    text_value = text_value.strip(" :：,，;；、|")
    return text_value


def labeled_role_items(value: Any, field: str) -> List[str]:
    if field in {"p_main_ingredients", "q_main_ingredients"}:
        if value is None:
            return []
        text_value = str(value or "").strip()
        if not text_value:
            return []
        result = []
        for item in re.split(r"[、;；|]+", text_value):
            cleaned = normalize_role_item(item)
            if cleaned:
                result.append(cleaned)
        return result

    if field == "starch_ingredients_json":
        raw = value
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None
        if isinstance(raw, list):
            result = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                category = normalize_role_item(item.get("category"))
                ingredient = normalize_role_item(item.get("ingredient_name"))
                if category and ingredient:
                    result.append(f"{category}-{ingredient}")
                elif category:
                    result.append(category)
                elif ingredient:
                    result.append(ingredient)
            return result

    items = []
    for item in safe_list_loads(value):
        cleaned = normalize_role_item(item)
        if cleaned:
            items.append(cleaned)
    return items


def build_label_based_ingredient_vector(row: Any) -> Dict[str, float]:
    vector: Dict[str, float] = {}
    for field, role_prefix, weight in INGREDIENT_LABEL_ROLE_FIELDS:
        for item in labeled_role_items(row.get(field), field):
            label = f"{role_prefix}：{item}"
            vector[label] = max(vector.get(label, 0.0), float(weight))
    return vector


def vector_overlap_details(
    target_vector: Dict[str, float],
    candidate_vector: Dict[str, float],
    key_label: str,
    top_n: int = 8,
) -> List[Dict[str, Any]]:
    details = []
    for key in set(target_vector.keys()) & set(candidate_vector.keys()):
        target_value = float(target_vector.get(key, 0.0) or 0.0)
        candidate_value = float(candidate_vector.get(key, 0.0) or 0.0)
        shared_value = min(target_value, candidate_value)
        if shared_value <= 0:
            continue
        details.append({
            key_label: key,
            "target_strength": round(target_value, 4),
            "candidate_strength": round(candidate_value, 4),
            "shared_strength": round(shared_value, 4),
        })
    details.sort(key=lambda item: item["shared_strength"], reverse=True)
    return details[:top_n]


def nutrition_component_details(
    target_nutrition: Dict[str, float],
    candidate_nutrition: Dict[str, float],
    mode: str,
) -> List[Dict[str, Any]]:
    weights = normalize_weights(NUTRITION_WEIGHTS.get(mode, NUTRITION_WEIGHTS["default"]))
    field_order = {field: index for index, field in enumerate(NUTRITION_FIELD_ORDER)}
    details = []
    for field in NUTRITION_FIELD_ORDER:
        if field not in target_nutrition or field not in candidate_nutrition:
            continue
        weight = weights.get(field, 0.0)
        target_value = clamp01(float(target_nutrition.get(field, 0.0) or 0.0))
        candidate_value = clamp01(float(candidate_nutrition.get(field, 0.0) or 0.0))
        similarity = clamp01(1.0 - abs(target_value - candidate_value))
        details.append({
            "field": field,
            "label": NUTRITION_FIELD_LABELS.get(field, field),
            "similarity": round(similarity, 4),
            "target_strength": round(target_value, 4),
            "candidate_strength": round(candidate_value, 4),
            "weight": round(float(weight or 0.0), 4),
        })
    details.sort(key=lambda item: field_order.get(item["field"], len(field_order)))
    return details


def numeric_value(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if math.isnan(number):
        return None
    return number


def normalize_brand(value: Any) -> str:
    return normalize_sku_id(value)


def normalize_sku_id(value: Any) -> str:
    """Build a comparison key for SKU ids produced by different pipelines."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def is_number(x: Any) -> bool:
    try:
        float(x)
        return True
    except Exception:
        return False


def clamp01(x: float) -> float:
    if x is None or np.isnan(x):
        return 0.0
    return max(0.0, min(1.0, float(x)))


def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(v for v in weights.values() if v > 0)
    if total <= 0:
        return weights
    return {k: v / total for k, v in weights.items()}


def table_columns(engine, table_name: str) -> set:
    sql = text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = :table_name
    """)
    df = pd.read_sql(sql, engine, params={"table_name": table_name})
    return set(df["COLUMN_NAME"].tolist())


def quote_identifier(name: str) -> str:
    return "`{}`".format(str(name).replace("`", "``"))


def select_expr(columns: set, field: str, alias: Optional[str] = None) -> str:
    alias = alias or field
    if field in columns:
        return f"{quote_identifier(field)} AS {quote_identifier(alias)}"
    return f"NULL AS {quote_identifier(alias)}"


# ============================================================
# 6. 相似度计算
# ============================================================

def weighted_jaccard_similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    """
    加权 Jaccard 相似度，适合原料类别向量、工艺标签向量。

    例：
    a = {"禽肉蛋白": 1.0, "肉粉蛋白": 0.8, "豆类碳水": 0.6}
    b = {"禽肉蛋白": 0.9, "肉粉蛋白": 0.8, "薯类碳水": 0.5}

    similarity = sum(min(a_i,b_i)) / sum(max(a_i,b_i))
    """
    if not a or not b:
        return 0.0

    keys = set(a.keys()) | set(b.keys())
    min_sum = 0.0
    max_sum = 0.0

    for k in keys:
        av = max(0.0, float(a.get(k, 0.0)))
        bv = max(0.0, float(b.get(k, 0.0)))
        min_sum += min(av, bv)
        max_sum += max(av, bv)

    if max_sum <= 0:
        return 0.0

    return clamp01(min_sum / max_sum)


def numeric_vector_similarity(
    a: Dict[str, float],
    b: Dict[str, float],
    weights: Dict[str, float],
) -> float:
    """
    营养结构相似度。
    用加权绝对距离计算：

    distance = Σ weight_i * abs(a_i - b_i)
    similarity = 1 - distance

    要求 a_i、b_i 已经标准化到 0-1。
    """
    if not a or not b:
        return 0.0

    weights = normalize_weights(weights)
    distance = 0.0
    valid_weight_sum = 0.0

    for k, w in weights.items():
        if w <= 0:
            continue

        av = a.get(k, None)
        bv = b.get(k, None)

        # 两边都没有这个指标，则跳过
        if av is None and bv is None:
            continue

        av = clamp01(float(av or 0.0))
        bv = clamp01(float(bv or 0.0))

        distance += w * abs(av - bv)
        valid_weight_sum += w

    if valid_weight_sum <= 0:
        return 0.0

    # 重新按有效权重归一
    distance = distance / valid_weight_sum
    return clamp01(1.0 - distance)


def calculate_three_similarities(
    target_row: pd.Series,
    candidate_row: pd.Series,
    mode: str = "default",
) -> Dict[str, float]:
    ingredient_col = COLUMN_CONFIG["ingredient_category_vector"]
    nutrition_col = COLUMN_CONFIG["nutrition_feature_vector"]
    process_col = COLUMN_CONFIG["process_structure_vector"]

    target_ingredient = safe_json_loads(target_row[ingredient_col])
    cand_ingredient = safe_json_loads(candidate_row[ingredient_col])

    target_nutrition = safe_json_loads(target_row[nutrition_col])
    cand_nutrition = safe_json_loads(candidate_row[nutrition_col])

    target_process = safe_json_loads(target_row[process_col])
    cand_process = safe_json_loads(candidate_row[process_col])

    nutrition_weights = NUTRITION_WEIGHTS.get(mode, NUTRITION_WEIGHTS["default"])

    ingredient_sim = weighted_jaccard_similarity(target_ingredient, cand_ingredient)
    nutrition_sim = numeric_vector_similarity(target_nutrition, cand_nutrition, nutrition_weights)
    process_sim = weighted_jaccard_similarity(target_process, cand_process)

    return {
        "ingredient_category_similarity": round(ingredient_sim, 6),
        "nutrition_structure_similarity": round(nutrition_sim, 6),
        "process_structure_similarity": round(process_sim, 6),
    }


def calculate_final_similarity(
    ingredient_sim: float,
    nutrition_sim: float,
    process_sim: float,
    mode: str = "default",
) -> float:
    weights = FINAL_WEIGHTS.get(mode, FINAL_WEIGHTS["default"])
    score = (
        weights["ingredient"] * ingredient_sim
        + weights["nutrition"] * nutrition_sim
        + weights["process"] * process_sim
    )
    return round(clamp01(score), 6)


# ============================================================
# 7. 高/中/低等级与组合标签
# ============================================================

def similarity_level(score: float) -> str:
    """
    第一版先用固定阈值。
    如果你后面样本多，建议改成历史分位数：
    Top 30% = 高，Middle 40% = 中，Bottom 30% = 低。
    """
    score = float(score or 0.0)
    if score >= 0.75:
        return "高"
    elif score >= 0.50:
        return "中"
    else:
        return "低"


def build_similarity_pattern(
    ingredient_level: str,
    nutrition_level: str,
    process_level: str,
) -> Tuple[str, str]:
    """
    根据三维等级生成组合标签和解释类型。
    """

    key = (ingredient_level, nutrition_level, process_level)

    pattern_map = {
        ("高", "高", "高"): (
            "配方-营养-工艺三重接近型",
            "目标 SKU 与候选 SKU 在原料骨架、营养压力和工艺路径上均接近，适合作为同类 SKU 参考，也适合做问题反馈迁移观察。"
        ),
        ("高", "高", "中"): (
            "同类配方同营养，工艺部分接近型",
            "两者配方骨架和营养压力接近，工艺路径部分相似。若反馈不同，应重点观察工艺控制或批次差异。"
        ),
        ("高", "高", "低"): (
            "同类配方同营养，工艺分化型",
            "两者原料和营养结构接近，但工艺路径差异明显。若症状反馈不同，工艺环节可能是关键解释变量。"
        ),

        ("高", "中", "高"): (
            "同类配方同工艺，营养强度轻度分化型",
            "两者配方骨架和工艺路径接近，但营养压力存在一定差异。适合观察配比强度是否放大风险。"
        ),
        ("高", "低", "高"): (
            "同类配方同工艺，营养强度显著分化型",
            "两者用料类型和工艺路径接近，但营养结构差异明显。若反馈差异大，应优先排查脂肪、蛋白、碳水、纤维等营养强度。"
        ),

        ("高", "中", "中"): (
            "同类配方骨架，营养与工艺中度接近型",
            "两者属于较接近的配方骨架，但营养压力和工艺路径只达到中度相似，适合做同类参考，但不宜直接做强风险迁移。"
        ),
        ("高", "中", "低"): (
            "同类配方骨架，工艺分化型",
            "两者原料类别接近，但工艺路径差异较大。若反馈差异明显，应重点观察生产方式、后喷、干燥、成型等工艺因素。"
        ),
        ("高", "低", "中"): (
            "同类配方骨架，营养分化型",
            "两者原料类别接近，但营养压力差异明显。适合观察同类原料在不同配比强度下的风险边界。"
        ),
        ("高", "低", "低"): (
            "同类原料骨架，但营养与工艺差异大",
            "两者只是用料类别相似，但营养压力和工艺路径均不同，不建议直接做问题归因迁移。"
        ),

        ("中", "高", "高"): (
            "营养-工艺收敛型",
            "两者原料类别中度接近，但营养压力和工艺路径高度接近。若出现相同反馈，可能是营养压力与工艺路径共同造成。"
        ),
        ("低", "高", "高"): (
            "异源配方，营养与工艺收敛型",
            "两者原料路径不同，但营养压力和工艺风险接近。适合观察结构性风险，而不是单一原料风险。"
        ),

        ("中", "高", "中"): (
            "营养压力主导型",
            "两者营养结构高度接近，原料和工艺中度相似。若症状相同，应优先观察共同营养压力。"
        ),
        ("低", "高", "中"): (
            "异源配方，营养压力收敛型",
            "两者原料类别不同，但营养压力接近。若反馈相似，风险更可能来自高脂、高蛋白、高碳水或低缓冲等营养结构。"
        ),
        ("低", "高", "低"): (
            "异源异工艺，营养压力收敛型",
            "两者原料和工艺不同，但营养结构接近。适合做营养压力归因，不适合直接谈原料或工艺共因。"
        ),

        ("中", "中", "高"): (
            "工艺路径主导型",
            "两者工艺结构高度接近，原料和营养中度相似。若反馈相同，应重点观察工艺质量指标。"
        ),
        ("低", "低", "高"): (
            "异源异营养，工艺路径接近型",
            "两者原料和营养都不同，但工艺路径高度接近。若出现相同反馈，应优先怀疑工艺共因。"
        ),
        ("低", "中", "高"): (
            "异源配方，工艺路径收敛型",
            "两者原料类别不同，但工艺路径高度接近。适合观察后喷、成型、干燥、氧化控制等生产路径风险。"
        ),

        ("中", "中", "中"): (
            "三维中度接近型",
            "两者在原料、营养和工艺上均有一定相似性，但都不算强相似，适合作为弱参考。"
        ),
        ("中", "中", "低"): (
            "原料营养中度接近，工艺差异型",
            "两者配方和营养有一定相似，但工艺不同。若反馈不同，工艺差异可能解释一部分结果。"
        ),
        ("中", "低", "中"): (
            "原料工艺中度接近，营养差异型",
            "两者用料和工艺有一定相似，但营养压力不同。适合观察配比强度带来的风险差异。"
        ),
        ("低", "中", "中"): (
            "异源配方，中度结构接近型",
            "两者原料不同，但营养和工艺有一定接近。可以作为辅助参考，不宜做强归因。"
        ),

        ("中", "低", "低"): (
            "弱配方参考型",
            "两者只有原料类别中度接近，营养和工艺差异较大，参考价值有限。"
        ),
        ("低", "中", "低"): (
            "弱营养参考型",
            "两者只有营养结构中度接近，原料和工艺差异较大，适合做宏观营养压力参考。"
        ),
        ("低", "低", "中"): (
            "弱工艺参考型",
            "两者只有工艺结构中度接近，适合做工艺风险辅助观察。"
        ),
        ("低", "低", "低"): (
            "结构差异较大型",
            "两者在原料、营养和工艺上均不接近，不建议作为相似 SKU 参考。"
        ),
    }

    return pattern_map.get(
        key,
        (
            "未定义组合型",
            "当前组合未覆盖，建议检查相似度分级逻辑。"
        )
    )


# ============================================================
# 8. 共同特征、关键差异、重点观察项
# ============================================================

def get_top_shared_features(
    target_vector: Dict[str, float],
    candidate_vector: Dict[str, float],
    top_n: int = 8,
) -> List[str]:
    """
    找共同命中的高权重标签。
    """
    shared = []
    for k in set(target_vector.keys()) & set(candidate_vector.keys()):
        tv = float(target_vector.get(k, 0.0))
        cv = float(candidate_vector.get(k, 0.0))
        shared_score = min(tv, cv)
        if shared_score > 0:
            shared.append((k, shared_score))

    shared.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in shared[:top_n]]


def get_key_differences(
    target_vector: Dict[str, float],
    candidate_vector: Dict[str, float],
    top_n: int = 8,
    prefix_target: str = "目标SKU更高",
    prefix_candidate: str = "候选SKU更高",
) -> List[str]:
    """
    找差异最大的标签/指标。
    """
    diffs = []
    keys = set(target_vector.keys()) | set(candidate_vector.keys())

    for k in keys:
        tv = float(target_vector.get(k, 0.0))
        cv = float(candidate_vector.get(k, 0.0))
        diff = tv - cv
        if abs(diff) >= 0.15:
            if diff > 0:
                diffs.append((f"{prefix_target}：{k}", abs(diff)))
            else:
                diffs.append((f"{prefix_candidate}：{k}", abs(diff)))

    diffs.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in diffs[:top_n]]


def build_observation_points(
    mode: str,
    target_row: pd.Series,
    candidate_row: pd.Series,
    ingredient_level: str,
    nutrition_level: str,
    process_level: str,
) -> List[str]:
    """
    根据分析模式 + 三维相似等级生成重点观察项。
    """
    rule = OBSERVATION_RULES.get(mode, OBSERVATION_RULES["default"])
    points = list(rule["default_observe"])

    ingredient_col = COLUMN_CONFIG["ingredient_category_vector"]
    nutrition_col = COLUMN_CONFIG["nutrition_feature_vector"]
    process_col = COLUMN_CONFIG["process_structure_vector"]

    target_ingredient = safe_json_loads(target_row[ingredient_col])
    cand_ingredient = safe_json_loads(candidate_row[ingredient_col])

    target_nutrition = safe_json_loads(target_row[nutrition_col])
    cand_nutrition = safe_json_loads(candidate_row[nutrition_col])

    target_process = safe_json_loads(target_row[process_col])
    cand_process = safe_json_loads(candidate_row[process_col])

    shared_ingredient = set(target_ingredient.keys()) & set(cand_ingredient.keys())
    shared_process = set(target_process.keys()) & set(cand_process.keys())

    # 如果原料高相似，强调原料共因
    if ingredient_level == "高":
        points.insert(0, "优先观察共同原料类别是否与症状反馈集中相关")
    elif ingredient_level == "低":
        points.insert(0, "原料路径不同，不宜直接归因为某个共同原料")

    # 如果营养高相似，强调营养压力共因
    if nutrition_level == "高":
        points.insert(0, "优先观察共同营养压力是否为主要风险来源")
    elif nutrition_level == "低":
        points.append("营养压力差异较大，需观察配比强度是否造成反馈差异")

    # 如果工艺高相似，强调工艺共因
    if process_level == "高":
        points.insert(0, "优先观察相同工艺路径下的质量控制指标")
    elif process_level == "低":
        points.append("工艺路径差异较大，若反馈不同，应重点观察工艺差异")

    # 根据命中的工艺标签补充 QC 观察项
    if shared_process:
        if "油脂后喷" in shared_process or "表面油脂残留风险" in shared_process:
            points.append("建议重点看：表面油脂残留、喷涂均匀度、过氧化值、酸价")
        if "高淀粉成型依赖" in shared_process or "膨化" in shared_process:
            points.append("建议重点看：淀粉糊化程度、颗粒硬度、膨化稳定性")
        if "冻干后混" in shared_process or "冻干" in shared_process:
            points.append("建议重点看：冻干混合均匀度、水分活度、批次稳定性")

    # 根据原料共同项补充观察
    if shared_ingredient:
        if any(x in shared_ingredient for x in ["豆类碳水", "薯类碳水", "精制淀粉", "植物蛋白"]):
            points.append("共同存在豆/薯/淀粉/植物蛋白结构时，需观察软便和消化耐受反馈")
        if any(x in shared_ingredient for x in ["动物脂肪", "鸡油", "牛油", "鱼油"]):
            points.append("共同存在脂肪来源时，需观察黑下巴、氧化稳定性和皮脂反馈")

    # 去重保序
    deduped = []
    for p in points:
        if p not in deduped:
            deduped.append(p)

    return deduped[:12]


def build_business_interpretation(
    pattern_name: str,
    pattern_desc: str,
    ingredient_level: str,
    nutrition_level: str,
    process_level: str,
    mode: str,
) -> str:
    """
    生成业务解释文本。
    """
    mode_name_map = {
        "default": "综合相似推荐",
        "black_chin": "黑下巴风险观察",
        "soft_stool": "软便风险观察",
        "process_risk": "工艺风险观察",
    }
    mode_name = mode_name_map.get(mode, "综合相似推荐")

    return (
        f"当前为【{mode_name}】模式。"
        f"该候选 SKU 的组合标签为【{pattern_name}】。"
        f"原料类别相似度为【{ingredient_level}】，营养结构相似度为【{nutrition_level}】，工艺结构相似度为【{process_level}】。"
        f"{pattern_desc}"
    )


# ============================================================
# 9. 数据读取与结果写入
# ============================================================

def load_sku_features(engine) -> pd.DataFrame:
    table = TABLE_CONFIG["feature_table"]
    feature_cols = table_columns(engine, table)

    needed_cols = [
        COLUMN_CONFIG["sku_id"],
        COLUMN_CONFIG["brand_name"],
        COLUMN_CONFIG["product_name"],
        COLUMN_CONFIG["ingredient_category_vector"],
        COLUMN_CONFIG["nutrition_feature_vector"],
        COLUMN_CONFIG["process_structure_vector"],
    ]

    if all(col in feature_cols for col in needed_cols):
        sql = f"""
            SELECT
                {", ".join(quote_identifier(col) for col in needed_cols)}
            FROM {quote_identifier(table)}
        """

        df = pd.read_sql(sql, engine)
        df = df[df[COLUMN_CONFIG["sku_id"]].notna()].copy()
        return df.drop_duplicates(subset=[COLUMN_CONFIG["sku_id"]], keep="first")

    required_base_cols = [
        COLUMN_CONFIG["sku_id"],
        "sku_name",
        COLUMN_CONFIG["brand_name"],
        "protein_score",
        "fat_score",
        "carb_score",
        "fiber_score",
        "prebiotic_score",
        "antioxidant_score",
        "created_at",
    ]
    missing_base_cols = [col for col in required_base_cols[:-1] if col not in feature_cols]
    if missing_base_cols:
        raise ValueError(f"{table} 缺少字段：{missing_base_cols}")

    label_table = "catfood_sku_label_wide"
    process_table = "sku_process_feature_profile"
    b2b_table = "sku_b2b_tag_result"
    label_cols = table_columns(engine, label_table)
    process_cols = table_columns(engine, process_table)
    b2b_cols = table_columns(engine, b2b_table)

    sql = f"""
        SELECT
            f.{quote_identifier(COLUMN_CONFIG["sku_id"])} AS sku_id,
            f.{quote_identifier(COLUMN_CONFIG["brand_name"])} AS brand_name,
            COALESCE(b.product_name, f.sku_name) AS product_name,
            f.protein_score,
            f.fat_score,
            f.carb_score,
            f.fiber_score,
            f.prebiotic_score,
            f.antioxidant_score,
            {select_expr(label_cols, "ingredient_composition")},
            {select_expr(label_cols, "animal_source_level1_categories")},
            {select_expr(label_cols, "animal_source_level2_sources")},
            {select_expr(label_cols, "protein_source_details")},
            {select_expr(label_cols, "plant_protein_labels")},
            {select_expr(label_cols, "carb_type")},
            {select_expr(label_cols, "carb_details")},
            {select_expr(label_cols, "fiber_source_details")},
            {select_expr(label_cols, "p_main_ingredients")},
            {select_expr(label_cols, "q_main_ingredients")},
            {select_expr(label_cols, "fat_sources")},
            {select_expr(label_cols, "fat_source_types")},
            {select_expr(label_cols, "biotic_structure")},
            {select_expr(label_cols, "biotic_type")},
            {select_expr(label_cols, "prebiotic_details")},
            {select_expr(label_cols, "probiotic_details")},
            {select_expr(label_cols, "antioxidant_sources")},
            {select_expr(label_cols, "antioxidant_types")},
            {select_expr(label_cols, "micronutrient_sources")},
            {select_expr(label_cols, "starch_ingredients_json")},
            {select_expr(process_cols, "main_process_tags")},
            {select_expr(b2b_cols, "process_structure_tags")},
            {select_expr(process_cols, "moisture_drying_score")},
            {select_expr(process_cols, "animal_protein_powder_score")},
            {select_expr(process_cols, "plant_protein_score")},
            {select_expr(process_cols, "starch_extrusion_score")},
            {select_expr(process_cols, "oil_spray_score")},
            {select_expr(process_cols, "oxidation_sensitivity_score")},
            {select_expr(process_cols, "palatability_process_score")},
            {select_expr(process_cols, "fermentation_substrate_score")},
            {select_expr(process_cols, "fiber_structure_score")},
            {select_expr(process_cols, "water_binding_gel_score")},
            {select_expr(process_cols, "mineral_powder_score")}
        FROM {quote_identifier(table)} f
        LEFT JOIN {quote_identifier(b2b_table)} b
            ON f.sku_id = b.sku_id
        LEFT JOIN {quote_identifier(label_table)} w
            ON f.sku_id = w.product_key
        LEFT JOIN {quote_identifier(process_table)} pf
            ON f.sku_id = pf.sku_id
        ORDER BY f.created_at DESC
    """

    df = pd.read_sql(sql, engine)

    # 去掉没有 sku_id 的记录
    df = df[df["sku_id"].notna()].copy()
    df = df.drop_duplicates(subset=["sku_id"], keep="first").reset_index(drop=True)

    nutrition_fields = [
        "protein_score",
        "fat_score",
        "carb_score",
        "fiber_score",
        "prebiotic_score",
        "antioxidant_score",
    ]
    for field in nutrition_fields:
        numeric = pd.to_numeric(df[field], errors="coerce")
        min_value = numeric.min()
        max_value = numeric.max()
        if pd.isna(min_value) or pd.isna(max_value) or max_value == min_value:
            df[f"__norm_{field}"] = numeric.apply(lambda value: 0.0 if pd.isna(value) else clamp01(value))
        else:
            df[f"__norm_{field}"] = (numeric - min_value) / (max_value - min_value)

    ingredient_fields = [
        "animal_source_level1_categories",
        "animal_source_level2_sources",
        "protein_source_details",
        "plant_protein_labels",
        "carb_type",
        "carb_details",
        "fiber_source_details",
        "p_main_ingredients",
        "q_main_ingredients",
        "fat_sources",
        "fat_source_types",
        "biotic_structure",
        "biotic_type",
        "prebiotic_details",
        "probiotic_details",
        "antioxidant_sources",
        "antioxidant_types",
        "micronutrient_sources",
        "starch_ingredients_json",
    ]
    process_score_fields = [
        "moisture_drying_score",
        "animal_protein_powder_score",
        "plant_protein_score",
        "starch_extrusion_score",
        "oil_spray_score",
        "oxidation_sensitivity_score",
        "palatability_process_score",
        "fermentation_substrate_score",
        "fiber_structure_score",
        "water_binding_gel_score",
        "mineral_powder_score",
    ]

    ingredient_vectors = []
    nutrition_vectors = []
    process_vectors = []

    for _, row in df.iterrows():
        ingredient_vector = build_label_based_ingredient_vector(row)
        if not ingredient_vector:
            ingredient_vector = build_ingredient_role_vector(row.get("ingredient_composition"))
        if not ingredient_vector:
            ingredient_vector = list_vector(*(row.get(field) for field in ingredient_fields))
        ingredient_vectors.append(json.dumps(
            ingredient_vector,
            ensure_ascii=False,
        ))
        nutrition_vectors.append(json.dumps({
            field: round(float(row.get(f"__norm_{field}") or 0.0), 6)
            for field in nutrition_fields
        }, ensure_ascii=False))

        process_vector = list_vector(row.get("main_process_tags"), row.get("process_structure_tags"))
        for field in process_score_fields:
            value = numeric_value(row.get(field))
            if value is not None and value > 0:
                process_vector[field] = clamp01(value)
        process_vectors.append(json.dumps(process_vector, ensure_ascii=False))

    df[COLUMN_CONFIG["ingredient_category_vector"]] = ingredient_vectors
    df[COLUMN_CONFIG["nutrition_feature_vector"]] = nutrition_vectors
    df[COLUMN_CONFIG["process_structure_vector"]] = process_vectors

    df = df.rename(columns={
        "sku_id": COLUMN_CONFIG["sku_id"],
        "brand_name": COLUMN_CONFIG["brand_name"],
        "product_name": COLUMN_CONFIG["product_name"],
    })

    return df[
        [
            COLUMN_CONFIG["sku_id"],
            COLUMN_CONFIG["brand_name"],
            COLUMN_CONFIG["product_name"],
            COLUMN_CONFIG["ingredient_category_vector"],
            COLUMN_CONFIG["nutrition_feature_vector"],
            COLUMN_CONFIG["process_structure_vector"],
        ]
    ].copy()


def ensure_result_table(engine):
    table = TABLE_CONFIG["result_table"]

    sql = f"""
    CREATE TABLE IF NOT EXISTS {table} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        target_sku_id VARCHAR(128) NOT NULL,
        candidate_sku_id VARCHAR(128) NOT NULL,
        mode VARCHAR(64) NOT NULL,

        final_similarity DECIMAL(10,6),
        ingredient_category_similarity DECIMAL(10,6),
        nutrition_structure_similarity DECIMAL(10,6),
        process_structure_similarity DECIMAL(10,6),

        ingredient_similarity_level VARCHAR(16),
        nutrition_similarity_level VARCHAR(16),
        process_similarity_level VARCHAR(16),

        similarity_pattern VARCHAR(255),
        business_interpretation TEXT,

        shared_ingredient_features JSON,
        shared_nutrition_features JSON,
        shared_process_features JSON,
        key_differences JSON,
        observation_points JSON,

        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) DEFAULT CHARSET=utf8mb4;
    """

    with engine.begin() as conn:
        conn.execute(text(sql))


def save_results_to_db(
    engine,
    result_df: pd.DataFrame,
    target_sku_id: str,
    mode: str,
):
    if result_df.empty:
        return

    ensure_result_table(engine)
    table = TABLE_CONFIG["result_table"]

    rows = []

    for _, r in result_df.iterrows():
        rows.append({
            "target_sku_id": target_sku_id,
            "candidate_sku_id": r["candidate_sku_id"],
            "mode": mode,
            "final_similarity": float(r["final_similarity"]),
            "ingredient_category_similarity": float(r["ingredient_category_similarity"]),
            "nutrition_structure_similarity": float(r["nutrition_structure_similarity"]),
            "process_structure_similarity": float(r["process_structure_similarity"]),
            "ingredient_similarity_level": r["ingredient_similarity_level"],
            "nutrition_similarity_level": r["nutrition_similarity_level"],
            "process_similarity_level": r["process_similarity_level"],
            "similarity_pattern": r["similarity_pattern"],
            "business_interpretation": r["business_interpretation"],
            "shared_ingredient_features": json.dumps(r["shared_ingredient_features"], ensure_ascii=False),
            "shared_nutrition_features": json.dumps(r["shared_nutrition_features"], ensure_ascii=False),
            "shared_process_features": json.dumps(r["shared_process_features"], ensure_ascii=False),
            "key_differences": json.dumps(r["key_differences"], ensure_ascii=False),
            "observation_points": json.dumps(r["observation_points"], ensure_ascii=False),
        })

    insert_sql = text(f"""
        INSERT INTO {table} (
            target_sku_id,
            candidate_sku_id,
            mode,
            final_similarity,
            ingredient_category_similarity,
            nutrition_structure_similarity,
            process_structure_similarity,
            ingredient_similarity_level,
            nutrition_similarity_level,
            process_similarity_level,
            similarity_pattern,
            business_interpretation,
            shared_ingredient_features,
            shared_nutrition_features,
            shared_process_features,
            key_differences,
            observation_points
        ) VALUES (
            :target_sku_id,
            :candidate_sku_id,
            :mode,
            :final_similarity,
            :ingredient_category_similarity,
            :nutrition_structure_similarity,
            :process_structure_similarity,
            :ingredient_similarity_level,
            :nutrition_similarity_level,
            :process_similarity_level,
            :similarity_pattern,
            :business_interpretation,
            CAST(:shared_ingredient_features AS JSON),
            CAST(:shared_nutrition_features AS JSON),
            CAST(:shared_process_features AS JSON),
            CAST(:key_differences AS JSON),
            CAST(:observation_points AS JSON)
        )
    """)

    with engine.begin() as conn:
        conn.execute(insert_sql, rows)


# ============================================================
# 10. 主推荐逻辑
# ============================================================

def build_similarity_recommendations(
    df: pd.DataFrame,
    target_sku_id: str,
    mode: str = "default",
    top_n: int = DEFAULT_TOP_N,
) -> pd.DataFrame:
    sku_col = COLUMN_CONFIG["sku_id"]
    brand_col = COLUMN_CONFIG["brand_name"]
    product_col = COLUMN_CONFIG["product_name"]

    ingredient_col = COLUMN_CONFIG["ingredient_category_vector"]
    nutrition_col = COLUMN_CONFIG["nutrition_feature_vector"]
    process_col = COLUMN_CONFIG["process_structure_vector"]

    if mode not in FINAL_WEIGHTS:
        raise ValueError(f"mode 不支持：{mode}，可选：{list(FINAL_WEIGHTS.keys())}")

    sku_text = df[sku_col].astype(str)
    target_text = str(target_sku_id)
    target_rows = df[sku_text == target_text]
    if target_rows.empty:
        target_key = normalize_sku_id(target_text)
        target_rows = df[sku_text.map(normalize_sku_id) == target_key]
    if target_rows.empty:
        raise ValueError(f"找不到 target_sku_id: {target_sku_id}")

    target_row = target_rows.iloc[0]
    resolved_target_key = normalize_sku_id(target_row[sku_col])
    target_brand_key = normalize_brand(target_row.get(brand_col))

    records = []

    for _, cand_row in df.iterrows():
        candidate_sku_id = str(cand_row[sku_col])

        # 跳过自己
        if normalize_sku_id(candidate_sku_id) == resolved_target_key:
            continue

        candidate_brand_key = normalize_brand(cand_row.get(brand_col))
        if target_brand_key and candidate_brand_key == target_brand_key:
            continue

        sims = calculate_three_similarities(target_row, cand_row, mode=mode)

        ingredient_sim = sims["ingredient_category_similarity"]
        nutrition_sim = sims["nutrition_structure_similarity"]
        process_sim = sims["process_structure_similarity"]

        final_sim = calculate_final_similarity(
            ingredient_sim,
            nutrition_sim,
            process_sim,
            mode=mode,
        )

        records.append({
            "candidate_sku_id": candidate_sku_id,
            "candidate_brand_name": cand_row.get(brand_col),
            "candidate_product_name": cand_row.get(product_col),
            "ingredient_category_similarity": ingredient_sim,
            "nutrition_structure_similarity": nutrition_sim,
            "process_structure_similarity": process_sim,
            "final_similarity": final_sim,
        })

    sim_df = pd.DataFrame(records)

    if sim_df.empty:
        return sim_df

    # 三路召回
    ingredient_top = sim_df.sort_values(
        "ingredient_category_similarity", ascending=False
    ).head(RECALL_TOP_N)

    nutrition_top = sim_df.sort_values(
        "nutrition_structure_similarity", ascending=False
    ).head(RECALL_TOP_N)

    process_top = sim_df.sort_values(
        "process_structure_similarity", ascending=False
    ).head(RECALL_TOP_N)

    candidate_pool_ids = set(ingredient_top["candidate_sku_id"]) \
        | set(nutrition_top["candidate_sku_id"]) \
        | set(process_top["candidate_sku_id"])

    candidate_df = sim_df[sim_df["candidate_sku_id"].isin(candidate_pool_ids)].copy()

    # 精排
    candidate_df = candidate_df.sort_values("final_similarity", ascending=False)

    enriched_records = []

    for _, r in candidate_df.iterrows():
        candidate_sku_id = str(r["candidate_sku_id"])
        cand_row = df[df[sku_col].astype(str) == candidate_sku_id].iloc[0]

        ingredient_level = similarity_level(r["ingredient_category_similarity"])
        nutrition_level = similarity_level(r["nutrition_structure_similarity"])
        process_level = similarity_level(r["process_structure_similarity"])

        pattern_name, pattern_desc = build_similarity_pattern(
            ingredient_level,
            nutrition_level,
            process_level,
        )

        target_ingredient = safe_json_loads(target_row[ingredient_col])
        cand_ingredient = safe_json_loads(cand_row[ingredient_col])

        target_nutrition = safe_json_loads(target_row[nutrition_col])
        cand_nutrition = safe_json_loads(cand_row[nutrition_col])

        target_process = safe_json_loads(target_row[process_col])
        cand_process = safe_json_loads(cand_row[process_col])

        shared_ingredient_features = get_top_shared_features(
            target_ingredient, cand_ingredient, top_n=8
        )
        ingredient_role_details = vector_overlap_details(
            target_ingredient,
            cand_ingredient,
            key_label="功能角色",
            top_n=8,
        )

        shared_nutrition_features = get_top_shared_features(
            target_nutrition, cand_nutrition, top_n=8
        )
        nutrition_details = nutrition_component_details(
            target_nutrition,
            cand_nutrition,
            mode=mode,
        )
        high_nutrition_parts = [
            item["label"]
            for item in nutrition_details
            if float(item.get("similarity") or 0.0) >= 0.75
        ]

        shared_process_features = get_top_shared_features(
            target_process, cand_process, top_n=8
        )

        key_differences = []
        key_differences.extend(
            get_key_differences(
                target_ingredient,
                cand_ingredient,
                top_n=4,
                prefix_target="目标SKU原料结构更高",
                prefix_candidate="候选SKU原料结构更高",
            )
        )
        key_differences.extend(
            get_key_differences(
                target_nutrition,
                cand_nutrition,
                top_n=4,
                prefix_target="目标SKU营养压力更高",
                prefix_candidate="候选SKU营养压力更高",
            )
        )
        key_differences.extend(
            get_key_differences(
                target_process,
                cand_process,
                top_n=4,
                prefix_target="目标SKU工艺风险更高",
                prefix_candidate="候选SKU工艺风险更高",
            )
        )

        observation_points = build_observation_points(
            mode=mode,
            target_row=target_row,
            candidate_row=cand_row,
            ingredient_level=ingredient_level,
            nutrition_level=nutrition_level,
            process_level=process_level,
        )

        business_interpretation = build_business_interpretation(
            pattern_name=pattern_name,
            pattern_desc=pattern_desc,
            ingredient_level=ingredient_level,
            nutrition_level=nutrition_level,
            process_level=process_level,
            mode=mode,
        )

        enriched_records.append({
            "target_sku_id": str(target_sku_id),
            "target_brand_name": target_row.get(brand_col),
            "target_product_name": target_row.get(product_col),

            "candidate_sku_id": candidate_sku_id,
            "candidate_brand_name": r["candidate_brand_name"],
            "candidate_product_name": r["candidate_product_name"],

            "mode": mode,

            "final_similarity": float(r["final_similarity"]),
            "ingredient_category_similarity": float(r["ingredient_category_similarity"]),
            "nutrition_structure_similarity": float(r["nutrition_structure_similarity"]),
            "process_structure_similarity": float(r["process_structure_similarity"]),

            "ingredient_similarity_level": ingredient_level,
            "nutrition_similarity_level": nutrition_level,
            "process_similarity_level": process_level,

            "similarity_pattern": pattern_name,
            "business_interpretation": business_interpretation,

            "shared_ingredient_features": shared_ingredient_features,
            "ingredient_role_similarity_details": ingredient_role_details,
            "shared_nutrition_features": shared_nutrition_features,
            "nutrition_component_similarities": nutrition_details,
            "high_nutrition_similarity_parts": high_nutrition_parts,
            "shared_process_features": shared_process_features,

            "key_differences": key_differences[:12],
            "observation_points": observation_points,
        })

    result_df = pd.DataFrame(enriched_records)
    result_df = result_df.sort_values("final_similarity", ascending=False)
    result_df["__brand_key"] = result_df["candidate_brand_name"].apply(normalize_brand)
    result_df = result_df.drop_duplicates(subset=["__brand_key"], keep="first")
    result_df = result_df.drop(columns=["__brand_key"]).head(top_n)

    return result_df


# ============================================================
# 11. 输出格式
# ============================================================

def print_readable_results(result_df: pd.DataFrame):
    if result_df.empty:
        print("没有找到相似 SKU。")
        return

    for idx, r in result_df.iterrows():
        print("=" * 100)
        print(f"候选 SKU：{r['candidate_brand_name']} - {r['candidate_product_name']} ({r['candidate_sku_id']})")
        print(f"最终相似度：{r['final_similarity']:.4f}")
        print(
            f"三维相似度："
            f"原料 {r['ingredient_category_similarity']:.4f} / "
            f"营养 {r['nutrition_structure_similarity']:.4f} / "
            f"工艺 {r['process_structure_similarity']:.4f}"
        )
        print(
            f"三维等级："
            f"原料 {r['ingredient_similarity_level']} / "
            f"营养 {r['nutrition_similarity_level']} / "
            f"工艺 {r['process_similarity_level']}"
        )
        print(f"组合标签：{r['similarity_pattern']}")
        print(f"解释：{r['business_interpretation']}")

        print("\n共同原料特征：")
        print("、".join(r["shared_ingredient_features"]) if r["shared_ingredient_features"] else "无明显共同原料特征")

        if r.get("ingredient_role_similarity_details"):
            print("\n配方骨架证据：")
            for item in r["ingredient_role_similarity_details"]:
                print(
                    f"- {item.get('功能角色')}："
                    f"目标 {item.get('target_strength')} / "
                    f"候选 {item.get('candidate_strength')} / "
                    f"共同 {item.get('shared_strength')}"
                )

        print("\n共同营养特征：")
        print("、".join(r["shared_nutrition_features"]) if r["shared_nutrition_features"] else "无明显共同营养特征")

        if r.get("nutrition_component_similarities"):
            print("\n营养压力拆解：")
            for item in r["nutrition_component_similarities"]:
                print(
                    f"- {item.get('label')}：相似度 {item.get('similarity')}，"
                    f"目标 {item.get('target_strength')} / 候选 {item.get('candidate_strength')}"
                )

        print("\n共同工艺特征：")
        print("、".join(r["shared_process_features"]) if r["shared_process_features"] else "无明显共同工艺特征")

        print("\n关键差异：")
        if r["key_differences"]:
            for item in r["key_differences"]:
                print(f"- {item}")
        else:
            print("无明显关键差异")

        print("\n重点观察：")
        for item in r["observation_points"]:
            print(f"- {item}")

        print()


def save_result_csv(result_df: pd.DataFrame, output_path: str):
    df = result_df.copy()

    # list 字段转 JSON 字符串，方便 CSV 查看
    list_cols = [
        "shared_ingredient_features",
        "ingredient_role_similarity_details",
        "shared_nutrition_features",
        "nutrition_component_similarities",
        "high_nutrition_similarity_parts",
        "shared_process_features",
        "key_differences",
        "observation_points",
    ]

    for col in list_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: json.dumps(x, ensure_ascii=False))

    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"已输出 CSV：{output_path}")


# ============================================================
# 12. 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="SKU 三维相似推荐")
    parser.add_argument("--target_sku_id", required=True, help="目标 SKU ID")
    parser.add_argument(
        "--mode",
        default="default",
        choices=["default", "black_chin", "soft_stool", "process_risk", "nutrition_structure"],
        help="推荐模式",
    )
    parser.add_argument("--top_n", type=int, default=DEFAULT_TOP_N, help="返回 Top N")
    parser.add_argument("--save_db", action="store_true", help="是否保存结果到数据库")
    parser.add_argument("--output_csv", default="", help="CSV 输出路径，可选")

    args = parser.parse_args()

    engine = get_engine()
    df = load_sku_features(engine)

    result_df = build_similarity_recommendations(
        df=df,
        target_sku_id=args.target_sku_id,
        mode=args.mode,
        top_n=args.top_n,
    )

    print_readable_results(result_df)

    if args.output_csv:
        save_result_csv(result_df, args.output_csv)

    if args.save_db:
        save_results_to_db(
            engine=engine,
            result_df=result_df,
            target_sku_id=args.target_sku_id,
            mode=args.mode,
        )
        print(f"已保存到数据库表：{TABLE_CONFIG['result_table']}")


if __name__ == "__main__":
    main()
