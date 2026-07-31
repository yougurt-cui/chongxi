# -*- coding: utf-8 -*-
"""B2B order analysis Streamlit page."""

from __future__ import annotations

import json
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.express as px
import pymysql
import streamlit as st


INGREDIENT_BREAKDOWN_RULES = {
    "蛋白质": {
        "tags": ["鲜肉", "冻肉", "肉粉", "内脏", "动物蛋白"],
        "keywords": [
            "鲜鸡肉", "鲜鸭肉", "鲜鱼", "鲜牛肉", "鲜羊肉", "鲜兔肉", "鸡肉", "鸭肉", "鱼肉", "牛肉", "羊肉",
            "冻鸡肉", "冻鸭肉", "冻鱼", "冻牛肉", "冻羊肉", "鸡肉粉", "鸭肉粉", "鱼粉", "牛肉粉", "羊肉粉",
            "肉粉", "鸡肝", "鸭肝", "牛肝", "肝", "心", "肾", "蛋粉",
        ],
    },
    "脂肪": {
        "tags": ["动物油脂", "功能油脂"],
        "keywords": [
            "鸡油", "鸭油", "牛油", "鱼油", "磷虾油", "亚麻籽油", "葵花籽油", "动物脂肪", "禽脂",
            "油脂", "植物油", "椰子油",
        ],
    },
    "碳水": {
        "tags": ["豆类", "薯类", "谷物", "精制淀粉/粉类"],
        "keywords": [
            "豌豆", "扁豆", "鹰嘴豆", "绿豆", "蚕豆", "大豆", "马铃薯", "土豆", "红薯", "甘薯",
            "木薯", "山药", "紫薯", "大米", "糙米", "玉米", "小麦", "燕麦", "高粱", "大麦",
            "淀粉", "木薯粉", "马铃薯粉", "土豆粉", "玉米粉", "小麦粉", "大米粉", "米粉",
        ],
    },
    "纤维素": {
        "tags": ["纤维"],
        "keywords": ["纤维素", "甜菜粕", "车前子", "苜蓿", "南瓜", "苹果纤维", "菊苣纤维", "燕麦纤维"],
    },
    "益生元": {
        "tags": ["益生元"],
        "keywords": ["菊粉", "果寡糖", "低聚果糖", "甘露寡糖", "低聚木糖", "益生元", "菊苣根"],
    },
}


IMPORTED_BRANDS = {
    "go", "冠能", "天衡宝", "巅峰", "希尔斯", "皇家", "绿福摩", "恩萃", "渴望", "百利",
    "素力高", "诺乐", "法米娜", "荒野", "爱肯拿", "纽翠斯", "草本魔力", "钻石", "汤普森", "美士",
}

DOMESTIC_BRANDS = {
    "麦富迪", "顽皮", "帕特", "蓝氏", "星益", "澳龙", "纯皓", "网易严选", "醇粹", "鲜朗",
    "甄萃", "金故", "霸弗", "领先", "诚实一口", "纯福", "自然光",
}


RISK_REASON_GROUPS = {
    "上游压力": ["脂肪负担偏高", "Omega-6压力偏高", "Omega比例偏失衡"],
    "保护不足": ["Omega-3支持不足", "抗氧化支持偏弱", "SCFA支持偏弱"],
    "中间机制": ["脂肪调节支持不足", "菌群代谢支持不足", "脂肪消化负担偏高"],
    "症状解释": ["黑下巴风险", "软便风险", "便便成形变差"],
}


SYMPTOM_RATE_FIELDS = [
    ("软便/拉稀", "soft_stool_rate"),
    ("呕吐/反胃", "vomiting_rate"),
    ("黑下巴", "black_chin_rate"),
    ("泪痕/上火", "tear_stain_rate"),
    ("油腻/碗油", "oily_feedback_rate"),
    ("粉多/碎渣", "dust_feedback_rate"),
    ("太硬", "too_hard_feedback_rate"),
    ("不吃/适口性差", "palatability_negative_rate"),
    ("换批不稳定", "batch_inconsistency_rate"),
]

DISPLAY_SECONDARY_SYMPTOM_FIELDS = [
    ("黑下巴", "black_chin_rate"),
    ("不吃/适口性差", "palatability_negative_rate"),
    ("软便/拉稀", "soft_stool_rate"),
    ("粉多/碎渣", "dust_feedback_rate"),
    ("呕吐/反胃", "vomiting_rate"),
]

ANTIOXIDANT_EXTRA_KEYWORDS = [
    "迷叠香", "迷迭香", "石榴", "苹果", "蓝莓", "蔓越莓", "越橘",
    "姜黄", "绿茶", "茶多酚", "丝兰", "柑橘提取物", "生育酚", "维生素E",
]


PROCESS_MODULE_LABELS = {
    "moisture_drying_score": "高含水肉源/干燥负荷",
    "animal_protein_powder_score": "动物蛋白干粉结构",
    "plant_protein_score": "植物蛋白补强",
    "starch_extrusion_score": "淀粉膨化支撑",
    "oil_spray_score": "油脂后喷涂",
    "oxidation_sensitivity_score": "氧化敏感脂肪",
    "palatability_process_score": "风味适口增强",
    "fermentation_substrate_score": "益生元发酵底物",
    "fiber_structure_score": "纤维结构支持",
    "water_binding_gel_score": "高吸水胶质结构",
    "mineral_powder_score": "矿物粉体结构",
}

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


APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import b2b_order_analysis as b2b  # noqa: E402

b2b = importlib.reload(b2b)
b2b.similarity_recall = importlib.reload(b2b.similarity_recall)

SUMMARY_MODULE_PATH = Path(__file__).resolve().with_name("summary.py")
summary_spec = importlib.util.spec_from_file_location("process_pressure_summary", SUMMARY_MODULE_PATH)
process_pressure_summary = importlib.util.module_from_spec(summary_spec)
assert summary_spec and summary_spec.loader
summary_spec.loader.exec_module(process_pressure_summary)

SKU_FUNCTION_STRUCTURE_MODULE_PATH = Path(__file__).resolve().with_name("sku_function_structure.py")
sku_function_spec = importlib.util.spec_from_file_location("sku_function_structure_rules", SKU_FUNCTION_STRUCTURE_MODULE_PATH)
sku_function_structure_rules = importlib.util.module_from_spec(sku_function_spec)
assert sku_function_spec and sku_function_spec.loader
sku_function_spec.loader.exec_module(sku_function_structure_rules)

DB_CONFIG = b2b.DB_CONFIG
B2B_TAG_TABLE = b2b.B2B_TAG_TABLE
OUTPUT_TABLE = b2b.OUTPUT_TABLE
PATH_MAPPING_XLSX = Path("/Users/yoghourt/Downloads/原料路径映射总表.xlsx")
analyze_by_filters = b2b.analyze_by_filters
analyze_by_target_sku = b2b.analyze_by_target_sku
ensure_output_table = b2b.ensure_output_table
fetch_default_target_sku_id = b2b.fetch_default_target_sku_id
json_dumps = b2b.json_dumps
safe_json_loads = b2b.safe_json_loads
safe_float = b2b.safe_float


def get_conn():
    return pymysql.connect(**DB_CONFIG)


def _table_columns(cursor, table_name: str) -> set[str]:
    cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
    columns = set()
    for row in cursor.fetchall():
        if isinstance(row, dict):
            columns.add(str(row.get("Field") or ""))
        elif row:
            columns.add(str(row[0] or ""))
    return {column for column in columns if column}


def _select_existing_columns(cursor, table_name: str, columns: List[str]) -> str:
    existing_columns = _table_columns(cursor, table_name)
    selected_columns = [column for column in columns if column in existing_columns]
    if not selected_columns:
        raise RuntimeError(f"No expected columns found in {table_name}")
    return ", ".join(f"`{column}`" for column in selected_columns)


def _keyed_select_existing_columns(
    cursor,
    table_name: str,
    columns: List[str],
    *,
    key_alias: str = "product_key",
) -> tuple[str, str]:
    existing_columns = _table_columns(cursor, table_name)
    key_column = next(
        (column for column in (key_alias, "product_key", "sku_id", "formula_id", "source_id", "id") if column in existing_columns),
        "",
    )
    if not key_column:
        raise RuntimeError(f"No usable key column found in {table_name}")
    selected_columns = [column for column in columns if column in existing_columns and column != key_column]
    select_parts = [f"`{key_column}` AS `{key_alias}`"]
    select_parts.extend(f"`{column}`" for column in selected_columns)
    return ", ".join(select_parts), f"`{key_column}` IS NOT NULL"


def parse_json_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def json_preview(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


def parse_optional_float(value: str) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    return float(value)


def parse_optional_range(value: str) -> tuple[float | None, float | None]:
    numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", str(value or ""))]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    lower, upper = numbers[:2]
    return min(lower, upper), max(lower, upper)


def classify_brand_origin(brand: Any, importer: Any = None) -> str:
    brand_text = str(brand or "").strip()
    importer_text = str(importer or "").strip()
    if brand_text in IMPORTED_BRANDS:
        return "进口品牌"
    if brand_text in DOMESTIC_BRANDS:
        return "国产品牌"
    if importer_text and importer_text.lower() != "nan":
        return "进口品牌"
    return "未识别"


@st.cache_data(ttl=60)
def load_sku_options(keyword: str = "", limit: int = 200) -> pd.DataFrame:
    sql = f"""
        SELECT
            b.sku_id,
            COALESCE(NULLIF(b.brand, ''), w.brand) AS brand,
            COALESCE(NULLIF(b.product_name, ''), w.product_name) AS product_name,
            b.confidence_score,
            b.factory_business_tags,
            b.process_structure_tags,
            b.process_attention_tags,
            b.quality_validation_tags,
            w.importer,
            w.ingredient_composition AS ingredient_text,
            COALESCE(w.guarantee_crude_protein_value, w.crude_protein_pct, w.agg_crude_protein_value) AS crude_protein,
            w.guarantee_crude_fat_value AS crude_fat,
            w.guarantee_crude_fiber_value AS crude_fiber
        FROM {B2B_TAG_TABLE} b
        LEFT JOIN catfood_sku_label_wide w
            ON w.product_key = b.sku_id
        WHERE (
            %s = ''
            OR b.sku_id LIKE %s
            OR b.brand LIKE %s
            OR b.product_name LIKE %s
            OR w.brand LIKE %s
            OR w.product_name LIKE %s
        )
        ORDER BY brand, product_name, b.sku_id
        LIMIT %s
    """
    like = f"%{keyword}%"
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (keyword, like, like, like, like, like, limit))
            rows = cursor.fetchall()
    return pd.DataFrame(rows)


@st.cache_data(ttl=300)
def load_all_sku_options(limit: int = 5000) -> pd.DataFrame:
    return load_sku_options("", limit)


@st.cache_data(ttl=300)
def load_filter_options() -> Dict[str, List[str]]:
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT factory_business_tags FROM {B2B_TAG_TABLE}")
            tag_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT DISTINCT black_chin_risk_level
                FROM catfood_sku_label_wide
                WHERE black_chin_risk_level IS NOT NULL AND black_chin_risk_level <> ''
                ORDER BY black_chin_risk_level
                """
            )
            black_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT DISTINCT soft_stool_risk_level
                FROM catfood_sku_label_wide
                WHERE soft_stool_risk_level IS NOT NULL AND soft_stool_risk_level <> ''
                ORDER BY soft_stool_risk_level
                """
            )
            soft_rows = cursor.fetchall()

    tag_set = set()
    for row in tag_rows:
        tag_set.update(safe_json_loads(row.get("factory_business_tags")))
    return {
        "factory_tags": sorted(tag_set),
        "black_chin_levels": [row["black_chin_risk_level"] for row in black_rows],
        "soft_stool_levels": [row["soft_stool_risk_level"] for row in soft_rows],
    }


@st.cache_data(ttl=300)
def load_nutrition_score_reference() -> Dict[str, Any]:
    fields = ["protein_score", "fat_score", "carb_score", "fiber_score", "prebiotic_score", "antioxidant_score"]
    sql = f"""
        SELECT sku_id, {", ".join(fields)}
        FROM sku_feature_input
        WHERE sku_id IS NOT NULL
    """
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    df = pd.DataFrame(rows)
    score_map: Dict[str, Dict[str, float]] = {}
    thresholds: Dict[str, Dict[str, float]] = {}
    if df.empty:
        return {"scores": score_map, "thresholds": thresholds}

    for field in fields:
        values = pd.to_numeric(df[field], errors="coerce").dropna()
        if values.empty:
            continue
        thresholds[field] = {
            "medium": float(values.quantile(0.34)),
            "high": float(values.quantile(0.67)),
        }

    for _, row in df.iterrows():
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id:
            continue
        scores = {}
        for field in fields:
            value = row.get(field)
            if value is None or pd.isna(value):
                continue
            scores[field] = safe_float(value, None)
        score_map[sku_id] = scores
    return {"scores": score_map, "thresholds": thresholds}


@st.cache_data(ttl=300)
def load_ingredient_role_reference() -> Dict[str, Dict[str, float]]:
    role_fields = [field for field, _, _ in getattr(b2b.similarity_recall, "INGREDIENT_LABEL_ROLE_FIELDS", INGREDIENT_LABEL_ROLE_FIELDS)]
    sql = f"""
        SELECT product_key AS sku_id, {", ".join(role_fields)}
        FROM catfood_sku_label_wide
        WHERE product_key IS NOT NULL
    """
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

    result: Dict[str, Dict[str, float]] = {}
    for row in rows:
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id:
            continue
        if hasattr(b2b.similarity_recall, "build_label_based_ingredient_vector"):
            result[sku_id] = b2b.similarity_recall.build_label_based_ingredient_vector(row)
    return result


def split_mapping_text(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return []
    normalized = re.sub(r"[；;，,、/|]+", "、", text)
    return [item.strip() for item in normalized.split("、") if item.strip()]


@st.cache_data(ttl=300)
def load_path_mapping_rules() -> Dict[str, List[Dict[str, Any]]]:
    if not PATH_MAPPING_XLSX.exists():
        return {}
    raw = pd.read_excel(PATH_MAPPING_XLSX, sheet_name="原料路径总表", header=None)
    header_idx = None
    for idx, row in raw.iterrows():
        if "功能模块" in [str(value).strip() for value in row.tolist()]:
            header_idx = idx
            break
    if header_idx is None:
        return {}

    headers = [str(value).strip() for value in raw.iloc[header_idx].tolist()]
    df = raw.iloc[header_idx + 1:].copy()
    df.columns = headers
    required = ["功能模块", "原料路径", "典型原料/底层标签", "工艺响应关注", "成品指标", "用户反馈观察点"]
    df = df[[col for col in required if col in df.columns]].dropna(subset=["功能模块", "原料路径"])

    rules: Dict[str, List[Dict[str, Any]]] = {}
    for _, row in df.iterrows():
        module = str(row.get("功能模块") or "").strip()
        path = str(row.get("原料路径") or "").strip()
        if not module or not path:
            continue
        rules.setdefault(module, []).append({
            "path": path,
            "keywords": split_mapping_text(row.get("典型原料/底层标签")),
            "process_points": str(row.get("工艺响应关注") or "").strip(),
            "indicators": str(row.get("成品指标") or "").strip(),
            "feedback": str(row.get("用户反馈观察点") or "").strip(),
        })
    return rules


def collect_json_texts(value: Any) -> List[str]:
    parsed = parse_json_cell(value)
    texts: List[str] = []
    if isinstance(parsed, dict):
        for key, item in parsed.items():
            texts.append(str(key))
            texts.extend(collect_json_texts(item))
    elif isinstance(parsed, list):
        for item in parsed:
            texts.extend(collect_json_texts(item))
    elif parsed not in (None, ""):
        texts.extend(split_mapping_text(parsed))
    return texts


def add_label(labels: List[str], value: Any):
    for item in split_mapping_text(value):
        if item not in labels:
            labels.append(item)


def add_json_labels(labels: List[str], value: Any):
    for item in collect_json_texts(value):
        text = str(item or "").strip()
        if text and text not in labels:
            labels.append(text)


def add_prebiotic_feature_labels(labels: List[str], value: Any):
    parsed = parse_json_cell(value)
    if not isinstance(parsed, dict):
        return
    tag_detail = parsed.get("ingredient_tag_detail")
    if not isinstance(tag_detail, dict):
        return
    for ingredient, detail in tag_detail.items():
        if not isinstance(detail, dict):
            continue
        category = str(detail.get("ingredient_category") or "")
        prebiotic_functions = [str(item) for item in as_list(detail.get("prebiotic_functions"))]
        fiber_functions = [str(item) for item in as_list(detail.get("fiber_functions"))]
        function_text = " ".join(prebiotic_functions + fiber_functions)
        if (
            "益生元" in category
            or prebiotic_functions
            or any(keyword in function_text for keyword in ["供菌", "SCFA", "发酵底物", "有益菌"])
        ):
            text = str(ingredient or "").strip()
            if text and text not in labels:
                labels.append(text)


@st.cache_data(ttl=300)
def load_material_path_reference() -> Dict[str, Dict[str, List[str]]]:
    result: Dict[str, Dict[str, List[str]]] = {}

    def ensure(sku_id: Any) -> Dict[str, List[str]]:
        key = str(sku_id or "").strip()
        if not key:
            return {
                "labels": [],
                "primary_labels": [],
                "secondary_labels": [],
                "prebiotic_labels": [],
                "protein_details": [],
                "plant_protein_labels": [],
                "carb_details": [],
                "fiber_details": [],
                "fat_details": [],
                "antioxidant_details": [],
                "raw_ingredient_items": [],
            }
        result.setdefault(key, {
            "labels": [],
            "primary_labels": [],
            "secondary_labels": [],
            "prebiotic_labels": [],
            "protein_details": [],
            "plant_protein_labels": [],
            "carb_details": [],
            "fiber_details": [],
            "fat_details": [],
            "antioxidant_details": [],
            "raw_ingredient_items": [],
        })
        return result[key]

    with get_conn() as conn:
        with conn.cursor() as cursor:
            protein_columns = [
                "product_key",
                "animal_sources",
                "animal_source_level1_categories",
                "animal_source_level2_sources",
                "protein_source_details",
                "primary_meat_source_species",
                "secondary_meat_source_species",
                "primary_meat_source_type",
                "secondary_meat_source_type",
                "protein_source_origin",
                "plant_protein_labels",
            ]
            protein_select_columns = _select_existing_columns(cursor, "protein_source_aggregate", protein_columns)
            cursor.execute(f"""
                SELECT {protein_select_columns}
                FROM protein_source_aggregate
                WHERE product_key IS NOT NULL
            """)
            for row in cursor.fetchall():
                bucket = ensure(row.get("product_key"))
                labels = bucket["labels"]
                for field, value in row.items():
                    if field != "product_key":
                        add_label(labels, value)
                add_label(bucket["protein_details"], row.get("protein_source_details"))
                add_label(bucket["plant_protein_labels"], row.get("plant_protein_labels"))
                for field in ("primary_meat_source_species", "primary_meat_source_type"):
                    add_label(bucket["primary_labels"], row.get(field))
                for field in ("secondary_meat_source_species", "secondary_meat_source_type"):
                    add_label(bucket["secondary_labels"], row.get(field))

            fiber_select_columns, fiber_key_where = _keyed_select_existing_columns(
                cursor,
                "catfood_fiber_feature_json",
                ["raw_ingredient_text", "ingredient_feature_json", "starch_ingredients_json"],
            )
            cursor.execute(f"""
                SELECT {fiber_select_columns}
                FROM catfood_fiber_feature_json
                WHERE {fiber_key_where}
            """)
            for row in cursor.fetchall():
                bucket = ensure(row.get("product_key"))
                labels = bucket["labels"]
                add_label(labels, row.get("raw_ingredient_text"))
                for item in split_ingredient_text(row.get("raw_ingredient_text")):
                    add_label(bucket["raw_ingredient_items"], item)
                add_json_labels(labels, row.get("ingredient_feature_json"))
                add_json_labels(labels, row.get("starch_ingredients_json"))
                add_prebiotic_feature_labels(bucket["prebiotic_labels"], row.get("ingredient_feature_json"))
                parsed_feature = parse_json_cell(row.get("ingredient_feature_json"))
                if isinstance(parsed_feature, dict):
                    detail = parsed_feature.get("ingredient_tag_detail")
                    if isinstance(detail, dict):
                        for ingredient, info in detail.items():
                            text = str(ingredient or "").strip()
                            if not text:
                                continue
                            if isinstance(info, dict) and str(info.get("ingredient_category") or "") == "膳食纤维":
                                add_label(bucket["fiber_details"], text)
                for starch_item in as_list(parse_json_cell(row.get("starch_ingredients_json"))):
                    if isinstance(starch_item, dict):
                        add_label(bucket["carb_details"], starch_item.get("ingredient_name"))

            fat_select_columns, fat_key_where = _keyed_select_existing_columns(
                cursor,
                "catfood_fat_material_features",
                [
                    "fat_sources",
                    "fat_source_types",
                    "antioxidant_sources",
                    "antioxidant_types",
                    "micronutrient_sources",
                    "micronutrient_types",
                    "omega6_sources",
                    "omega3_sources",
                    "ingredient_composition",
                ],
            )
            cursor.execute(f"""
                SELECT {fat_select_columns}
                FROM catfood_fat_material_features
                WHERE {fat_key_where}
            """)
            for row in cursor.fetchall():
                bucket = ensure(row.get("product_key"))
                labels = bucket["labels"]
                for field, value in row.items():
                    if field != "product_key":
                        add_label(labels, value)
                for item in split_ingredient_text(row.get("ingredient_composition")):
                    add_label(bucket["raw_ingredient_items"], item)
                for field in ("fat_sources", "omega6_sources", "omega3_sources"):
                    add_label(bucket["fat_details"], row.get(field))
                for field in ("antioxidant_sources", "antioxidant_types", "micronutrient_sources"):
                    add_label(bucket["antioxidant_details"], row.get(field))

    return result


FORMULA_TAG_TABLES = [
    "sku_nutrition_structure_tags",
    "sku_function_structure_tags",
    "sku_ingredient_path_tags",
]


def mysql_placeholders(values: List[Any]) -> str:
    return ", ".join(["%s"] * len(values))


def table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


@st.cache_data(ttl=300)
def load_formula_capability_profiles(sku_ids: tuple[str, ...]) -> Dict[str, Dict[str, Any]]:
    cleaned_ids = tuple(dict.fromkeys(str(sku_id or "").strip() for sku_id in sku_ids if str(sku_id or "").strip()))
    if not cleaned_ids:
        return {}

    feature_base_df = load_formula_base_tags_from_feature_tables()
    if not feature_base_df.empty:
        feature_base_df = feature_base_df[feature_base_df["sku_id"].isin(cleaned_ids)].copy()
        feature_capability_df = sku_function_structure_rules.generate_sku_formula_capabilities(feature_base_df)
        if not feature_capability_df.empty:
            return {
                str(sku_id): profile_from_capability_records(group.to_dict("records"))
                for sku_id, group in feature_capability_df.groupby("sku_id")
            }

    rows: List[Dict[str, Any]] = []
    with get_conn() as conn:
        with conn.cursor() as cursor:
            for table_name in FORMULA_TAG_TABLES:
                if not table_exists(cursor, table_name):
                    continue
                sql = f"""
                    SELECT
                        sku_id,
                        sku_name,
                        tag_key,
                        tag_name,
                        tag_dimension,
                        COALESCE(tag_score, 1.0) AS tag_score,
                        COALESCE(evidence_text, '') AS evidence_text,
                        %s AS source_table
                    FROM {table_name}
                    WHERE sku_id IN ({mysql_placeholders(list(cleaned_ids))})
                """
                cursor.execute(sql, (table_name, *cleaned_ids))
                rows.extend(cursor.fetchall())

    if not rows:
        return {}

    base_df = pd.DataFrame(rows)
    capability_df = sku_function_structure_rules.generate_sku_formula_capabilities(base_df)
    if capability_df.empty:
        return {}

    profiles: Dict[str, Dict[str, Any]] = {}
    for sku_id, group in capability_df.groupby("sku_id"):
        role_order = {"主配方能力": 0, "辅助配方能力": 1, "风险短板": 2, "补充配方能力": 3}
        records = sorted(
            group.to_dict("records"),
            key=lambda item: (
                role_order.get(str(item.get("display_role") or ""), 9),
                -safe_float(item.get("capability_score"), 0.0),
            ),
        )
        main_caps = [item for item in records if item.get("display_role") == "主配方能力"]
        assist_caps = [item for item in records if item.get("display_role") == "辅助配方能力"]
        risk_caps = [item for item in records if item.get("display_role") == "风险短板"]
        top_records = records[:6]
        profiles[str(sku_id)] = {
            "main_capabilities": main_caps,
            "assist_capabilities": assist_caps,
            "risk_shortcomings": risk_caps,
            "all_capabilities": top_records,
            "main_text": " / ".join(
                f"{item.get('capability_name')}（{item.get('capability_level')}，{item.get('capability_score')}）"
                for item in main_caps[:2]
            ) or "暂无",
            "assist_text": " / ".join(
                f"{item.get('capability_name')}（{item.get('capability_level')}）"
                for item in assist_caps[:4]
            ) or "暂无",
            "description_text": "；".join(
                str(item.get("description") or "")
                for item in top_records[:3]
                if item.get("description")
            ) or "暂无",
            "risk_text": " / ".join(
                f"{item.get('capability_name')}（{item.get('capability_level')}）"
                for item in risk_caps[:2]
            ) or "暂无",
            "evidence_text": formula_evidence_text(top_records),
        }
    return profiles


def formula_evidence_text(capabilities: List[Dict[str, Any]]) -> str:
    evidence_names: List[str] = []
    for item in capabilities[:4]:
        for detail in as_list(parse_json_cell(item.get("evidence_detail_json"))):
            if isinstance(detail, dict):
                tag_name = str(detail.get("tag_name") or "").strip()
                if tag_name and tag_name not in evidence_names:
                    evidence_names.append(tag_name)
    return " / ".join(evidence_names[:8]) if evidence_names else "暂无"


def add_formula_tag(rows: List[Dict[str, Any]], sku_id: str, sku_name: str, tag_key: str, tag_name: str, dimension: str, score: float, evidence: str):
    rows.append({
        "sku_id": sku_id,
        "sku_name": sku_name,
        "tag_key": tag_key,
        "tag_name": tag_name,
        "tag_dimension": dimension,
        "tag_score": score,
        "evidence_text": evidence,
        "source_table": "current_similarity_result_fallback",
    })


def _text_contains(text: str, keywords: List[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _feature_product_name(row: Dict[str, Any]) -> str:
    return str(row.get("product_name") or row.get("brand") or row.get("brand_name") or row.get("product_key") or "").strip()


@st.cache_data(ttl=300)
def load_formula_base_tags_from_feature_tables() -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []

    with get_conn() as conn:
        with conn.cursor() as cursor:
            protein_columns = [
                "product_key",
                "brand_name",
                "product_name",
                "animal_sources",
                "animal_source_level1_categories",
                "animal_source_level2_sources",
                "protein_source_details",
                "primary_meat_source_species",
                "secondary_meat_source_species",
                "primary_meat_source_type",
                "secondary_meat_source_type",
                "plant_protein_labels",
                "guarantee_crude_protein_value",
            ]
            protein_select_columns = _select_existing_columns(cursor, "protein_source_aggregate", protein_columns)
            cursor.execute(f"""
                SELECT {protein_select_columns}
                FROM protein_source_aggregate
                WHERE product_key IS NOT NULL
            """)
            for row in cursor.fetchall():
                sku_id = str(row.get("product_key") or "").strip()
                sku_name = _feature_product_name(row)
                detail_text = " ".join(str(value or "") for value in row.values())
                animal_sources = split_mapping_text(row.get("animal_source_level2_sources") or row.get("animal_sources"))
                animal_level1 = split_mapping_text(row.get("animal_source_level1_categories"))
                protein_value = safe_float(row.get("guarantee_crude_protein_value"), 0.0)
                primary_species = str(row.get("primary_meat_source_species") or "").strip()
                secondary_species = str(row.get("secondary_meat_source_species") or "").strip()
                primary_type = str(row.get("primary_meat_source_type") or "").strip()
                secondary_type = str(row.get("secondary_meat_source_type") or "").strip()

                if animal_sources or protein_value >= 35:
                    add_formula_tag(rows, sku_id, sku_name, "high_animal_protein", "动物蛋白结构明确", "protein", 1.5, row.get("protein_source_details") or "")
                if protein_value >= 35:
                    add_formula_tag(rows, sku_id, sku_name, "crude_protein_high", "粗蛋白偏高", "nutrition", 1.2, f"粗蛋白 {protein_value:g}")
                if len(animal_sources) == 1 and not row.get("plant_protein_labels"):
                    add_formula_tag(rows, sku_id, sku_name, "single_animal_protein", "单一肉源", "protein", 1.8, row.get("animal_source_level2_sources") or "")
                    add_formula_tag(rows, sku_id, sku_name, "simple_protein_structure", "蛋白结构简单", "protein", 1.0, "肉源数量为 1 且无明显植物蛋白补强")
                if len(animal_sources) >= 2:
                    add_formula_tag(rows, sku_id, sku_name, "multiple_animal_protein", "多动物蛋白", "protein", 1.6, row.get("animal_source_level2_sources") or "")
                    add_formula_tag(rows, sku_id, sku_name, "multi_meat_source", "多肉源结构", "protein", 1.5, row.get("protein_source_details") or "")
                if len(animal_level1) >= 2:
                    add_formula_tag(rows, sku_id, sku_name, "multi_meat_source", "跨类多肉源结构", "protein", 1.8, row.get("animal_source_level1_categories") or "")
                if primary_species and secondary_species and primary_species == secondary_species and primary_type != secondary_type:
                    add_formula_tag(rows, sku_id, sku_name, "same_species_multi_form", "同源多形态", "protein", 1.5, f"{primary_species}: {primary_type}+{secondary_type}")
                if _text_contains(detail_text, ["鲜肉", "鲜鸡", "鲜鸭", "鲜鱼", "鲜牛", "鲜羊"]):
                    add_formula_tag(rows, sku_id, sku_name, "fresh_meat_used", "鲜肉使用", "protein", 1.6, row.get("protein_source_details") or "")
                if _text_contains(detail_text, ["冻肉", "冻鸡", "冻鸭", "冷冻", "冻去骨"]):
                    add_formula_tag(rows, sku_id, sku_name, "frozen_meat_used", "冻肉使用", "protein", 1.5, row.get("protein_source_details") or "")
                if _text_contains(detail_text, ["肉粉", "鱼粉", "鸡肉粉", "鸭肉粉", "火鸡肉粉"]):
                    add_formula_tag(rows, sku_id, sku_name, "meat_meal_used", "肉粉/鱼粉使用", "protein", 1.5, row.get("protein_source_details") or "")
                if _text_contains(detail_text, ["鱼", "三文鱼", "鳕鱼", "鲱鱼", "鲭鱼", "沙丁鱼"]):
                    add_formula_tag(rows, sku_id, sku_name, "fish_meat_source", "鱼源蛋白", "protein", 1.4, row.get("protein_source_details") or "")
                if _text_contains(detail_text, ["水解"]):
                    add_formula_tag(rows, sku_id, sku_name, "hydrolyzed_protein_used", "水解蛋白", "protein", 1.8, row.get("protein_source_details") or "")
                if _text_contains(detail_text, ["肝", "心", "肾", "肺", "脾", "内脏"]):
                    add_formula_tag(rows, sku_id, sku_name, "organ_involved", "内脏参与", "protein", 1.1, row.get("protein_source_details") or "")
                if row.get("plant_protein_labels") or _text_contains(detail_text, ["植物蛋白", "豌豆蛋白", "小麦蛋白", "玉米蛋白", "大豆蛋白"]):
                    add_formula_tag(rows, sku_id, sku_name, "plant_protein_high", "植物蛋白补强", "protein", 1.3, row.get("plant_protein_labels") or row.get("protein_source_details") or "")
                else:
                    add_formula_tag(rows, sku_id, sku_name, "plant_protein_low", "无明显植物蛋白补强", "protein", 1.0, "protein_source_aggregate 未识别植物蛋白补强")

            fat_select_columns, fat_key_where = _keyed_select_existing_columns(
                cursor,
                "catfood_fat_material_features",
                [
                    "brand",
                    "product_name",
                    "fat_sources",
                    "fat_source_types",
                    "antioxidant_sources",
                    "antioxidant_types",
                    "omega6_sources",
                    "omega3_sources",
                    "guarantee_crude_fat_value",
                ],
            )
            cursor.execute(f"""
                SELECT {fat_select_columns}
                FROM catfood_fat_material_features
                WHERE {fat_key_where}
            """)
            for row in cursor.fetchall():
                sku_id = str(row.get("product_key") or "").strip()
                sku_name = _feature_product_name(row)
                fat_text = " ".join(str(value or "") for value in row.values())
                fat_value = safe_float(row.get("guarantee_crude_fat_value"), 0.0)
                if fat_value >= 18:
                    add_formula_tag(rows, sku_id, sku_name, "fat_high", "脂肪偏高", "fat", 1.2, f"粗脂肪 {fat_value:g}")
                elif fat_value >= 12:
                    add_formula_tag(rows, sku_id, sku_name, "fat_moderate", "脂肪适中", "fat", 1.0, f"粗脂肪 {fat_value:g}")
                if _text_contains(fat_text, ["动物脂肪", "鸡油", "鸭油", "牛油", "禽脂"]):
                    add_formula_tag(rows, sku_id, sku_name, "animal_fat_structure", "动物脂肪结构", "fat", 1.2, row.get("fat_sources") or "")
                if row.get("omega3_sources") or _text_contains(fat_text, ["鱼油", "三文鱼", "鲱鱼", "鲭鱼", "鳕鱼", "亚麻籽"]):
                    add_formula_tag(rows, sku_id, sku_name, "omega3_support", "Omega-3支持", "fat", 1.3, row.get("omega3_sources") or "")
                if _text_contains(fat_text, ["鱼油"]):
                    add_formula_tag(rows, sku_id, sku_name, "fish_oil_used", "鱼油使用", "fat", 1.2, row.get("fat_sources") or row.get("omega3_sources") or "")
                if _text_contains(fat_text, ["亚麻籽", "亚麻仁", "亚麻籽油"]):
                    add_formula_tag(rows, sku_id, sku_name, "flaxseed_used", "亚麻籽/Omega支持", "fat", 1.1, fat_text)
                if row.get("omega6_sources") and not row.get("omega3_sources"):
                    add_formula_tag(rows, sku_id, sku_name, "omega6_pressure_high", "Omega-6压力偏高", "fat", 1.0, row.get("omega6_sources") or "")
                if row.get("antioxidant_sources") or row.get("antioxidant_types") or _text_contains(fat_text, ["生育酚", "维生素E", "迷迭香", "迷叠香", "茶多酚", "抗氧化"]):
                    add_formula_tag(rows, sku_id, sku_name, "antioxidant_system", "抗氧化体系", "antioxidant", 1.3, row.get("antioxidant_sources") or row.get("antioxidant_types") or "")
                    add_formula_tag(rows, sku_id, sku_name, "vitamin_e_used", "维生素E/抗氧化剂使用", "antioxidant", 1.0, row.get("antioxidant_sources") or "")
                if _text_contains(fat_text, ["迷迭香", "迷叠香"]):
                    add_formula_tag(rows, sku_id, sku_name, "rosemary_extract_used", "迷迭香提取物支持", "antioxidant", 1.0, fat_text)
                if "茶多酚" in fat_text:
                    add_formula_tag(rows, sku_id, sku_name, "tea_polyphenol_used", "植物多酚支持", "antioxidant", 1.0, fat_text)

            fiber_select_columns, fiber_key_where = _keyed_select_existing_columns(
                cursor,
                "catfood_fiber_feature_json",
                ["brand", "product_name", "raw_ingredient_text", "ingredient_feature_json", "starch_ingredients_json"],
            )
            cursor.execute(f"""
                SELECT {fiber_select_columns}
                FROM catfood_fiber_feature_json
                WHERE {fiber_key_where}
            """)
            for row in cursor.fetchall():
                sku_id = str(row.get("product_key") or "").strip()
                sku_name = _feature_product_name(row)
                raw_text = str(row.get("raw_ingredient_text") or "")
                feature = parse_json_cell(row.get("ingredient_feature_json"))
                starch_items = as_list(parse_json_cell(row.get("starch_ingredients_json")))
                fiber_functions: List[str] = []
                if isinstance(feature, dict):
                    detail = feature.get("ingredient_tag_detail") or {}
                    if isinstance(detail, dict):
                        for ingredient, info in detail.items():
                            if not isinstance(info, dict):
                                continue
                            evidence = str(ingredient or "")
                            if evidence == "南瓜" and "南瓜籽" in raw_text and "南瓜、" not in raw_text and "南瓜，" not in raw_text:
                                continue
                            funcs = [str(item) for item in as_list(info.get("fiber_functions"))]
                            pre_funcs = [str(item) for item in as_list(info.get("prebiotic_functions"))]
                            fiber_functions.extend(funcs)
                            joined_funcs = " ".join(funcs + pre_funcs)
                            if "吸水成形" in joined_funcs:
                                add_formula_tag(rows, sku_id, sku_name, "soluble_fiber_structure", "吸水成形纤维", "fiber", 1.2, evidence)
                            if "增加粪便骨架" in joined_funcs:
                                add_formula_tag(rows, sku_id, sku_name, "insoluble_fiber_structure", "粪便骨架纤维", "fiber", 1.2, evidence)
                            if _text_contains(joined_funcs, ["供菌", "SCFA", "益生元", "发酵底物"]):
                                add_formula_tag(rows, sku_id, sku_name, "prebiotic_used", "供菌底物/益生元支持", "prebiotic", 1.2, evidence)
                fiber_text = " ".join(fiber_functions)
                if "吸水成形" in fiber_text and "增加粪便骨架" in fiber_text:
                    add_formula_tag(rows, sku_id, sku_name, "compound_fiber", "复合纤维结构", "fiber", 1.4, "同时命中吸水成形和粪便骨架支持")
                if len(set(fiber_functions)) >= 3:
                    add_formula_tag(rows, sku_id, sku_name, "fiber_high", "纤维功能丰富", "fiber", 1.0, "纤维功能标签较多")

                has_grain = False
                has_legume_or_tuber = False
                for item in starch_items:
                    if not isinstance(item, dict):
                        continue
                    category = str(item.get("category") or "")
                    ingredient = str(item.get("ingredient_name") or "")
                    evidence = f"{ingredient}｜{category}"
                    if "豆类" in category or _text_contains(ingredient, ["豌豆", "扁豆", "鹰嘴豆", "兵豆"]):
                        has_legume_or_tuber = True
                        add_formula_tag(rows, sku_id, sku_name, "legume_starch_structure", "豆类碳水结构", "carb", 1.4, evidence)
                    if "薯" in category or _text_contains(ingredient, ["马铃薯", "土豆", "红薯", "甘薯", "木薯"]):
                        has_legume_or_tuber = True
                        add_formula_tag(rows, sku_id, sku_name, "tuber_starch_structure", "薯类碳水结构", "carb", 1.4, evidence)
                    if "谷物" in category or _text_contains(ingredient, ["米", "玉米", "小麦", "燕麦", "高粱", "大麦"]):
                        has_grain = True
                        add_formula_tag(rows, sku_id, sku_name, "grain_starch_structure", "谷物淀粉结构", "carb", 1.2, evidence)
                    if "精制淀粉" in category or "淀粉" in ingredient:
                        add_formula_tag(rows, sku_id, sku_name, "refined_starch_structure", "精制淀粉结构", "carb", 1.3, evidence)
                if has_legume_or_tuber and not has_grain and not _text_contains(raw_text, ["大米", "玉米", "小麦", "燕麦", "糙米", "高粱", "大麦"]):
                    add_formula_tag(rows, sku_id, sku_name, "grain_free", "无谷结构", "carb", 1.5, "淀粉来源为豆薯类且未识别谷物")

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset=["sku_id", "tag_key", "evidence_text"]).reset_index(drop=True)
    df["source_table"] = "protein_source_aggregate/catfood_fat_material_features/catfood_fiber_feature_json"
    return df


def fallback_base_tags_from_product_row(row: Dict[str, Any]) -> pd.DataFrame:
    sku_id = str(row.get("sku_id") or "").strip()
    sku_name = str(row.get("product_name") or sku_id).strip()
    ingredient_text = str(row.get("ingredient_text") or "")
    factory_tags = " ".join(str(item) for item in as_list(row.get("factory_business_tags")))
    process_tags = " ".join(str(item) for item in as_list(row.get("process_structure_tags")) + as_list(row.get("main_process_tags")))
    text = f"{ingredient_text} {factory_tags} {process_tags}"
    rows: List[Dict[str, Any]] = []

    protein = safe_float(row.get("crude_protein"), 0.0)
    fat = safe_float(row.get("crude_fat"), 0.0)
    fiber = safe_float(row.get("crude_fiber"), 0.0)

    if protein >= 35 or "高动物蛋白" in text or "高肉" in text:
        add_formula_tag(rows, sku_id, sku_name, "high_animal_protein", "高动物蛋白", "protein", 2.0, f"粗蛋白 {protein:g}；{factory_tags}")
        add_formula_tag(rows, sku_id, sku_name, "crude_protein_high", "粗蛋白偏高", "nutrition", 1.5, f"粗蛋白 {protein:g}")
    if any(keyword in text for keyword in ["鲜肉", "鲜鸡", "鲜鸭", "鲜牛", "鲜鱼", "冻肉", "冻牛", "冻鸡", "鲜肉添加"]):
        add_formula_tag(rows, sku_id, sku_name, "fresh_meat_used", "鲜肉/冻肉使用", "protein", 1.8, "配方或业务标签命中鲜肉/冻肉路径")
    if any(keyword in text for keyword in ["冻干", "生骨肉", "鲜肉冻干"]):
        add_formula_tag(rows, sku_id, sku_name, "freeze_dried_used", "冻干使用", "protein", 2.0, "产品名或配方命中冻干")
        add_formula_tag(rows, sku_id, sku_name, "fresh_meat_freeze_dried_used", "鲜肉冻干结构", "protein", 1.8, "产品名或配方命中鲜肉冻干/生骨肉")
    if "生骨肉" in text:
        add_formula_tag(rows, sku_id, sku_name, "raw_bone_meat_used", "生骨肉结构", "protein", 2.2, "产品名或配方命中生骨肉")
    if any(keyword in text for keyword in ["肉粉", "鱼粉", "鸡肉粉", "鸭肉粉", "白鱼粉", "肉粉型配方", "动物蛋白干粉"]):
        add_formula_tag(rows, sku_id, sku_name, "meat_meal_used", "肉粉/鱼粉使用", "protein", 1.5, "配方或工艺标签命中肉粉/鱼粉路径")
    if "豌豆蛋白" in text or "植物蛋白" in text:
        add_formula_tag(rows, sku_id, sku_name, "plant_protein_high", "植物蛋白补强", "protein", 1.2, "配方命中植物蛋白")
    meat_hit_count = sum(1 for keyword in ["鸡", "鸭", "牛", "羊", "兔", "三文鱼", "白鱼", "鳕鱼", "鲱鱼", "乳鸽", "火鸡", "鹿"] if keyword in text)
    if meat_hit_count == 1 and not any(keyword in text for keyword in ["豌豆蛋白", "大豆蛋白", "玉米蛋白粉", "植物蛋白"]):
        add_formula_tag(rows, sku_id, sku_name, "single_animal_protein", "单一肉源", "protein", 1.8, "配方命中单一主要动物来源")
        add_formula_tag(rows, sku_id, sku_name, "simple_protein_structure", "蛋白结构简单", "protein", 1.0, "未识别明显多肉源或植物蛋白补强")
    if not any(keyword in text for keyword in ["豌豆蛋白", "大豆蛋白", "玉米蛋白粉", "植物蛋白"]):
        add_formula_tag(rows, sku_id, sku_name, "plant_protein_low", "无明显植物蛋白补强", "protein", 1.0, "未识别明显植物蛋白粉补强")
    if meat_hit_count >= 2:
        add_formula_tag(rows, sku_id, sku_name, "multiple_animal_protein", "多动物蛋白", "protein", 1.6, "配方命中多个动物蛋白来源")
        add_formula_tag(rows, sku_id, sku_name, "multi_meat_source", "多肉源结构", "protein", 1.5, "配方命中多肉源")
    if any(keyword in text for keyword in ["三文鱼", "白鱼", "鲱鱼", "鳕鱼", "鱼肉", "鱼粉"]):
        add_formula_tag(rows, sku_id, sku_name, "fish_meat_source", "鱼肉/鱼油来源", "protein", 1.4, "配方命中鱼类或鱼油来源")
    if any(keyword in text for keyword in ["鸡肝", "鸭肝", "牛肝", "肝", "心", "肾", "肺", "脾", "内脏"]):
        add_formula_tag(rows, sku_id, sku_name, "organ_involved", "内脏参与", "protein", 1.1, "配方命中内脏或风味型动物副产物")

    if "无谷" in text:
        add_formula_tag(rows, sku_id, sku_name, "grain_free", "无谷结构", "carb", 2.0, factory_tags or "业务标签命中无谷")
    if any(keyword in text for keyword in ["豌豆", "鹰嘴豆", "扁豆", "兵豆", "豆类淀粉"]):
        add_formula_tag(rows, sku_id, sku_name, "legume_starch_structure", "豆类碳水结构", "carb", 1.5, "配方命中豆类碳水")
    if any(keyword in text for keyword in ["马铃薯", "土豆", "红薯", "甘薯", "木薯", "薯类"]):
        add_formula_tag(rows, sku_id, sku_name, "tuber_starch_structure", "薯类碳水结构", "carb", 1.5, "配方命中薯类/块茎碳水")
    if any(keyword in text for keyword in ["大米", "玉米", "小麦", "燕麦", "糙米"]):
        add_formula_tag(rows, sku_id, sku_name, "grain_starch_structure", "谷物淀粉结构", "carb", 1.2, "配方命中谷物碳水")

    if any(keyword in text for keyword in ["菊粉", "菊苣", "低聚果糖", "FOS", "MOS", "益生元"]):
        add_formula_tag(rows, sku_id, sku_name, "prebiotic_used", "益生元添加", "prebiotic", 1.4, "配方命中益生元/发酵底物")
        add_formula_tag(rows, sku_id, sku_name, "soluble_fiber_structure", "可溶/发酵纤维结构", "fiber", 1.2, "配方命中菊粉/菊苣/低聚糖")
    if any(keyword in text for keyword in ["纤维素", "豌豆纤维", "鹰嘴豆纤维", "木质纤维", "苜蓿"]):
        add_formula_tag(rows, sku_id, sku_name, "insoluble_fiber_structure", "不可溶纤维结构", "fiber", 1.2, "配方命中结构型纤维")
    if fiber >= 4:
        add_formula_tag(rows, sku_id, sku_name, "fiber_high", "纤维支持偏高", "fiber", 1.0, f"粗纤维 {fiber:g}")

    if fat >= 18:
        add_formula_tag(rows, sku_id, sku_name, "fat_high", "脂肪偏高", "fat", 1.2, f"粗脂肪 {fat:g}")
    elif fat >= 12:
        add_formula_tag(rows, sku_id, sku_name, "fat_moderate", "脂肪适中", "fat", 1.0, f"粗脂肪 {fat:g}")
    if any(keyword in text for keyword in ["鱼油", "三文鱼油", "Omega", "欧米伽"]):
        add_formula_tag(rows, sku_id, sku_name, "omega3_support", "Omega-3支持", "fat", 1.3, "配方命中鱼油/Omega")
        add_formula_tag(rows, sku_id, sku_name, "fish_oil_used", "鱼油使用", "fat", 1.2, "配方命中鱼油")
    if any(keyword in text for keyword in ["亚麻籽", "亚麻仁", "亚麻籽油"]):
        add_formula_tag(rows, sku_id, sku_name, "flaxseed_used", "亚麻籽/Omega支持", "fat", 1.1, "配方命中亚麻籽或亚麻籽油")
    if any(keyword in text for keyword in ["鸡油", "鸭油", "牛油", "动物脂肪"]):
        add_formula_tag(rows, sku_id, sku_name, "animal_fat_structure", "动物脂肪结构", "fat", 1.1, "配方命中动物脂肪")
    if any(keyword in text for keyword in ["适口", "喷涂", "风味", "诱食", "冻干"]):
        add_formula_tag(rows, sku_id, sku_name, "palatability_coating", "适口/风味增强", "palatability", 1.2, "配方或工艺标签命中适口增强")

    if any(keyword in text for keyword in ["维生素E", "生育酚", "迷迭香", "迷叠香", "茶多酚", "天然抗氧化", "抗氧化体系"]):
        add_formula_tag(rows, sku_id, sku_name, "antioxidant_system", "抗氧化体系", "antioxidant", 1.3, "配方命中抗氧化体系")
        add_formula_tag(rows, sku_id, sku_name, "vitamin_e_used", "维生素E/抗氧化剂使用", "antioxidant", 1.0, "配方命中抗氧化相关成分")
    if any(keyword in text for keyword in ["迷迭香", "迷叠香"]):
        add_formula_tag(rows, sku_id, sku_name, "rosemary_extract_used", "迷迭香提取物支持", "antioxidant", 1.0, "配方命中迷迭香/迷叠香")
    if "茶多酚" in text:
        add_formula_tag(rows, sku_id, sku_name, "tea_polyphenol_used", "植物多酚支持", "antioxidant", 1.0, "配方命中茶多酚")

    return pd.DataFrame(rows)


def profile_from_capability_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    role_order = {"主配方能力": 0, "辅助配方能力": 1, "风险短板": 2, "补充配方能力": 3}
    records = sorted(
        records,
        key=lambda item: (
            role_order.get(str(item.get("display_role") or ""), 9),
            -safe_float(item.get("capability_score"), 0.0),
        ),
    )
    main_caps = [item for item in records if item.get("display_role") == "主配方能力"]
    assist_caps = [item for item in records if item.get("display_role") == "辅助配方能力"]
    risk_caps = [item for item in records if item.get("display_role") == "风险短板"]
    top_records = records[:6]
    return {
        "main_capabilities": main_caps,
        "assist_capabilities": assist_caps,
        "risk_shortcomings": risk_caps,
        "all_capabilities": top_records,
        "main_text": " / ".join(
            f"{item.get('capability_name')}（{item.get('capability_level')}，{item.get('capability_score')}）"
            for item in main_caps[:2]
        ) or "暂无",
        "assist_text": " / ".join(
            f"{item.get('capability_name')}（{item.get('capability_level')}）"
            for item in assist_caps[:4]
        ) or "暂无",
        "description_text": "；".join(
            str(item.get("description") or "")
            for item in top_records[:3]
            if item.get("description")
        ) or "暂无",
        "risk_text": " / ".join(
            f"{item.get('capability_name')}（{item.get('capability_level')}）"
            for item in risk_caps[:2]
        ) or "暂无",
        "evidence_text": formula_evidence_text(top_records),
    }


def fallback_formula_profile_from_product_row(row: Dict[str, Any]) -> Dict[str, Any]:
    base_df = fallback_base_tags_from_product_row(row)
    if base_df.empty:
        return {}
    capability_df = sku_function_structure_rules.generate_sku_formula_capabilities(base_df)
    if capability_df.empty:
        return {}
    return profile_from_capability_records(capability_df.to_dict("records"))


def run_analysis(target_sku_id: str, top_n: int, min_similarity: float, recall_mode: str) -> Dict[str, Any]:
    with get_conn() as conn:
        ensure_output_table(conn)
        result = analyze_by_target_sku(
            conn=conn,
            target_sku_id=target_sku_id,
            top_n=top_n,
            min_similarity=min_similarity,
            recall_mode=recall_mode,
        )
        conn.commit()
    return result


def run_filter_analysis(filters: Dict[str, Any], top_n: int) -> Dict[str, Any]:
    with get_conn() as conn:
        ensure_output_table(conn)
        result = analyze_by_filters(conn=conn, top_n=top_n, **filters)
        conn.commit()
    return result


def build_pair_similarity_row(target_sku: Dict[str, Any], candidate_sku: Dict[str, Any], recall_mode: str) -> Dict[str, Any]:
    similarity_recall = b2b.similarity_recall
    engine = similarity_recall.get_engine()
    feature_df = similarity_recall.load_sku_features(engine)
    sku_col = similarity_recall.COLUMN_CONFIG["sku_id"]
    target_id = str(target_sku.get("sku_id") or "")
    candidate_id = str(candidate_sku.get("sku_id") or "")
    target_matches = feature_df[feature_df[sku_col].astype(str) == target_id]
    candidate_matches = feature_df[feature_df[sku_col].astype(str) == candidate_id]
    if target_matches.empty:
        raise ValueError(f"未找到目标 SKU 的相似度特征：{target_id}")
    if candidate_matches.empty:
        raise ValueError(f"未找到对比 SKU 的相似度特征：{candidate_id}")

    target_feature = target_matches.iloc[0]
    candidate_feature = candidate_matches.iloc[0]
    sims = similarity_recall.calculate_three_similarities(target_feature, candidate_feature, mode=recall_mode)
    ingredient_sim = safe_float(sims.get("ingredient_category_similarity"), 0.0)
    nutrition_sim = safe_float(sims.get("nutrition_structure_similarity"), 0.0)
    process_sim = safe_float(sims.get("process_structure_similarity"), 0.0)
    final_sim = similarity_recall.calculate_final_similarity(
        ingredient_sim,
        nutrition_sim,
        process_sim,
        mode=recall_mode,
    )

    ingredient_level = similarity_recall.similarity_level(ingredient_sim)
    nutrition_level = similarity_recall.similarity_level(nutrition_sim)
    process_level = similarity_recall.similarity_level(process_sim)
    pattern_name, pattern_desc = similarity_recall.build_similarity_pattern(
        ingredient_level,
        nutrition_level,
        process_level,
    )

    ingredient_col = similarity_recall.COLUMN_CONFIG["ingredient_category_vector"]
    nutrition_col = similarity_recall.COLUMN_CONFIG["nutrition_feature_vector"]
    process_col = similarity_recall.COLUMN_CONFIG["process_structure_vector"]
    target_ingredient = similarity_recall.safe_json_loads(target_feature[ingredient_col])
    candidate_ingredient = similarity_recall.safe_json_loads(candidate_feature[ingredient_col])
    target_nutrition = similarity_recall.safe_json_loads(target_feature[nutrition_col])
    candidate_nutrition = similarity_recall.safe_json_loads(candidate_feature[nutrition_col])
    target_process = similarity_recall.safe_json_loads(target_feature[process_col])
    candidate_process = similarity_recall.safe_json_loads(candidate_feature[process_col])

    shared_ingredient_features = similarity_recall.get_top_shared_features(target_ingredient, candidate_ingredient, top_n=8)
    ingredient_role_details = similarity_recall.vector_overlap_details(
        target_ingredient,
        candidate_ingredient,
        key_label="功能角色",
        top_n=8,
    )
    nutrition_details = similarity_recall.nutrition_component_details(
        target_nutrition,
        candidate_nutrition,
        mode=recall_mode,
    )
    high_nutrition_parts = [
        item["label"]
        for item in nutrition_details
        if safe_float(item.get("similarity"), 0.0) >= 0.75
    ]
    shared_process_features = similarity_recall.get_top_shared_features(target_process, candidate_process, top_n=8)
    key_differences = []
    key_differences.extend(similarity_recall.get_key_differences(
        target_ingredient,
        candidate_ingredient,
        top_n=4,
        prefix_target="目标SKU原料结构更高",
        prefix_candidate="候选SKU原料结构更高",
    ))
    key_differences.extend(similarity_recall.get_key_differences(
        target_nutrition,
        candidate_nutrition,
        top_n=4,
        prefix_target="目标SKU营养压力更高",
        prefix_candidate="候选SKU营养压力更高",
    ))
    key_differences.extend(similarity_recall.get_key_differences(
        target_process,
        candidate_process,
        top_n=4,
        prefix_target="目标SKU工艺风险更高",
        prefix_candidate="候选SKU工艺风险更高",
    ))
    observation_points = similarity_recall.build_observation_points(
        mode=recall_mode,
        target_row=target_feature,
        candidate_row=candidate_feature,
        ingredient_level=ingredient_level,
        nutrition_level=nutrition_level,
        process_level=process_level,
    )
    business_interpretation = similarity_recall.build_business_interpretation(
        pattern_name,
        pattern_desc,
        ingredient_level,
        nutrition_level,
        process_level,
        recall_mode,
    )
    difference_summary = {
        "shared_ingredient_category_tags": shared_ingredient_features,
        "ingredient_role_similarity_details": ingredient_role_details,
        "nutrition_component_similarities": nutrition_details,
        "high_nutrition_similarity_parts": high_nutrition_parts,
        "shared_process_structure_tags": shared_process_features,
        "key_differences": key_differences[:12],
        "observation_points": observation_points,
        "similarity_pattern": pattern_name,
        "business_interpretation": business_interpretation,
    }

    return {
        **candidate_sku,
        "overall_similarity": final_sim,
        "process_structure_similarity": process_sim,
        "ingredient_similarity": ingredient_sim,
        "nutrition_similarity": nutrition_sim,
        "risk_reason_similarity": 0.0,
        "confidence_factor": 1.0,
        "similarity_level": similarity_recall.similarity_level(final_sim),
        "shared_process_structure_tags": shared_process_features,
        "shared_ingredient_category_tags": shared_ingredient_features,
        "ingredient_role_similarity_details": ingredient_role_details,
        "nutrition_component_similarities": nutrition_details,
        "high_nutrition_similarity_parts": high_nutrition_parts,
        "recall_groups": ["手动两品对比"],
        "difference_summary": difference_summary,
        "similarity_pattern": pattern_name,
        "business_interpretation": business_interpretation,
        "key_differences": key_differences[:12],
        "observation_points": observation_points,
    }


def run_pair_comparison(target_sku_id: str, candidate_sku_id: str, recall_mode: str) -> Dict[str, Any]:
    if target_sku_id == candidate_sku_id:
        raise ValueError("请选择两款不同的产品进行对比。")
    with get_conn() as conn:
        target_sku = b2b.fetch_target_sku(conn, target_sku_id)
        candidate_sku = b2b.fetch_target_sku(conn, candidate_sku_id)
    if not target_sku:
        raise ValueError(f"未找到目标 SKU：{target_sku_id}")
    if not candidate_sku:
        raise ValueError(f"未找到对比 SKU：{candidate_sku_id}")
    comparison_row = build_pair_similarity_row(target_sku, candidate_sku, recall_mode)
    return {
        "analysis_id": f"PAIR_{target_sku_id}__{candidate_sku_id}",
        "analysis_type": "pair_compare",
        "target_sku_id": target_sku_id,
        "target_sku": target_sku,
        "product_search_result": [comparison_row],
        "similar_product_problem_clusters": [],
        "process_failure_priority": [],
        "quality_validation_plan": [],
        "preserve_product_rows": True,
        "comparison_mode": "pair",
    }


def render_tag_list(tags: List[str]):
    if not tags:
        st.caption("暂无")
        return
    st.write(" / ".join(str(tag) for tag in tags[:12]))


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = parse_json_cell(value)
        if isinstance(parsed, list):
            return parsed
        if parsed:
            return [parsed]
        return []
    return [value]


def as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    parsed = parse_json_cell(value)
    return parsed if isinstance(parsed, dict) else {}


def brand_key(value: Any) -> str:
    return str(value or "").strip().lower()


def brand_from_sku_id(sku_id: Any) -> str:
    text = str(sku_id or "").strip()
    if "||" in text:
        return text.split("||", 1)[0]
    return ""


def filter_cross_brand_products(product_rows: List[Dict[str, Any]], target_sku: Dict[str, Any], target_sku_id: Any = "") -> List[Dict[str, Any]]:
    target_brand = brand_key(target_sku.get("brand") or target_sku.get("brand_name") or brand_from_sku_id(target_sku_id))
    filtered = []
    seen_brands = set()
    for row in product_rows:
        row_brand = brand_key(row.get("brand") or brand_from_sku_id(row.get("sku_id")))
        if target_brand and row_brand == target_brand:
            continue
        if row_brand:
            if row_brand in seen_brands:
                continue
            seen_brands.add(row_brand)
        filtered.append(row)
    return filtered


NUTRITION_LABEL_MAP = {
    "protein_score": "蛋白质",
    "carb_score": "碳水/淀粉",
    "fat_score": "脂肪",
    "fiber_score": "纤维素",
    "prebiotic_score": "益生元",
    "antioxidant_score": "抗氧化",
}

NUTRITION_FIELD_ORDER = [
    "protein_score",
    "carb_score",
    "fat_score",
    "fiber_score",
    "prebiotic_score",
    "antioxidant_score",
]

NUTRITION_RAW_FIELD_MAP = {
    "protein_score": "crude_protein",
    "fat_score": "crude_fat",
    "carb_score": "estimated_carb",
    "fiber_score": "crude_fiber",
}

SIMILARITY_MODULES = [
    {
        "module": "蛋白质模块",
        "score_field": "protein_score",
        "role_keywords": ["动物蛋白", "蛋白来源", "植物蛋白"],
        "process_response": "关注蛋白原料预处理、粉体混合均匀性和熟化一致性。",
        "indicators": ["粗蛋白实测", "蛋白消化率", "混合均匀性", "熟化一致性"],
    },
    {
        "module": "碳水模块",
        "score_field": "carb_score",
        "role_keywords": ["碳水", "淀粉"],
        "process_response": "关注淀粉糊化、膨化成型、水分和颗粒硬度控制。",
        "indicators": ["淀粉糊化度", "膨化度", "水分", "颗粒硬度"],
    },
    {
        "module": "脂肪模块",
        "score_field": "fat_score",
        "role_keywords": ["脂肪"],
        "process_response": "关注油脂添加方式、后喷均匀性、表面残油和氧化控制。",
        "indicators": ["粗脂肪实测", "表面残油", "喷涂均匀性", "酸价", "过氧化值"],
    },
    {
        "module": "纤维素/纤维模块",
        "score_field": "fiber_score",
        "role_keywords": ["纤维"],
        "process_response": "关注纤维结构对吸水、粉化、颗粒完整度和粪便成形的影响。",
        "indicators": ["粗纤维实测", "吸水性", "粉化率", "颗粒完整度", "试喂便便成形"],
    },
    {
        "module": "益生元模块",
        "score_field": "prebiotic_score",
        "role_keywords": ["益生元", "发酵底物", "益生菌"],
        "process_response": "关注功能成分混合均匀性、热敏损失和活性稳定性。",
        "indicators": ["功能成分实测", "混合均匀性", "活性稳定性", "批次波动"],
    },
    {
        "module": "抗氧化模块",
        "score_field": "antioxidant_score",
        "role_keywords": ["抗氧化"],
        "process_response": "关注油脂氧化控制、抗氧化体系覆盖和储存稳定性。",
        "indicators": ["酸价", "过氧化值", "气味稳定性", "留样稳定性"],
    },
    {
        "module": "适口性喷涂模块",
        "score_field": None,
        "role_keywords": ["适口", "喷涂", "风味"],
        "process_response": "关注喷涂均匀性、诱食剂分布、表面油脂和气味一致性。",
        "indicators": ["适口性测试", "喷涂均匀性", "表面残油", "气味稳定性"],
    },
]

MODULE_PATH_PROFILES = {
    "蛋白质模块": [
        {
            "keywords": ["水解", "酶解"],
            "path": "水解蛋白 / 酶解蛋白路径",
            "process_points": "吸湿性、结块风险、风味稳定、适口稳定、后喷/调味依赖、混合均匀性",
            "indicators": "水分活度、结块率、气味稳定性、喷涂均匀度、适口批次稳定性",
            "feedback": "挑食、拒食、呕吐、气味异常、适口波动",
        },
        {
            "keywords": ["鲜肉", "鲜鸡", "鲜鸭", "去骨", "冻肉"],
            "path": "鲜肉/冻肉蛋白路径",
            "process_points": "原料预处理、水分控制、搅拌均匀性、干燥负担、熟化一致性、成型稳定",
            "indicators": "成品水分、水分活度、熟化度、颗粒成型稳定性、颗粒硬度",
            "feedback": "软便、呕吐、消化不良、颗粒不稳定反馈",
        },
        {
            "keywords": ["肉粉", "鱼粉", "禽肉粉"],
            "path": "肉粉 / 鱼粉蛋白路径",
            "process_points": "粉体批次稳定、混合均匀性、熟化一致性、灰分负担、气味控制",
            "indicators": "粗蛋白实测、灰分、熟化度、粉化率、气味稳定性",
            "feedback": "软便、呕吐、适口下降、粪便气味重",
        },
        {
            "keywords": ["植物蛋白", "豌豆蛋白", "大豆", "谷朊粉"],
            "path": "植物蛋白补强路径",
            "process_points": "粉体分散、吸水性、糊化协同、抗营养因子控制、熟化一致性",
            "indicators": "蛋白实测、淀粉糊化度、颗粒硬度、粉化率、消化率",
            "feedback": "软便、胀气、适口下降、消化负担反馈",
        },
    ],
    "碳水模块": [
        {
            "keywords": ["豌豆", "鹰嘴豆", "扁豆", "豆"],
            "path": "豆类碳水 / 豆类淀粉路径",
            "process_points": "浸润吸水、糊化充分性、膨化稳定、颗粒硬度、抗营养因子控制",
            "indicators": "淀粉糊化度、水分、颗粒硬度、粉化率、粪便成形",
            "feedback": "软便、胀气、消化不良、颗粒硬度波动",
        },
        {
            "keywords": ["马铃薯", "土豆", "甘薯", "红薯", "木薯", "淀粉"],
            "path": "薯类 / 纯淀粉成型路径",
            "process_points": "糊化窗口、膨化倍率、水分迁移、干燥曲线、成型稳定",
            "indicators": "糊化度、膨化度、水分活度、颗粒硬度、碎粉率",
            "feedback": "软便、呕吐、颗粒粉化、适口波动",
        },
        {
            "keywords": ["大米", "糙米", "小麦", "玉米", "燕麦", "谷物"],
            "path": "谷物 / 米类碳水路径",
            "process_points": "粉碎粒度、糊化充分性、膨化稳定、水分控制、颗粒硬度",
            "indicators": "淀粉糊化度、水分、膨化度、颗粒硬度、碎粉率",
            "feedback": "软便、消化不良、颗粒硬度异常",
        },
    ],
    "脂肪模块": [
        {
            "keywords": ["鸡油", "鸭油", "牛脂", "动物脂肪", "禽脂"],
            "path": "动物脂肪添加路径",
            "process_points": "油脂熔融、添加温度、混合均匀性、后喷负担、氧化控制",
            "indicators": "粗脂肪实测、表面残油、酸价、过氧化值、喷涂均匀度",
            "feedback": "黑下巴、软便、油哈味、挑食、呕吐",
        },
        {
            "keywords": ["鱼油", "三文鱼油", "亚麻籽油", "植物油", "葵花籽油"],
            "path": "功能油脂 / 不饱和油脂路径",
            "process_points": "低温保护、抗氧化覆盖、后喷均匀性、储存稳定、气味控制",
            "indicators": "过氧化值、酸价、气味稳定性、Omega 脂肪酸、留样稳定性",
            "feedback": "黑下巴、油脂氧化味、拒食、呕吐、皮肤反馈波动",
        },
    ],
    "纤维素/纤维模块": [
        {
            "keywords": ["纤维素", "木质纤维", "豌豆纤维"],
            "path": "不溶性纤维支撑路径",
            "process_points": "粉体分散、吸水膨胀、混合均匀性、颗粒完整度、粉化控制",
            "indicators": "粗纤维实测、吸水性、粉化率、颗粒完整度、粪便成形",
            "feedback": "便秘、软便、粪便成形异常、颗粒粉化",
        },
        {
            "keywords": ["甜菜粕", "菊苣", "车前子", "南瓜", "果胶"],
            "path": "可发酵 / 成形纤维路径",
            "process_points": "吸水性、黏结性、发酵底物稳定、干燥负担、成型稳定",
            "indicators": "粗纤维实测、水分活度、颗粒硬度、粪便成形、试喂反馈",
            "feedback": "软便、胀气、便便形态波动、消化适应期反馈",
        },
    ],
    "益生元模块": [
        {
            "keywords": ["低聚果糖", "FOS", "低聚木糖", "MOS", "甘露寡糖", "菊粉"],
            "path": "益生元 / 发酵底物路径",
            "process_points": "功能成分计量、混合均匀性、热稳定、吸湿控制、批次一致性",
            "indicators": "添加量复核、混合均匀性、水分活度、活性稳定性、试喂便便成形",
            "feedback": "软便、胀气、消化适应期、粪便气味波动",
        },
        {
            "keywords": ["益生菌", "乳酸菌", "芽孢杆菌", "双歧杆菌"],
            "path": "益生菌活性路径",
            "process_points": "后添加温度、活菌保护、混合均匀性、储存稳定、湿热暴露控制",
            "indicators": "活菌数、混合均匀性、水分活度、留样活性、批次稳定性",
            "feedback": "软便、消化适应期、效果波动、批次反馈不稳定",
        },
    ],
    "抗氧化模块": [
        {
            "keywords": ["维生素E", "生育酚", "迷迭香", "迷叠香", "茶多酚", "绿茶", "姜黄", "丝兰"],
            "path": "抗氧化体系覆盖路径",
            "process_points": "抗氧化剂覆盖、油脂接触面保护、后喷保护、储存氧化控制",
            "indicators": "过氧化值、酸价、维生素E、气味稳定性、留样稳定性",
            "feedback": "黑下巴、油哈味、拒食、呕吐、皮肤反馈波动",
        },
        {
            "keywords": ["蔓越莓", "蓝莓", "越橘", "石榴", "苹果", "果蔬", "柑橘提取物"],
            "path": "天然果蔬抗氧化辅助路径",
            "process_points": "粉体分散、热敏保护、颜色气味稳定、批次来源稳定",
            "indicators": "多酚稳定性、颜色稳定、气味稳定、混合均匀性、留样观察",
            "feedback": "适口波动、气味异常、皮肤反馈波动",
        },
    ],
    "适口性喷涂模块": [
        {
            "keywords": ["喷涂", "后喷", "诱食", "适口", "风味"],
            "path": "后喷 / 调味适口路径",
            "process_points": "喷涂温度、喷涂均匀性、诱食剂分散、表面油脂、气味稳定",
            "indicators": "喷涂均匀度、表面残油、气味稳定性、适口测试、批次采食率",
            "feedback": "挑食、拒食、气味异常、适口批次波动、呕吐",
        },
    ],
}

DEFAULT_MODULE_PROFILE = {
    "path": "常规模块路径",
    "process_points": "原料计量、混合均匀性、水分控制、热加工稳定、批次一致性",
    "indicators": "关键营养实测、水分活度、混合均匀性、颗粒稳定性、留样观察",
    "feedback": "软便、呕吐、挑食、批次反馈波动",
}

MODULE_MAPPING_KEY = {
    "蛋白质模块": "蛋白质",
    "碳水模块": "碳水",
    "脂肪模块": "脂肪",
    "纤维素/纤维模块": "纤维",
    "益生元模块": "益生元",
    "抗氧化模块": "抗氧化",
    "适口性喷涂模块": "适口喷涂",
}

PROTEIN_MIXED_PROFILE = {
    "path": "鲜肉/冻肉 + 肉粉复合蛋白路径",
    "process_points": "高含水肉源预处理、粉体分散、混合均匀性、水分控制、熟化一致性、成型稳定",
    "indicators": "成品水分、水分活度、粗蛋白实测、熟化度、颗粒硬度、粉化率",
    "feedback": "软便、呕吐、消化不良、适口波动、颗粒稳定性反馈",
}

PROTEIN_FRESH_PROFILE = {
    "path": "鲜肉/冻肉蛋白路径",
    "process_points": "原料预处理、水分控制、搅拌均匀性、干燥负担、熟化一致性、成型稳定",
    "indicators": "成品水分、水分活度、熟化度、颗粒成型稳定性、颗粒硬度",
    "feedback": "软便、呕吐、消化不良、颗粒不稳定反馈",
}

PROTEIN_POWDER_PROFILE = {
    "path": "肉粉 / 鱼粉蛋白路径",
    "process_points": "粉体批次稳定、混合均匀性、熟化一致性、灰分负担、气味控制",
    "indicators": "粗蛋白实测、灰分、熟化度、粉化率、气味稳定性",
    "feedback": "软便、呕吐、适口下降、粪便气味重",
}

PROTEIN_HYDROLYZED_PROFILE = {
    "path": "水解蛋白 / 酶解蛋白路径",
    "process_points": "吸湿性、结块风险、风味稳定、适口稳定、后喷/调味依赖、混合均匀性",
    "indicators": "水分活度、结块率、气味稳定性、喷涂均匀度、适口批次稳定性",
    "feedback": "挑食、拒食、呕吐、气味异常、适口波动",
}

NUTRITION_DIFF_FOCUS = {
    ("蛋白质模块", "目标"): {
        "process_points": "目标侧重点看高蛋白负载下的熟化充分性、蛋白粉/鲜肉混合均匀性、干燥负担、颗粒成型稳定",
        "candidate_process_points": "候选侧作为较低蛋白压力对照，重点确认常规熟化和混合稳定",
        "indicators": "目标侧优先看粗蛋白实测、蛋白消化率、熟化度、成品水分/水分活度、颗粒硬度、粉化率",
        "candidate_indicators": "候选侧记录粗蛋白实测、熟化度、水分活度，作为压力较低基线",
        "feedback": "目标侧更关注软便、呕吐、消化负担、粪便气味加重、颗粒稳定性反馈",
        "candidate_feedback": "候选侧作为低压力对照，观察上述反馈是否明显减弱",
    },
    ("蛋白质模块", "候选"): {
        "process_points": "候选侧重点看高蛋白负载下的熟化充分性、蛋白粉/鲜肉混合均匀性、干燥负担、颗粒成型稳定",
        "candidate_process_points": "目标侧作为较低蛋白压力对照，重点确认常规熟化和混合稳定",
        "indicators": "候选侧优先看粗蛋白实测、蛋白消化率、熟化度、成品水分/水分活度、颗粒硬度、粉化率",
        "candidate_indicators": "目标侧记录粗蛋白实测、熟化度、水分活度，作为压力较低基线",
        "feedback": "候选侧更关注软便、呕吐、消化负担、粪便气味加重、颗粒稳定性反馈",
        "candidate_feedback": "目标侧作为低压力对照，观察上述反馈是否明显减弱",
    },
}


def nutrition_label(value: Any) -> str:
    text = str(value or "").strip()
    if text == "纤维":
        return "纤维素"
    return NUTRITION_LABEL_MAP.get(text, text)


def nutrition_label_order(value: Any) -> int:
    text = str(value or "").strip()
    for idx, field in enumerate(NUTRITION_FIELD_ORDER):
        label = NUTRITION_LABEL_MAP.get(field, field)
        if field == text or label in text:
            return idx
        if field == "fiber_score" and "纤维" in text:
            return idx
    return len(NUTRITION_FIELD_ORDER)


def sort_nutrition_labels(labels: List[str]) -> List[str]:
    unique_labels = []
    for label in labels:
        text = str(label or "").strip()
        if text and text not in unique_labels:
            unique_labels.append(text)
    return sorted(unique_labels, key=lambda item: (nutrition_label_order(item), unique_labels.index(item)))


def format_nutrition_diff_labels(labels: List[str]) -> str:
    sorted_labels = sort_nutrition_labels(labels)
    target_labels = [label for label in sorted_labels if label.startswith("目标")]
    candidate_labels = [label for label in sorted_labels if label.startswith("候选")]
    other_labels = [
        label for label in sorted_labels
        if not label.startswith("目标") and not label.startswith("候选")
    ]
    lines = []
    if target_labels:
        lines.append(" / ".join(target_labels))
    if candidate_labels:
        lines.append(" / ".join(candidate_labels))
    if other_labels:
        lines.append(" / ".join(other_labels))
    return "\n".join(lines) if lines else "暂无"


def nutrition_pressure_level(value: Any) -> str:
    score = safe_float(value, 0.0)
    if score >= 0.67:
        return "高"
    if score >= 0.34:
        return "中"
    return "低"


def nutrition_score_pressure_level(field: str, value: Any, thresholds: Optional[Dict[str, Dict[str, float]]] = None) -> str:
    number = safe_float(value, None)
    if number is None or pd.isna(number):
        return "未分级"
    if thresholds and field in thresholds:
        high = thresholds[field].get("high")
        medium = thresholds[field].get("medium")
        if high is not None and number >= high:
            return "高"
        if medium is not None and number >= medium:
            return "中"
        return "低"
    if field == "protein_score":
        if number >= 40:
            return "高"
        if number >= 32:
            return "中"
        return "低"
    if field == "fat_score":
        if number >= 18:
            return "高"
        if number >= 13:
            return "中"
        return "低"
    if field == "fiber_score":
        if number >= 5:
            return "高"
        if number >= 3:
            return "中"
        return "低"
    if field == "carb_score":
        if number >= 0.67:
            return "高"
        if number >= 0.34:
            return "中"
        return "低"
    return "未分级"


def legacy_nutrition_same_label(
    field: Any,
    row: Dict[str, Any],
    target_sku: Optional[Dict[str, Any]],
    score_reference: Optional[Dict[str, Any]] = None,
) -> str:
    field_name = str(field or "").strip()
    label = nutrition_label(field_name)
    score_reference = score_reference or {}
    score_map = score_reference.get("scores") or {}
    thresholds = score_reference.get("thresholds") or {}
    target_scores = score_map.get(str((target_sku or {}).get("sku_id") or ""))
    candidate_scores = score_map.get(str(row.get("sku_id") or ""))

    if target_scores and candidate_scores and field_name in target_scores and field_name in candidate_scores:
        target_level = nutrition_score_pressure_level(field_name, target_scores.get(field_name), thresholds)
        candidate_level = nutrition_score_pressure_level(field_name, candidate_scores.get(field_name), thresholds)
    else:
        raw_field = NUTRITION_RAW_FIELD_MAP.get(field_name)
        if not raw_field or not target_sku:
            return f"{label}压力接近（未分级）"
        target_level = nutrition_score_pressure_level(field_name, target_sku.get(raw_field))
        candidate_level = nutrition_score_pressure_level(field_name, row.get(raw_field))

    if target_level == "未分级" or candidate_level == "未分级":
        return f"{label}压力接近（未分级）"
    if target_level == candidate_level:
        return f"{target_level}{label}压力接近"
    return f"{target_level}/{candidate_level}{label}压力接近"


def nutrition_same_label(item: Dict[str, Any]) -> str:
    label = nutrition_label(item.get("label") or item.get("field"))
    target_value = safe_float(item.get("target_strength"), 0.0)
    candidate_value = safe_float(item.get("candidate_strength"), 0.0)
    avg_value = (target_value + candidate_value) / 2
    return f"{nutrition_pressure_level(avg_value)}{label}压力接近"


def nutrition_diff_label(item: Dict[str, Any]) -> str:
    label = nutrition_label(item.get("label") or item.get("field"))
    target_value = safe_float(item.get("target_strength"), 0.0)
    candidate_value = safe_float(item.get("candidate_strength"), 0.0)
    if target_value >= candidate_value:
        return f"目标{label}压力更高"
    return f"候选{label}压力更高"


def nutrition_same_diff_labels(
    row: Dict[str, Any],
    target_sku: Optional[Dict[str, Any]] = None,
    score_reference: Optional[Dict[str, Any]] = None,
) -> tuple[List[str], List[str]]:
    difference = as_dict(row.get("difference_summary"))
    details = [
        as_dict(item)
        for item in as_list(row.get("nutrition_component_similarities") or difference.get("nutrition_component_similarities"))
    ]
    if not details:
        same_labels = [
            legacy_nutrition_same_label(item, row, target_sku, score_reference)
            for item in as_list(row.get("shared_nutrition_features") or difference.get("shared_nutrition_features"))
        ]
        diff_labels = []
        for item in as_list(row.get("key_differences") or difference.get("key_differences")):
            text = str(item)
            if "营养" not in text:
                continue
            matched = [label for key, label in NUTRITION_LABEL_MAP.items() if key in text]
            if matched:
                prefix = "目标" if "目标" in text else "候选" if "候选" in text else ""
                diff_labels.extend([f"{prefix}{label}压力更高" if prefix else f"{label}压力差异" for label in matched])
            else:
                diff_labels.append(text)
        return same_labels[:4], sort_nutrition_labels(diff_labels)[:4]

    same_items = [
        item for item in details
        if safe_float(item.get("similarity"), 0.0) >= 0.75
    ]
    same_items.sort(key=lambda item: safe_float(item.get("similarity"), 0.0), reverse=True)
    diff_items = sorted(
        details,
        key=lambda item: nutrition_label_order(item.get("label") or item.get("field")),
    )
    same_labels = [nutrition_same_label(item) for item in same_items if item.get("label") or item.get("field")]
    same_base_labels = [
        nutrition_label(item.get("label") or item.get("field"))
        for item in same_items[:2]
        if item.get("label") or item.get("field")
    ]
    diff_labels = [
        nutrition_diff_label(item)
        for item in diff_items
        if (item.get("label") or item.get("field"))
        and nutrition_label(item.get("label") or item.get("field")) not in same_base_labels
    ]
    return same_labels[:4], sort_nutrition_labels(diff_labels)[:4]


def ingredient_same_diff_labels(
    row: Dict[str, Any],
    target_sku: Optional[Dict[str, Any]] = None,
    ingredient_reference: Optional[Dict[str, Dict[str, float]]] = None,
) -> tuple[List[str], List[str]]:
    difference = as_dict(row.get("difference_summary"))
    ingredient_reference = ingredient_reference or {}
    target_vector = ingredient_reference.get(str((target_sku or {}).get("sku_id") or ""))
    candidate_vector = ingredient_reference.get(str(row.get("sku_id") or ""))
    if target_vector and candidate_vector:
        overlap = b2b.similarity_recall.vector_overlap_details(
            target_vector,
            candidate_vector,
            key_label="功能角色",
            top_n=8,
        )
        same_labels = [
            str(item.get("功能角色") or "")
            for item in overlap
            if item.get("功能角色")
        ]
        diff_labels = []
        for item in b2b.similarity_recall.get_key_differences(
            target_vector,
            candidate_vector,
            top_n=4,
            prefix_target="目标SKU配方角色更高",
            prefix_candidate="候选SKU配方角色更高",
        ):
            diff_labels.append(str(item))
        return same_labels[:4], diff_labels[:4]

    details = [
        as_dict(item)
        for item in as_list(row.get("ingredient_role_similarity_details") or difference.get("ingredient_role_similarity_details"))
    ]
    details.sort(key=lambda item: safe_float(item.get("shared_strength"), 0.0), reverse=True)
    same_labels = [
        str(item.get("功能角色") or item.get("role") or "")
        for item in details
        if item.get("功能角色") or item.get("role")
    ]
    if not same_labels:
        same_labels = [
            str(item)
            for item in as_list(
                row.get("shared_ingredient_category_tags")
                or difference.get("shared_ingredient_category_tags")
                or difference.get("shared_ingredient_features")
            )
        ]
    diff_labels = [
        str(item)
        for item in as_list(row.get("key_differences") or difference.get("key_differences"))
        if "原料" in str(item)
    ]
    if not diff_labels:
        diff_labels = [
            str(item)
            for item in as_list(row.get("observation_points") or difference.get("observation_points"))
            if "原料" in str(item) or "配方" in str(item)
        ]
    return same_labels[:4], diff_labels[:4]


def module_score_label(field: Optional[str], sku_id: Any, score_reference: Optional[Dict[str, Any]]) -> tuple[str, Any]:
    if not field:
        return "看形态/来源", ""
    score_reference = score_reference or {}
    scores = (score_reference.get("scores") or {}).get(str(sku_id or ""), {})
    thresholds = score_reference.get("thresholds") or {}
    value = scores.get(field)
    return nutrition_score_pressure_level(field, value, thresholds), value


def module_role_labels(module: Dict[str, Any], sku_id: Any, ingredient_reference: Optional[Dict[str, Dict[str, float]]]) -> List[str]:
    vector = (ingredient_reference or {}).get(str(sku_id or ""), {})
    keywords = module.get("role_keywords") or []
    labels = []
    for label in vector.keys():
        if any(keyword in str(label) for keyword in keywords):
            labels.append(str(label))
    return labels


def split_form_source(labels: List[str]) -> tuple[List[str], List[str]]:
    form_keys = ["类型", "结构", "形态", "粉", "鲜", "冻", "水解", "喷涂", "后喷"]
    form_labels = []
    source_labels = []
    for label in labels:
        if any(key in label for key in form_keys):
            form_labels.append(label)
        else:
            source_labels.append(label)
    return form_labels, source_labels


def compare_label_sets(target_labels: List[str], candidate_labels: List[str]) -> str:
    common = [label for label in target_labels if label in candidate_labels]
    target_only = [label for label in target_labels if label not in candidate_labels]
    candidate_only = [label for label in candidate_labels if label not in target_labels]
    parts = []
    if common:
        parts.append("共同：" + unique_join(common[:3]))
    if target_only:
        parts.append("目标特有：" + unique_join(target_only[:2]))
    if candidate_only:
        parts.append("候选特有：" + unique_join(candidate_only[:2]))
    return "；".join(parts) if parts else "暂无标签"


def label_set_diff(target_labels: List[str], candidate_labels: List[str]) -> tuple[List[str], List[str], List[str]]:
    common = [label for label in target_labels if label in candidate_labels]
    target_only = [label for label in target_labels if label not in candidate_labels]
    candidate_only = [label for label in candidate_labels if label not in target_labels]
    return common, target_only, candidate_only


def module_match_text(
    module: Dict[str, Any],
    sku_row: Dict[str, Any],
    sku_id: Any,
    ingredient_reference: Optional[Dict[str, Dict[str, float]]],
    material_reference: Optional[Dict[str, Dict[str, List[str]]]] = None,
) -> str:
    labels = module_role_labels(module, sku_id, ingredient_reference)
    bucket = (material_reference or {}).get(str(sku_id or ""), {})
    labels.extend(bucket.get("labels", []) if isinstance(bucket, dict) else [])
    if str(module.get("module") or "") == "益生元模块" and isinstance(bucket, dict):
        labels.extend(bucket.get("prebiotic_labels", []))
    parts = labels + [
        str(sku_row.get("ingredient_text") or ""),
        str(sku_row.get("process_structure_summary") or ""),
        unique_join(as_list(sku_row.get("main_process_tags"))),
    ]
    return " ".join(part for part in parts if part)


ANIMAL_SPECIES_KEYWORDS_FOR_PATH = [
    "鸡", "鸭", "火鸡", "鱼", "三文鱼", "鳕鱼", "鲱鱼", "鲭鱼", "沙丁鱼",
    "牛", "羊", "鹿", "兔", "猪", "鸽", "鹌鹑",
]


def animal_species_count_for_path(
    sku_id: Any,
    material_reference: Optional[Dict[str, Dict[str, List[str]]]],
) -> int:
    bucket = (material_reference or {}).get(str(sku_id or ""), {})
    if not isinstance(bucket, dict):
        return 0
    labels = list(bucket.get("primary_labels", [])) + list(bucket.get("secondary_labels", []))
    if not labels:
        labels = list(bucket.get("labels", []))

    species: List[str] = []
    for label in labels:
        text = str(label or "").strip()
        if not text:
            continue
        if any(keyword in text for keyword in ANIMAL_PROTEIN_TYPE_KEYWORDS):
            continue
        for keyword in ANIMAL_SPECIES_KEYWORDS_FOR_PATH:
            if keyword in text and keyword not in species:
                species.append(keyword)
    return len(species)


def should_skip_path_mapping_rule(
    module_name: str,
    rule: Dict[str, Any],
    sku_id: Any,
    material_reference: Optional[Dict[str, Dict[str, List[str]]]],
) -> bool:
    path = str(rule.get("path") or "")
    if module_name == "蛋白质模块" and "多动物蛋白复杂特征" in path:
        return animal_species_count_for_path(sku_id, material_reference) < 2
    if module_name == "碳水模块" and "豆类碳水" in path:
        bucket = (material_reference or {}).get(str(sku_id or ""), {})
        labels = bucket.get("labels", []) if isinstance(bucket, dict) else []
        text = " ".join(str(label or "") for label in labels)
        legume_carb_markers = [
            "豆类碳水来源", "豆类淀粉", "豌豆", "鹰嘴豆", "扁豆", "兵豆",
            "绿豆", "菜豆", "红兵豆", "绿兵豆", "黄豌豆", "绿豌豆",
        ]
        return not any(marker in text for marker in legume_carb_markers)
    return False


def module_path_profile(
    module: Dict[str, Any],
    sku_row: Dict[str, Any],
    sku_id: Any,
    ingredient_reference: Optional[Dict[str, Dict[str, float]]],
    material_reference: Optional[Dict[str, Dict[str, List[str]]]] = None,
    path_mapping_rules: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, str]:
    module_name = str(module.get("module") or "")
    mapping_key = MODULE_MAPPING_KEY.get(module_name, concise_module_name(module_name))
    text = module_match_text(module, sku_row, sku_id, ingredient_reference, material_reference)
    profiles = []
    rules = list((path_mapping_rules or {}).get(mapping_key, []))
    if module_name == "抗氧化模块":
        rules.extend(MODULE_PATH_PROFILES.get(module_name, []))
    for rule in rules:
        if should_skip_path_mapping_rule(module_name, rule, sku_id, material_reference):
            continue
        hit_count = sum(1 for keyword in rule.get("keywords", []) if keyword and keyword in text)
        if hit_count > 0:
            profiles.append((hit_count, rule))
    if profiles:
        profiles.sort(key=lambda item: item[0], reverse=True)
        matched_rules = [rule for _, rule in profiles[:3]]
        return {
            "path": unique_join([rule["path"] for rule in matched_rules]),
            "process_points": unique_join([rule["process_points"] for rule in matched_rules]),
            "indicators": unique_join([rule["indicators"] for rule in matched_rules]),
            "feedback": unique_join([rule["feedback"] for rule in matched_rules]),
        }

    for profile in MODULE_PATH_PROFILES.get(module_name, []):
        if any(keyword and keyword in text for keyword in profile.get("keywords", [])):
            return profile
    fallback = dict(DEFAULT_MODULE_PROFILE)
    fallback["path"] = f"{concise_module_name(module_name)}常规路径"
    return fallback


def material_bucket(
    material_reference: Optional[Dict[str, Dict[str, List[str]]]],
    sku_id: Any,
) -> Dict[str, List[str]]:
    return (material_reference or {}).get(
        str(sku_id or ""),
        {"labels": [], "primary_labels": [], "secondary_labels": []},
    )


def path_profile_from_labels(
    module_name: str,
    module_key: str,
    labels: List[str],
    path_mapping_rules: Optional[Dict[str, List[Dict[str, Any]]]],
) -> Dict[str, str]:
    text = " ".join(str(label) for label in labels if str(label).strip())
    profiles = []
    for rule in (path_mapping_rules or {}).get(module_key, []):
        hit_count = sum(1 for keyword in rule.get("keywords", []) if keyword and keyword in text)
        if hit_count > 0:
            profiles.append((hit_count, rule))
    if profiles:
        profiles.sort(key=lambda item: item[0], reverse=True)
        matched_rules = [rule for _, rule in profiles[:2]]
        return {
            "path": unique_join([rule["path"] for rule in matched_rules]),
            "process_points": unique_join([rule["process_points"] for rule in matched_rules]),
            "indicators": unique_join([rule["indicators"] for rule in matched_rules]),
            "feedback": unique_join([rule["feedback"] for rule in matched_rules]),
        }
    fallback = dict(DEFAULT_MODULE_PROFILE)
    fallback["path"] = f"{concise_module_name(module_name)}常规路径"
    return fallback


ANIMAL_PROTEIN_TYPE_KEYWORDS = (
    "鲜肉", "冻肉", "去骨", "肉粉", "鱼粉", "内脏", "水解", "酶解", "蛋白粉", "浓缩蛋白"
)


def split_animal_source_labels(labels: List[str]) -> Tuple[List[str], List[str]]:
    species_labels: List[str] = []
    type_labels: List[str] = []
    for label in labels:
        text = str(label or "").strip()
        if not text:
            continue
        if any(keyword in text for keyword in ANIMAL_PROTEIN_TYPE_KEYWORDS):
            add_label(type_labels, text)
        else:
            add_label(species_labels, text)
    return species_labels, type_labels


def animal_protein_type_path(type_labels: List[str]) -> str:
    text = " ".join(type_labels)
    paths: List[str] = []
    if any(keyword in text for keyword in ("鲜肉", "冻肉", "去骨")):
        paths.append("鲜肉/冻肉路径")
    if any(keyword in text for keyword in ("肉粉", "鱼粉", "蛋白粉", "浓缩蛋白")):
        paths.append("肉粉浓缩蛋白路径")
    if any(keyword in text for keyword in ("水解", "酶解")):
        paths.append("水解/适口蛋白路径")
    if "内脏" in text:
        paths.append("动物内脏路径")
    return unique_join(paths)


def protein_source_path_text(
    sku_id: Any,
    side: str,
    material_reference: Optional[Dict[str, Dict[str, List[str]]]],
    path_mapping_rules: Optional[Dict[str, List[Dict[str, Any]]]],
) -> str:
    bucket = material_bucket(material_reference, sku_id)
    key = "primary_labels" if side == "primary" else "secondary_labels"
    labels = bucket.get(key, [])
    if not labels:
        return "暂无明确主动物蛋白路径" if side == "primary" else "暂无明确次动物蛋白路径"
    species_labels, type_labels = split_animal_source_labels(labels)
    profile = path_profile_from_labels("蛋白质模块", "蛋白质", type_labels or labels, path_mapping_rules)
    path_text = animal_protein_type_path(type_labels) or profile["path"]
    type_text = unique_join(type_labels[:4]) or "类型未细分"
    return f"{path_text}（{type_text}）"


def plant_protein_path_text(
    sku_id: Any,
    material_reference: Optional[Dict[str, Dict[str, List[str]]]],
) -> str:
    bucket = material_bucket(material_reference, sku_id)
    labels = bucket.get("labels", [])
    plant_labels: List[str] = []
    for label in labels:
        text = str(label or "").strip()
        if not text:
            continue
        if any(keyword in text for keyword in ("植物蛋白", "豌豆蛋白", "黄豆蛋白", "大豆蛋白", "豆蛋白", "小麦蛋白", "玉米蛋白")):
            add_label(plant_labels, text)
    if not plant_labels:
        return "暂无明确植物蛋白补强路径"
    return f"植物蛋白补强路径（{unique_join(plant_labels[:4])}）"


def protein_path_profile(sku_row: Dict[str, Any]) -> Dict[str, str]:
    ingredients = split_ingredient_text(sku_row.get("ingredient_text"))
    protein_items = [item for item in ingredients if is_protein_ingredient(item)]
    leading_items = protein_items[:3]
    has_hydrolyzed = any("水解" in item or "酶解" in item for item in protein_items)
    hydrolyzed_leading = any("水解" in item or "酶解" in item for item in leading_items)
    has_fresh_or_frozen = any(is_fresh_or_frozen_protein(item) for item in protein_items)
    has_powder = any(is_protein_powder(item) for item in protein_items)

    if has_hydrolyzed and (hydrolyzed_leading or not has_fresh_or_frozen and not has_powder):
        return PROTEIN_HYDROLYZED_PROFILE
    if has_fresh_or_frozen and has_powder:
        return PROTEIN_MIXED_PROFILE
    if has_fresh_or_frozen:
        return PROTEIN_FRESH_PROFILE
    if has_powder:
        return PROTEIN_POWDER_PROFILE
    if has_hydrolyzed:
        return PROTEIN_HYDROLYZED_PROFILE
    return PROTEIN_FRESH_PROFILE


def is_protein_ingredient(item: str) -> bool:
    text = str(item or "")
    protein_keywords = [
        "肉", "鱼", "鹿", "鸭", "鸡", "火鸡", "牛", "羊", "兔", "鳟", "三文鱼", "鳕",
        "肝", "心", "蛋", "水解", "酶解", "蛋白", "肉粉", "鱼粉",
    ]
    exclude_keywords = ["鸡油", "鸭油", "牛油", "鱼油", "油脂"]
    return any(keyword in text for keyword in protein_keywords) and not any(keyword in text for keyword in exclude_keywords)


def is_front_animal_protein_ingredient(item: str) -> bool:
    text = str(item or "")
    include_keywords = [
        "肉", "鱼", "鹿", "鸭", "鸡", "火鸡", "牛", "羊", "兔", "鳟", "三文鱼", "鳕",
        "肉粉", "鱼粉", "禽肉粉",
    ]
    exclude_keywords = [
        "鸡油", "鸭油", "牛油", "鱼油", "油脂", "脂肪", "蛋制品", "蛋粉", "蛋氨酸",
        "牛磺酸", "左旋肉碱", "氯化胆碱", "蛋白锌", "蛋白铁", "蛋白铜", "蛋白锰",
        "豌豆蛋白", "大豆蛋白", "黄豆蛋白", "小麦蛋白", "玉米蛋白", "植物蛋白",
    ]
    return any(keyword in text for keyword in include_keywords) and not any(keyword in text for keyword in exclude_keywords)


def front_animal_protein_items(items: List[str]) -> List[str]:
    matched: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        if is_front_animal_protein_ingredient(text):
            add_label(matched, text)
            continue
        if matched:
            break
    return matched


def is_fresh_or_frozen_protein(item: str) -> bool:
    text = str(item or "")
    return (
        any(keyword in text for keyword in ["鲜", "冻", "去骨"])
        and any(keyword in text for keyword in ["肉", "鱼", "鹿", "鸭", "鸡", "牛", "羊", "兔", "鳟", "三文鱼", "鳕"])
        and not is_protein_powder(text)
    )


def is_protein_powder(item: str) -> bool:
    text = str(item or "")
    return any(keyword in text for keyword in ["肉粉", "鱼粉", "禽肉粉", "鸡肉粉", "鸭肉粉", "火鸡肉粉", "三文鱼粉", "羊肉粉", "牛肉粉"])


def split_ingredient_text(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    normalized = (
        text.replace("，", "、")
        .replace(",", "、")
        .replace("；", "、")
        .replace(";", "、")
        .replace("\n", "、")
    )
    items = []
    for item in normalized.split("、"):
        cleaned = item.strip()
        if cleaned and cleaned not in items:
            items.append(cleaned)
    return items


ABSTRACT_DETAIL_KEYWORDS = [
    "碳水类型", "淀粉来源", "碳水来源", "薯类淀粉来源", "高淀粉粉类",
    "益生元 / 发酵底物", "发酵底物", "益生元：", "功能角色",
]
GENERIC_DETAIL_LABELS = {
    "益生元", "发酵底物", "碳水", "薯类碳水", "无谷", "薯类淀粉来源", "高淀粉粉类",
}


def clean_role_detail_label(label: Any, module_name: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    if "：" in text:
        prefix, value = text.split("：", 1)
        if module_name == "碳水模块" and ("类型" in prefix or value.endswith("碳水") or value == "无谷"):
            return ""
        text = value.strip()
    if "-" in text:
        text = text.split("-")[-1].strip()
    if any(keyword == text for keyword in ["薯类淀粉来源", "高淀粉粉类", "薯类碳水", "豆类碳水来源"]):
        return ""
    return text


def is_abstract_detail_label(label: Any) -> bool:
    text = str(label or "").strip()
    return text in GENERIC_DETAIL_LABELS or any(keyword in text for keyword in ABSTRACT_DETAIL_KEYWORDS)


def module_detail_allowed(item: Any, module_name: str) -> bool:
    text = str(item or "").strip()
    if not text or is_abstract_detail_label(text):
        return False
    if module_name == "蛋白质模块":
        return is_protein_ingredient(text) and "纤维" not in text
    if module_name == "碳水模块":
        return not any(keyword in text for keyword in ["蛋白", "纤维", "油", "脂肪", "菊粉", "益生"])
    if module_name == "纤维素/纤维模块":
        return not any(keyword in text for keyword in ["蛋白", "油", "脂肪"])
    if module_name == "益生元模块":
        return not any(keyword in text for keyword in ["蛋白", "油", "脂肪"])
    if module_name == "脂肪模块":
        return not any(keyword in text for keyword in ["蛋白", "纤维"])
    return True


def normalized_detail_items(value: Any, module_name: str) -> List[str]:
    items = []
    for item in split_ingredient_text(value):
        cleaned = clean_role_detail_label(item, module_name)
        if cleaned and module_detail_allowed(cleaned, module_name) and cleaned not in items:
            items.append(cleaned)
    return items


def add_matched_detail(
    matched: List[str],
    value: Any,
    module_name: str,
    keywords: List[str],
):
    for item in normalized_detail_items(value, module_name):
        if not any(keyword and keyword in item for keyword in keywords):
            continue
        if item not in matched:
            matched.append(item)


def module_ingredient_details(
    module: Dict[str, Any],
    sku_row: Dict[str, Any],
    sku_id: Any,
    ingredient_reference: Optional[Dict[str, Dict[str, float]]],
    material_reference: Optional[Dict[str, Dict[str, List[str]]]] = None,
    path_mapping_rules: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> str:
    module_name = str(module.get("module") or "")
    material_bucket_for_sku = (material_reference or {}).get(str(sku_id or ""), {})
    if isinstance(material_bucket_for_sku, dict):
        if module_name == "蛋白质模块":
            front_protein_items = front_animal_protein_items(material_bucket_for_sku.get("raw_ingredient_items", []))
            if front_protein_items:
                return unique_join(front_protein_items[:8])
            protein_details = [
                item for item in material_bucket_for_sku.get("protein_details", [])
                if not any(keyword in str(item) for keyword in ["植物蛋白", "其他动物蛋白", "蛋制品"])
            ]
            if protein_details:
                return unique_join(protein_details[:8])
        if module_name == "碳水模块":
            carb_details = material_bucket_for_sku.get("carb_details", [])
            if carb_details:
                return unique_join(carb_details[:8])
        if module_name == "脂肪模块":
            fat_details = material_bucket_for_sku.get("fat_details", [])
            if fat_details:
                return unique_join(fat_details[:8])
        if module_name == "纤维素/纤维模块":
            fiber_details = material_bucket_for_sku.get("fiber_details", [])
            if fiber_details:
                return unique_join(fiber_details[:8])
        if module_name == "益生元模块":
            prebiotic_details = material_bucket_for_sku.get("prebiotic_labels", [])
            if prebiotic_details:
                return unique_join(prebiotic_details[:8])
        if module_name == "抗氧化模块":
            antioxidant_details = material_bucket_for_sku.get("antioxidant_details", [])
            if antioxidant_details:
                return unique_join(antioxidant_details[:8])

    if module_name == "蛋白质模块":
        matched = []
        protein_keywords = ["蛋白", "肉", "鱼", "鸡", "鸭", "火鸡", "牛", "羊", "鹿", "兔", "肝", "心", "蛋", "水解", "酶解", "肉粉", "鱼粉"]
        add_matched_detail(matched, sku_row.get("ingredient_text"), module_name, protein_keywords)
        material_labels = material_bucket_for_sku.get("labels", []) if isinstance(material_bucket_for_sku, dict) else material_bucket_for_sku
        for label in material_labels:
            add_matched_detail(matched, label, module_name, protein_keywords)
        return unique_join(matched[:8])

    mapping_key = MODULE_MAPPING_KEY.get(module_name, concise_module_name(module_name))
    mapping_keywords = []
    for rule in (path_mapping_rules or {}).get(mapping_key, []):
        mapping_keywords.extend(rule.get("keywords", []))
    keywords = list(module.get("role_keywords") or [])
    keywords.extend(mapping_keywords)
    for profile in MODULE_PATH_PROFILES.get(module_name, []):
        keywords.extend(profile.get("keywords", []))
    if module_name == "抗氧化模块":
        keywords.extend(ANTIOXIDANT_EXTRA_KEYWORDS)

    matched = []
    if module_name == "益生元模块":
        material_bucket_for_sku = (material_reference or {}).get(str(sku_id or ""), {})
        prebiotic_labels = material_bucket_for_sku.get("prebiotic_labels", []) if isinstance(material_bucket_for_sku, dict) else []
        for label in prebiotic_labels:
            for item in normalized_detail_items(label, module_name):
                if item not in matched:
                    matched.append(item)
        if matched:
            return unique_join(matched[:8])

    add_matched_detail(matched, sku_row.get("ingredient_text"), module_name, keywords)

    material_bucket_for_sku = (material_reference or {}).get(str(sku_id or ""), {})
    material_labels = material_bucket_for_sku.get("labels", []) if isinstance(material_bucket_for_sku, dict) else material_bucket_for_sku
    for label in material_labels:
        add_matched_detail(matched, label, module_name, keywords)

    if matched:
        return unique_join(matched[:8])

    for label in module_role_labels(module, sku_id, ingredient_reference):
        add_matched_detail(matched, label, module_name, keywords)

    return unique_join(matched[:8])


def module_has_difference(
    module: Dict[str, Any],
    row: Dict[str, Any],
    target_sku: Dict[str, Any],
    score_reference: Optional[Dict[str, Any]],
    ingredient_reference: Optional[Dict[str, Dict[str, float]]],
) -> bool:
    score_field = module.get("score_field")
    if score_field and score_field in nutrition_diff_fields_for_row(row):
        return True
    if not score_field:
        difference_text = " ".join(
            as_list(row.get("key_differences") or as_dict(row.get("difference_summary")).get("key_differences"))
        )
        if any(keyword in difference_text for keyword in module.get("role_keywords", [])):
            return True

    target_id = target_sku.get("sku_id")
    candidate_id = row.get("sku_id")
    target_level, _ = module_score_label(score_field, target_id, score_reference)
    candidate_level, _ = module_score_label(score_field, candidate_id, score_reference)
    target_labels = module_role_labels(module, target_id, ingredient_reference)
    candidate_labels = module_role_labels(module, candidate_id, ingredient_reference)
    target_form, target_source = split_form_source(target_labels)
    candidate_form, candidate_source = split_form_source(candidate_labels)
    form_diff = label_set_diff(target_form, candidate_form)
    source_diff = label_set_diff(target_source, candidate_source)
    return (
        target_level != candidate_level
        or bool(form_diff[1] or form_diff[2])
        or bool(source_diff[1] or source_diff[2])
    )


def concise_module_name(module_name: str) -> str:
    return str(module_name or "").replace("模块", "")


def build_candidate_change_text(
    module_name: str,
    target_level: str,
    candidate_level: str,
    form_diff: tuple[List[str], List[str], List[str]],
    source_diff: tuple[List[str], List[str], List[str]],
) -> str:
    name = concise_module_name(module_name)
    changes = []
    if target_level != "看形态/来源" and candidate_level != "看形态/来源" and target_level != candidate_level:
        order = {"低": 0, "中": 1, "高": 2}
        if order.get(candidate_level, -1) > order.get(target_level, -1):
            changes.append(f"{name}更突出")
        elif order.get(candidate_level, -1) < order.get(target_level, -1):
            changes.append(f"{name}更弱")
    if form_diff[1] or form_diff[2]:
        changes.append("形态不同")
    if source_diff[1] or source_diff[2]:
        changes.append("来源不同")
    if not changes:
        changes.append("差异较小")
    return f"候选 SKU 相比目标 SKU，{' / '.join(changes)}。"


def build_difference_type_text(
    target_level: str,
    candidate_level: str,
    form_diff: tuple[List[str], List[str], List[str]],
    source_diff: tuple[List[str], List[str], List[str]],
) -> str:
    types = []
    if target_level != "看形态/来源" and candidate_level != "看形态/来源" and target_level != candidate_level:
        types.append("强度差异")
    if form_diff[1] or form_diff[2]:
        types.append("形态差异")
    if source_diff[1] or source_diff[2]:
        types.append("来源差异")
    if not types:
        types.append("功能差异较小")
    return " / ".join(types)


def nutrition_diff_fields_for_row(row: Dict[str, Any]) -> set[str]:
    difference = as_dict(row.get("difference_summary"))
    fields = set()
    details = [
        as_dict(item)
        for item in as_list(row.get("nutrition_component_similarities") or difference.get("nutrition_component_similarities"))
    ]
    if details:
        same_items = [
            item for item in details
            if safe_float(item.get("similarity"), 0.0) >= 0.75
        ]
        same_items.sort(key=lambda item: safe_float(item.get("similarity"), 0.0), reverse=True)
        same_base_fields = {
            str(item.get("field") or "").strip()
            for item in same_items[:2]
            if item.get("field")
        }
        diff_items = sorted(
            details,
            key=lambda item: abs(safe_float(item.get("target_strength"), 0.0) - safe_float(item.get("candidate_strength"), 0.0)),
            reverse=True,
        )
        for item in diff_items:
            field = str(item.get("field") or "").strip()
            if field and field not in same_base_fields:
                fields.add(field)
        return fields

    for item in as_list(row.get("key_differences") or difference.get("key_differences")):
        text = str(item)
        if "营养" not in text:
            continue
        for field in NUTRITION_LABEL_MAP.keys():
            if field in text:
                fields.add(field)

    if fields:
        return fields

    return fields


def nutrition_diff_label_by_field(row: Dict[str, Any], field_name: Optional[str]) -> str:
    if not field_name:
        return ""
    difference = as_dict(row.get("difference_summary"))
    details = [
        as_dict(item)
        for item in as_list(row.get("nutrition_component_similarities") or difference.get("nutrition_component_similarities"))
    ]
    for item in details:
        if str(item.get("field") or "").strip() == field_name:
            return nutrition_diff_label(item)

    field_label = NUTRITION_LABEL_MAP.get(field_name, "")
    if not field_label:
        return ""
    for item in as_list(row.get("key_differences") or difference.get("key_differences")):
        text = str(item)
        if field_name in text or field_label in text:
            if "目标" in text:
                return f"目标{field_label}压力更高"
            if "候选" in text:
                return f"候选{field_label}压力更高"
            return text
    return ""


def module_diff_focus(module_name: str, diff_label: str) -> Optional[Dict[str, str]]:
    label = str(diff_label or "")
    if not label:
        return None
    side = "目标" if "目标" in label else "候选" if "候选" in label else ""
    if not side:
        return None
    return NUTRITION_DIFF_FOCUS.get((module_name, side))


def build_seven_module_differences(
    row: Dict[str, Any],
    target_sku: Dict[str, Any],
    score_reference: Optional[Dict[str, Any]],
    ingredient_reference: Optional[Dict[str, Dict[str, float]]],
    material_reference: Optional[Dict[str, Dict[str, List[str]]]] = None,
    path_mapping_rules: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    result = []
    target_id = target_sku.get("sku_id")
    candidate_id = row.get("sku_id")
    for module in SIMILARITY_MODULES:
        if not module_has_difference(module, row, target_sku, score_reference, ingredient_reference):
            continue
        target_profile = module_path_profile(
            module,
            target_sku,
            target_id,
            ingredient_reference,
            material_reference,
            path_mapping_rules,
        )
        candidate_profile = module_path_profile(
            module,
            row,
            candidate_id,
            ingredient_reference,
            material_reference,
            path_mapping_rules,
        )
        diff_label = nutrition_diff_label_by_field(row, module.get("score_field"))
        target_process_points = target_profile["process_points"]
        candidate_process_points = candidate_profile["process_points"]
        target_indicators = target_profile["indicators"]
        candidate_indicators = candidate_profile["indicators"]
        target_feedback = target_profile["feedback"]
        candidate_feedback = candidate_profile["feedback"]
        result.append({
            "模块": module["module"],
            "差异标签": diff_label,
            "对比明细": [
                {
                    "对比项": "原料明细",
                    "目标 SKU": module_ingredient_details(
                        module,
                        target_sku,
                        target_id,
                        ingredient_reference,
                        material_reference,
                        path_mapping_rules,
                    ),
                    "候选 SKU": module_ingredient_details(
                        module,
                        row,
                        candidate_id,
                        ingredient_reference,
                        material_reference,
                        path_mapping_rules,
                    ),
                },
                *([
                    {
                        "对比项": "主动物蛋白路径",
                        "目标 SKU": protein_source_path_text(target_id, "primary", material_reference, path_mapping_rules),
                        "候选 SKU": protein_source_path_text(candidate_id, "primary", material_reference, path_mapping_rules),
                    },
                    {
                        "对比项": "次动物蛋白路径",
                        "目标 SKU": protein_source_path_text(target_id, "secondary", material_reference, path_mapping_rules),
                        "候选 SKU": protein_source_path_text(candidate_id, "secondary", material_reference, path_mapping_rules),
                    },
                    {
                        "对比项": "植物蛋白补强路径",
                        "目标 SKU": plant_protein_path_text(target_id, material_reference),
                        "候选 SKU": plant_protein_path_text(candidate_id, material_reference),
                    },
                ] if module["module"] == "蛋白质模块" else []),
                {
                    "对比项": "原料路径",
                    "目标 SKU": target_profile["path"],
                    "候选 SKU": candidate_profile["path"],
                },
                {
                    "对比项": "工艺观察点",
                    "目标 SKU": target_process_points,
                    "候选 SKU": candidate_process_points,
                },
                {
                    "对比项": "成品指标",
                    "目标 SKU": target_indicators,
                    "候选 SKU": candidate_indicators,
                },
                {
                    "对比项": "可能关联反馈",
                    "目标 SKU": target_feedback,
                    "候选 SKU": candidate_feedback,
                },
            ],
        })
    return result


def section_compare_value(section: Dict[str, Any], compare_item: str, side: str) -> str:
    for item in section.get("对比明细", []):
        if str(item.get("对比项") or "") == compare_item:
            return str(item.get(side) or "").strip()
    return ""


def canonical_pressure_material(module_name: str, path_text: str, detail_text: str) -> str:
    text = f"{path_text} {detail_text}"
    if module_name == "蛋白质模块":
        if any(keyword in text for keyword in ["鲜肉", "冻肉", "去骨", "鲜鸡", "鲜鸭", "鲜鱼"]):
            return "鲜肉"
        if any(keyword in text for keyword in ["肉粉", "鱼粉", "禽肉粉", "蛋白粉", "浓缩蛋白"]):
            return "肉粉"
    if module_name == "纤维素/纤维模块":
        if any(keyword in text for keyword in ["不溶", "不可溶", "纤维素", "木质纤维", "豌豆纤维", "竹纤维", "燕麦纤维"]):
            return "不可溶纤维"
        if any(keyword in text for keyword in ["可溶", "可发酵", "成形纤维", "菊粉", "低聚果糖", "FOS", "果寡糖", "车前子", "瓜尔胶", "甜菜粕"]):
            return "可溶纤维"
    return ""


def ingredient_item_count(text: str) -> int:
    if not text or text == "暂无":
        return 0
    return len([item for item in re.split(r"[/、,，;；]+", text) if item.strip() and item.strip() != "暂无"])


def split_path_parts(text: str) -> List[str]:
    parts = []
    for item in re.split(r"\s+/\s+|[、,，;；]+", str(text or "")):
        cleaned = item.strip()
        if cleaned and cleaned != "暂无" and cleaned not in parts:
            parts.append(cleaned)
    return parts


def compact_path_change(target_path: str, candidate_path: str) -> tuple[str, str, str]:
    target_parts = split_path_parts(target_path)
    candidate_parts = split_path_parts(candidate_path)
    target_only = [item for item in target_parts if item not in candidate_parts]
    candidate_only = [item for item in candidate_parts if item not in target_parts]
    if target_only and candidate_only:
        from_text = unique_join(target_only)
        to_text = unique_join(candidate_only)
        return f"{from_text} -> {to_text}", from_text, to_text
    if candidate_only:
        to_text = unique_join(candidate_only)
        return f"新增：{to_text}", unique_join(target_parts), to_text
    if target_only:
        from_text = unique_join(target_only)
        return f"减少：{from_text}", from_text, unique_join(candidate_parts)
    if target_parts and candidate_parts and target_parts[0] != candidate_parts[0]:
        return f"主路径变化：{target_parts[0]} -> {candidate_parts[0]}", target_parts[0], candidate_parts[0]
    path_text = unique_join(target_parts)
    return "路径顺序变化", path_text, path_text


def append_unique_signal(signals: List[Dict[str, Any]], signal: Dict[str, Any]):
    key = (signal.get("change_signal"), signal.get("from"), signal.get("to"), signal.get("candidate_sku_id"))
    for existing in signals:
        existing_key = (
            existing.get("change_signal"),
            existing.get("from"),
            existing.get("to"),
            existing.get("candidate_sku_id"),
        )
        if existing_key == key:
            return
    signals.append(signal)


def build_pressure_signals_from_module_sections(
    module_sections: List[Dict[str, Any]],
    candidate_row: Dict[str, Any],
) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []
    candidate_sku_id = str(candidate_row.get("sku_id") or "").strip()

    for section in module_sections:
        module_name = str(section.get("模块") or "")
        target_path = section_compare_value(section, "原料路径", "目标 SKU")
        candidate_path = section_compare_value(section, "原料路径", "候选 SKU")
        target_detail = section_compare_value(section, "原料明细", "目标 SKU")
        candidate_detail = section_compare_value(section, "原料明细", "候选 SKU")

        from_material = canonical_pressure_material(module_name, target_path, target_detail)
        to_material = canonical_pressure_material(module_name, candidate_path, candidate_detail)
        if from_material and to_material and from_material != to_material:
            append_unique_signal(signals, {
                "change_signal": f"{from_material} -> {to_material}",
                "from": from_material,
                "to": to_material,
                "module": module_name,
                "candidate_sku_id": candidate_sku_id,
                "evidence": f"目标路径：{target_path or '暂无'}；候选路径：{candidate_path or '暂无'}",
            })
        elif target_path and candidate_path and target_path != candidate_path:
            compact_signal, compact_from, compact_to = compact_path_change(target_path, candidate_path)
            append_unique_signal(signals, {
                "change_signal": compact_signal,
                "from": compact_from,
                "to": compact_to,
                "module": module_name,
                "candidate_sku_id": candidate_sku_id,
                "evidence": (
                    f"目标路径：{target_path or '暂无'}；候选路径：{candidate_path or '暂无'}；"
                    f"目标原料：{target_detail or '暂无'}；候选原料：{candidate_detail or '暂无'}"
                ),
                "rule_match_status": "未命中summary规则",
            })

        target_count = ingredient_item_count(target_detail)
        candidate_count = ingredient_item_count(candidate_detail)
        if module_name == "蛋白质模块" and target_count and candidate_count:
            if target_count <= 1 < candidate_count:
                append_unique_signal(signals, {
                    "change_signal": "单一蛋白 -> 多蛋白",
                    "from": "单一蛋白",
                    "to": "多蛋白",
                    "module": module_name,
                    "candidate_sku_id": candidate_sku_id,
                    "evidence": f"目标原料：{target_detail}；候选原料：{candidate_detail}",
                })
            elif target_count > 1 and candidate_count <= 1:
                append_unique_signal(signals, {
                    "change_signal": "多蛋白 -> 单一蛋白",
                    "from": "多蛋白",
                    "to": "单一蛋白",
                    "module": module_name,
                    "candidate_sku_id": candidate_sku_id,
                    "evidence": f"目标原料：{target_detail}；候选原料：{candidate_detail}",
                })

        if module_name == "纤维素/纤维模块" and target_count and candidate_count and candidate_count > target_count:
            append_unique_signal(signals, {
                "change_signal": "单一纤维 -> 复合纤维",
                "from": "单一纤维",
                "to": "复合纤维",
                "module": module_name,
                "candidate_sku_id": candidate_sku_id,
                "evidence": f"目标原料：{target_detail}；候选原料：{candidate_detail}",
            })

        if module_name == "益生元模块" and candidate_count > target_count:
            append_unique_signal(signals, {
                "change_signal": "低益生元 -> 复合益生元",
                "from": "低益生元" if target_count else "无益生元",
                "to": "复合益生元",
                "module": module_name,
                "candidate_sku_id": candidate_sku_id,
                "evidence": f"目标原料：{target_detail or '暂无'}；候选原料：{candidate_detail or '暂无'}",
            })

    return signals


def infer_process_pressure_from_skeleton_differences(
    product_rows: List[Dict[str, Any]],
    target_sku: Dict[str, Any],
    score_reference: Dict[str, Any],
    ingredient_reference: Dict[str, Dict[str, float]],
    material_reference: Dict[str, List[str]],
    path_mapping_rules: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    rows = sorted(
        product_rows,
        key=lambda item: safe_float(item.get("nutrition_similarity"), 0.0),
        reverse=True,
    )[:3]
    signals: List[Dict[str, Any]] = []
    source_sections = []
    candidate_results = []
    for row in rows:
        sections = build_seven_module_differences(
            row,
            target_sku,
            score_reference,
            ingredient_reference,
            material_reference,
            path_mapping_rules,
        )
        source_sections.append({
            "candidate_sku_id": row.get("sku_id"),
            "brand": row.get("brand"),
            "product_name": row.get("product_name"),
            "module_count": len(sections),
        })
        row_signals = build_pressure_signals_from_module_sections(sections, row)
        for signal in row_signals:
            append_unique_signal(signals, signal)
        candidate_result = build_process_pressure_result(row_signals)
        candidate_result["candidate_sku_id"] = row.get("sku_id")
        candidate_result["brand"] = row.get("brand")
        candidate_result["product_name"] = row.get("product_name")
        candidate_result["nutrition_similarity"] = row.get("nutrition_similarity")
        candidate_result["overall_similarity"] = row.get("overall_similarity")
        candidate_results.append(candidate_result)

    result = build_process_pressure_result(signals)
    result["input_signals"] = signals
    result["candidate_results"] = candidate_results
    result["source"] = "formula_skeleton_module_diagnosis"
    result["source_sections"] = source_sections
    result["summary_text"] = process_pressure_summary.fallback_summary(result)
    return result


def build_process_pressure_result(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    result = process_pressure_summary.infer_process_pressure(signals)
    matched_signal_texts = {
        str(item.get("input_signal") or "")
        for item in result.get("matched_rules", [])
    }
    for signal in signals:
        if signal.get("change_signal") in matched_signal_texts:
            signal["rule_match_status"] = "已命中summary规则"
        else:
            signal.setdefault("rule_match_status", "未命中summary规则")
    result["input_signals"] = signals
    return result


def render_process_pressure_result(process_result: Dict[str, Any]):
    input_signals = process_result.get("input_signals", [])
    candidate_results = process_result.get("candidate_results", [])

    st.markdown("### 工艺压力判断")
    st.caption("按候选产品分别读取「配方骨架差异拆解」，再按 summary.py 的原料路径变化规则推导。")
    if not input_signals:
        st.info("当前配方骨架差异拆解没有形成可识别的原料路径变化信号。")
        return

    for candidate in candidate_results:
        title = (
            f"{candidate.get('candidate_sku_id')} | "
            f"{candidate.get('brand') or ''} {candidate.get('product_name') or ''} | "
            f"营养相似 {percent_text(candidate.get('nutrition_similarity'))}"
        )
        with st.expander(title, expanded=True):
            render_single_candidate_process_pressure(candidate)

    with st.expander("汇总视图", expanded=False):
        signal_rows = [
            {
                "候选 SKU": item.get("candidate_sku_id"),
                "模块": item.get("module"),
                "变化信号": item.get("change_signal"),
                "规则状态": item.get("rule_match_status"),
                "证据": item.get("evidence"),
            }
            for item in input_signals
        ]
        st.dataframe(pd.DataFrame(signal_rows), width="stretch", hide_index=True)


def render_single_candidate_process_pressure(candidate_result: Dict[str, Any]):
    signals = candidate_result.get("input_signals", [])
    pressures = candidate_result.get("process_pressures", [])
    indicators = candidate_result.get("recommended_indicators", [])
    matched_rules = candidate_result.get("matched_rules", [])

    signal_rows = [
        {
            "模块": item.get("module"),
            "变化信号": item.get("change_signal"),
            "规则状态": item.get("rule_match_status"),
            "证据": item.get("evidence"),
        }
        for item in signals
    ]
    if signal_rows:
        st.dataframe(pd.DataFrame(signal_rows), width="stretch", hide_index=True)
    else:
        st.caption("该候选产品没有形成可展示的路径变化信号。")

    if pressures:
        st.caption("命中 summary.py 规则后生成的工艺压力")
        pressure_rows = [
            {
                "工艺压力": item.get("pressure_name"),
                "等级": item.get("level"),
                "规则分": item.get("score"),
                "原因": "；".join(item.get("reasons", [])),
            }
            for item in pressures
        ]
        st.dataframe(pd.DataFrame(pressure_rows), width="stretch", hide_index=True)
    else:
        st.caption("该候选产品已有路径差异，但没有命中 summary.py 现有工艺压力规则。")

    if indicators:
        st.caption("推荐观测指标")
        st.dataframe(pd.DataFrame(indicators), width="stretch", hide_index=True)

    if matched_rules:
        with st.expander("命中规则", expanded=False):
            st.dataframe(pd.DataFrame(matched_rules), width="stretch", hide_index=True)


def render_quality_indicators_by_candidate(process_result: Dict[str, Any]):
    candidate_results = process_result.get("candidate_results", [])
    if not candidate_results:
        st.info("没有生成质检指标。")
        return

    st.markdown("### 分产品质检指标")
    st.caption("每个候选产品的质检指标来自该产品自己的工艺压力，不与其他候选混合。")

    has_any = False
    for candidate in candidate_results:
        indicators = candidate.get("recommended_indicators", [])
        title = (
            f"{candidate.get('candidate_sku_id')} | "
            f"{candidate.get('brand') or ''} {candidate.get('product_name') or ''} | "
            f"营养相似 {percent_text(candidate.get('nutrition_similarity'))}"
        )
        with st.expander(title, expanded=True):
            if indicators:
                has_any = True
                st.dataframe(pd.DataFrame(indicators), width="stretch", hide_index=True)
            else:
                st.caption("该候选产品没有命中可生成质检指标的工艺压力规则。")

    aggregate_indicators = process_result.get("recommended_indicators", [])
    if aggregate_indicators:
        with st.expander("汇总质检指标", expanded=False):
            st.dataframe(pd.DataFrame(aggregate_indicators), width="stretch", hide_index=True)
    elif not has_any:
        st.info("当前候选产品均没有生成质检指标。")


def unique_join(values: List[Any]) -> str:
    cleaned = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return " / ".join(cleaned) if cleaned else "暂无"


def extract_breakdown_items(ingredient_text: Any, group_name: str) -> List[str]:
    text = str(ingredient_text or "")
    if not text:
        return []
    rules = INGREDIENT_BREAKDOWN_RULES[group_name]
    return [keyword for keyword in rules["keywords"] if keyword in text]


def process_detail_items_by_tag(details: Any) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    parsed = parse_json_cell(details)
    if not isinstance(parsed, list):
        return grouped

    for item in parsed:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("matched_tag") or "").strip()
        ingredient = str(item.get("ingredient_name") or "").strip()
        contribution = item.get("contribution")
        if not tag or not ingredient:
            continue
        label = ingredient
        if contribution not in (None, ""):
            label = f"{ingredient}({contribution})"
        grouped.setdefault(tag, [])
        if label not in grouped[tag]:
            grouped[tag].append(label)
    return grouped


def module_rows(modules: Any) -> List[Dict[str, Any]]:
    parsed = parse_json_cell(modules)
    if not isinstance(parsed, list):
        return []
    rows = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        module = item.get("module")
        rows.append({
            "模块": PROCESS_MODULE_LABELS.get(module, module),
            "模块字段": module,
            "分数": item.get("score"),
        })
    return rows


def render_process_breakdown(target_sku: Dict[str, Any], similar_row: Dict[str, Any]):
    target_tags = as_list(target_sku.get("main_process_tags"))
    similar_tags = as_list(similar_row.get("main_process_tags"))
    target_items = process_detail_items_by_tag(target_sku.get("process_tag_details"))
    similar_items = process_detail_items_by_tag(similar_row.get("process_tag_details"))
    all_tags = sorted(set(map(str, target_tags)) | set(map(str, similar_tags)) | set(target_items) | set(similar_items))

    st.subheader("工艺维度拆解")
    c1, c2 = st.columns(2)
    with c1:
        st.caption("目标 SKU 工艺摘要")
        st.write(target_sku.get("process_structure_summary") or "暂无")
    with c2:
        st.caption("相似 SKU 工艺摘要")
        st.write(similar_row.get("process_structure_summary") or "暂无")

    rows = []
    for tag in all_tags:
        target_actual = target_items.get(tag, [])
        similar_actual = similar_items.get(tag, [])
        rows.append({
            "工艺标签": tag,
            "目标SKU实际内容": unique_join(target_actual),
            "相似SKU实际内容": unique_join(similar_actual),
            "共同命中": "是" if tag in set(map(str, target_tags)) and tag in set(map(str, similar_tags)) else "否",
        })

    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.caption("暂无工艺画像明细。")

    target_module_rows = module_rows(target_sku.get("main_process_modules"))
    similar_module_rows = module_rows(similar_row.get("main_process_modules"))
    if target_module_rows or similar_module_rows:
        c1, c2 = st.columns(2)
        with c1:
            st.caption("目标 SKU 主导工艺模块")
            st.dataframe(pd.DataFrame(target_module_rows), width="stretch", hide_index=True)
        with c2:
            st.caption("相似 SKU 主导工艺模块")
            st.dataframe(pd.DataFrame(similar_module_rows), width="stretch", hide_index=True)


def render_structured_risk_reasons(tags: List[Any]):
    tag_texts = [str(tag).strip() for tag in tags if str(tag).strip()]
    rows = []
    matched = set()

    for group_name, expected_tags in RISK_REASON_GROUPS.items():
        hits = []
        for expected_tag in expected_tags:
            if any(expected_tag in tag_text or tag_text in expected_tag for tag_text in tag_texts):
                hits.append(expected_tag)
                matched.add(expected_tag)
        rows.append({
            "结构": group_name,
            "命中标签": unique_join(hits),
        })

    extra_tags = [
        tag_text for tag_text in tag_texts
        if not any(expected_tag in tag_text or tag_text in expected_tag for expected_tag in matched)
    ]
    if extra_tags:
        rows.append({
            "结构": "其他",
            "命中标签": unique_join(extra_tags),
        })

    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def render_ingredient_breakdown(target_sku: Dict[str, Any], similar_row: Dict[str, Any]):
    target_text = target_sku.get("ingredient_text", "")
    similar_text = similar_row.get("ingredient_text", "")
    rows = []

    for group_name, rules in INGREDIENT_BREAKDOWN_RULES.items():
        target_items = extract_breakdown_items(target_text, group_name)
        similar_items = extract_breakdown_items(similar_text, group_name)
        shared_items = sorted(set(target_items) & set(similar_items))
        rows.append({
            "拆解维度": group_name,
            "标签": unique_join(rules["tags"]),
            "目标SKU实际内容": unique_join(target_items),
            "相似SKU实际内容": unique_join(similar_items),
            "共同内容": unique_join(shared_items),
        })

    st.subheader("原料维度拆解")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def render_formula_details(target_sku: Dict[str, Any], product_rows: List[Dict[str, Any]], comparison_mode: str = ""):
    target_formula = str(target_sku.get("ingredient_text") or "").strip()
    target_label = "产品A（基准产品）配方" if comparison_mode == "pair" else "目标 SKU 配方"
    product_rows_label = "产品B（对比产品）配方" if comparison_mode == "pair" else "相似 SKU 配方"
    if target_formula:
        st.subheader(target_label)
        st.text_area(
            target_label,
            value=target_formula,
            height=140,
            label_visibility="collapsed",
            disabled=True,
        )

    formula_rows = []
    for row in product_rows:
        formula_rows.append({
            "sku_id": row.get("sku_id"),
            "brand": row.get("brand"),
            "product_name": row.get("product_name"),
            "overall_similarity": row.get("overall_similarity"),
            "ingredient_text": row.get("ingredient_text") or "",
        })

    if formula_rows:
        st.subheader(product_rows_label)
        st.dataframe(pd.DataFrame(formula_rows), width="stretch", hide_index=True)
    elif not target_formula:
        st.info("当前结果没有配方明细。请使用「目标 SKU」模式重新运行一次分析。")


def capability_names(profile: Dict[str, Any]) -> List[str]:
    names = []
    for item in profile.get("all_capabilities", []):
        name = str(item.get("capability_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def fallback_formula_profile_summary(profile_rows: List[Dict[str, Any]]) -> str:
    if not profile_rows:
        return "暂无可总结的产品画像。"

    capability_counts: Dict[str, int] = {}
    for row in profile_rows:
        for name in row.get("capability_names", []):
            capability_counts[name] = capability_counts.get(name, 0) + 1
    common_names = [
        name for name, count in sorted(capability_counts.items(), key=lambda item: (-item[1], item[0]))
        if count >= 2
    ]
    common_text = "、".join(common_names[:6]) or "暂无稳定共同画像能力，当前候选更适合分产品观察。"

    diff_lines = []
    for row in profile_rows:
        main_text = row.get("main_text") or "暂无"
        evidence_text = row.get("evidence_text") or "暂无"
        diff_lines.append(
            f"- {row.get('sku_id')}：主画像为{main_text}；主要证据为{evidence_text}。"
        )
    return "共同内容：\n" + common_text + "\n\n差异内容：\n" + "\n".join(diff_lines)


@st.cache_data(ttl=300)
def summarize_formula_profiles_with_qwen(profile_rows_json: str) -> str:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return fallback_formula_profile_summary(json.loads(profile_rows_json))

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        model = os.getenv("QWEN_MODEL", "qwen-plus")
        profile_rows = json.loads(profile_rows_json)
        prompt = f"""
你是宠物食品配方画像分析师。请基于多个候选产品的配方画像，输出适合放在B端产品页面里的简洁总结。

要求：
1. 只基于输入内容总结，不要编造不存在的配方、功效或工艺。
2. 分成两段：产品配方共同内容、产品配方差异内容。
3. 共同内容总结这些产品共享的配方骨架、能力方向或证据标签。
4. 差异内容按产品说明各自最突出的画像差异。
5. 控制在250字以内，语气专业、直接。

输入：
{json.dumps(profile_rows, ensure_ascii=False, indent=2)}
"""
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是专业的宠物食品配方画像分析师。"},
                {"role": "user", "content": prompt.strip()},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        return fallback_formula_profile_summary(json.loads(profile_rows_json)) + f"\n\n[大模型总结失败，已使用规则兜底：{exc}]"


def render_similar_products_with_formula_profile(
    product_rows: List[Dict[str, Any]],
    target_sku: Optional[Dict[str, Any]] = None,
    comparison_mode: str = "",
):
    if not product_rows:
        st.info("没有检索到相似产品。")
        return

    profile_rows = list(product_rows)
    if comparison_mode == "pair" and target_sku:
        target_profile_row = dict(target_sku)
        target_profile_row["_product_role"] = "基准产品A"
        profile_rows = [target_profile_row] + [
            {**row, "_product_role": "对比产品B"}
            for row in product_rows
        ]

    sku_ids = tuple(str(row.get("sku_id") or "").strip() for row in profile_rows if row.get("sku_id"))
    profiles = load_formula_capability_profiles(sku_ids)
    display_rows = []
    summary_rows = []
    for row in profile_rows:
        sku_id = str(row.get("sku_id") or "").strip()
        profile = profiles.get(sku_id) or fallback_formula_profile_from_product_row(row)
        if profile and sku_id not in profiles:
            profiles[sku_id] = profile
        display_row = {
            "sku_id": row.get("sku_id"),
            "brand": row.get("brand"),
            "product_name": row.get("product_name"),
            "画像名称": profile.get("main_text") or "暂无",
            "一级标签": profile.get("assist_text") or "暂无",
            "风险短板": profile.get("risk_text") or "暂无",
            "证据标签": profile.get("evidence_text") or "暂无",
        }
        if comparison_mode == "pair":
            display_row = {"产品角色": row.get("_product_role") or "对比产品", **display_row}
        display_rows.append(display_row)
        summary_rows.append({
            "sku_id": sku_id,
            "product_role": row.get("_product_role") or "候选产品",
            "brand": row.get("brand"),
            "product_name": row.get("product_name"),
            "main_text": profile.get("main_text") or "暂无",
            "assist_text": profile.get("assist_text") or "暂无",
            "risk_text": profile.get("risk_text") or "暂无",
            "description_text": profile.get("description_text") or "暂无",
            "evidence_text": profile.get("evidence_text") or "暂无",
            "capability_names": capability_names(profile),
        })

    st.dataframe(pd.DataFrame(display_rows), width="stretch", hide_index=True)

    st.markdown("### 配方画像说明")
    summary_json = json.dumps(summary_rows, ensure_ascii=False, sort_keys=True)
    st.write(summarize_formula_profiles_with_qwen(summary_json))

    st.caption("优先使用 sku_nutrition_structure_tags、sku_function_structure_tags、sku_ingredient_path_tags；若三表缺失或该 SKU 无数据，则用当前相似产品结果做临时画像兜底。")


def percent_text(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except Exception:
        return "-"


def problem_probability_level(value: Any) -> str:
    rate = safe_float(value, 0.0)
    if rate >= 0.65:
        return "高"
    if rate >= 0.35:
        return "中"
    if rate > 0:
        return "低"
    return "暂无"


def load_feedback_rows_for_skus(sku_ids: List[Any]) -> Dict[str, Dict[str, Any]]:
    cleaned_ids = []
    for sku_id in sku_ids:
        text = str(sku_id or "").strip()
        if text and text not in cleaned_ids:
            cleaned_ids.append(text)
    if not cleaned_ids:
        return {}
    with get_conn() as conn:
        rows = b2b.fetch_feedback_for_skus(conn, cleaned_ids)
    return {str(row.get("sku_id")): row for row in rows}


def symptom_comparison_rows(
    product_rows: List[Dict[str, Any]],
    target_sku: Dict[str, Any],
    feedback_by_sku: Dict[str, Dict[str, Any]],
    score_reference: Dict[str, Any],
) -> List[Dict[str, Any]]:
    target_id = str(target_sku.get("sku_id") or "").strip()
    rows: List[Dict[str, Any]] = []

    if target_id:
        target_feedback = feedback_by_sku.get(target_id, {})
        target_row = {
            "类型": "目标 SKU",
            "SKU": target_id,
            "品牌产品": f"{target_sku.get('brand') or ''} {target_sku.get('product_name') or ''}".strip(),
            "召回分组": "-",
            "营养相似": "-",
            "综合相似": "-",
            "差异标签": "-",
            "反馈标签": unique_join(as_list(target_feedback.get("derived_tags"))[:8]),
        }
        for label, field in SYMPTOM_RATE_FIELDS:
            target_row[label] = percent_text(target_feedback.get(field))
        rows.append(target_row)

    recalled_rows = sorted(
        product_rows,
        key=lambda item: safe_float(item.get("nutrition_similarity"), 0.0),
        reverse=True,
    )
    for row in recalled_rows:
        sku_id = str(row.get("sku_id") or "").strip()
        feedback = feedback_by_sku.get(sku_id, {})
        _, diff_labels = nutrition_same_diff_labels(row, target_sku, score_reference)
        display_row = {
            "类型": "相似召回",
            "SKU": sku_id,
            "品牌产品": f"{row.get('brand') or ''} {row.get('product_name') or ''}".strip(),
            "召回分组": unique_join(as_list(row.get("recall_groups"))),
            "营养相似": percent_text(row.get("nutrition_similarity")),
            "综合相似": percent_text(row.get("overall_similarity")),
            "差异标签": format_nutrition_diff_labels(diff_labels),
            "反馈标签": unique_join(as_list(feedback.get("derived_tags"))[:8]),
        }
        for label, field in SYMPTOM_RATE_FIELDS:
            display_row[label] = percent_text(feedback.get(field))
        rows.append(display_row)
    return rows


def secondary_symptom_probability_rows(
    product_rows: List[Dict[str, Any]],
    feedback_by_sku: Dict[str, Dict[str, Any]],
    score_reference: Dict[str, Any],
    target_sku: Dict[str, Any],
) -> List[Dict[str, Any]]:
    probability_rows = []
    candidates = [
        row for row in product_rows
        if str(row.get("sku_id") or "").strip() in feedback_by_sku
    ]
    for label, field in DISPLAY_SECONDARY_SYMPTOM_FIELDS:
        weighted_sum = 0.0
        weight_sum = 0.0
        max_rate = 0.0
        max_sku = ""
        affected_count = 0
        for row in candidates:
            sku_id = str(row.get("sku_id") or "").strip()
            feedback = feedback_by_sku.get(sku_id, {})
            rate = safe_float(feedback.get(field), 0.0)
            weight = max(
                safe_float(row.get("nutrition_similarity"), 0.0),
                safe_float(row.get("overall_similarity"), 0.0),
                0.05,
            )
            weighted_sum += rate * weight
            weight_sum += weight
            if rate > 0:
                affected_count += 1
            if rate > max_rate:
                max_rate = rate
                max_sku = sku_id
        if not candidates:
            continue
        probability = weighted_sum / weight_sum if weight_sum else 0.0
        probability_rows.append({
            "二级病症": label,
            "相似召回发生概率": round(probability, 4),
            "概率": percent_text(probability),
            "等级": problem_probability_level(probability),
            "命中 SKU 数": affected_count,
            "召回 SKU 数": len(candidates),
            "最高 SKU": max_sku or "暂无",
            "单 SKU 最高概率": percent_text(max_rate),
        })
    probability_rows.sort(key=lambda item: item["相似召回发生概率"], reverse=True)
    return probability_rows


def symptom_probability_matrix_rows(
    product_rows: List[Dict[str, Any]],
    feedback_by_sku: Dict[str, Dict[str, Any]],
    score_reference: Dict[str, Any],
    target_sku: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows = []
    target_id = str(target_sku.get("sku_id") or "").strip()
    if target_id:
        target_feedback = feedback_by_sku.get(target_id, {})
        target_row = {
            "类型": "目标 SKU",
            "SKU": target_id,
            "品牌产品": f"{target_sku.get('brand') or ''} {target_sku.get('product_name') or ''}".strip(),
        }
        for label, field in DISPLAY_SECONDARY_SYMPTOM_FIELDS:
            target_row[label] = percent_text(target_feedback.get(field))
        rows.append(target_row)

    recalled_rows = sorted(
        product_rows,
        key=lambda item: safe_float(item.get("nutrition_similarity"), 0.0),
        reverse=True,
    )
    for row in recalled_rows:
        sku_id = str(row.get("sku_id") or "").strip()
        feedback = feedback_by_sku.get(sku_id, {})
        matrix_row = {
            "类型": "相似召回",
            "SKU": sku_id,
            "品牌产品": f"{row.get('brand') or ''} {row.get('product_name') or ''}".strip(),
        }
        for label, field in DISPLAY_SECONDARY_SYMPTOM_FIELDS:
            matrix_row[label] = percent_text(feedback.get(field))
        rows.append(matrix_row)
    return rows


def secondary_symptom_summary_matrix_rows(probability_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not probability_rows:
        return []
    probability_by_label = {
        str(row.get("二级病症") or ""): row
        for row in probability_rows
    }
    summary_row = {"统计口径": "相似召回加权概率"}
    level_row = {"统计口径": "概率等级"}
    for label, _ in DISPLAY_SECONDARY_SYMPTOM_FIELDS:
        row = probability_by_label.get(label, {})
        summary_row[label] = row.get("概率") or "-"
        level_row[label] = row.get("等级") or "-"
    return [summary_row, level_row]


def render_problem_cluster_from_similarity(
    product_rows: List[Dict[str, Any]],
    target_sku: Dict[str, Any],
):
    if not product_rows:
        st.info("没有相似度召回内容，无法生成问题聚类。")
        return

    target_id = str(target_sku.get("sku_id") or "").strip()
    sku_ids = [target_id] + [row.get("sku_id") for row in product_rows]
    feedback_by_sku = load_feedback_rows_for_skus(sku_ids)
    score_reference = load_nutrition_score_reference()

    comparison_rows = symptom_comparison_rows(product_rows, target_sku, feedback_by_sku, score_reference)
    probability_rows = secondary_symptom_probability_rows(
        product_rows,
        feedback_by_sku,
        score_reference,
        target_sku,
    )
    summary_matrix_rows = secondary_symptom_summary_matrix_rows(probability_rows)
    matrix_rows = symptom_probability_matrix_rows(
        product_rows,
        feedback_by_sku,
        score_reference,
        target_sku,
    )

    st.markdown("### 症状对比表")
    st.caption("基于当前相似度召回 SKU 展示症状信号，并把营养结构差异标签放入候选 SKU 行。")
    if comparison_rows:
        display_columns = ["类型", "SKU", "品牌产品", "召回分组", "营养相似", "综合相似", "差异标签"]
        comparison_df = pd.DataFrame(comparison_rows)
        st.dataframe(comparison_df[[col for col in display_columns if col in comparison_df.columns]], width="stretch", hide_index=True)
    else:
        st.info("当前召回 SKU 没有可用症状信号。")

    st.markdown("### 二级病症发生概率")
    st.caption("行是相似召回 SKU，列是二级病症；每个单元格为该 SKU 的症状信号概率。")
    if matrix_rows:
        st.dataframe(pd.DataFrame(matrix_rows), width="stretch", hide_index=True)
    else:
        st.info("暂无可聚合的二级病症概率。")

    if summary_matrix_rows:
        st.caption("相似召回汇总概率")
        st.dataframe(pd.DataFrame(summary_matrix_rows), width="stretch", hide_index=True)


def three_level(score: Any) -> str:
    score_number = safe_float(score, 0.0)
    if score_number >= 0.75:
        return "高"
    if score_number >= 0.50:
        return "中"
    return "低"


def dimension_meaning(key: str, level: str) -> str:
    meanings = {
        "ingredient": {
            "高": "两款产品的原料类别组合比较接近，适合观察共同配方骨架。",
            "中": "两款产品有部分共同原料类别，可做同类参考但不宜强归因。",
            "低": "两款产品原料路径差异较大，更适合观察营养或工艺共因。",
        },
        "nutrition": {
            "高": "两款产品最终形成的蛋白、脂肪、碳水、纤维等营养压力比较接近。",
            "中": "两款产品营养压力有一定接近，但配比强度仍可能解释反馈差异。",
            "低": "两款产品营养压力差异明显，适合观察同类配方下的风险边界。",
        },
        "process": {
            "高": "两款产品在加工方式、成型、后喷或氧化控制等工艺路径上比较接近。",
            "中": "两款产品工艺路径部分相似，可用于辅助排查生产变量。",
            "低": "两款产品工艺路径差异较大，反馈差异可能与工艺变量有关。",
        },
    }
    return meanings[key][level]


def diagnosis_type(row: Dict[str, Any]) -> str:
    difference = as_dict(row.get("difference_summary"))
    pattern = str(row.get("similarity_pattern") or difference.get("similarity_pattern") or "").strip()
    if pattern:
        return pattern

    ingredient_level = three_level(row.get("ingredient_similarity"))
    nutrition_level = three_level(row.get("nutrition_similarity"))
    process_level = three_level(row.get("process_structure_similarity"))
    if ingredient_level == nutrition_level == process_level == "高":
        return "配方-营养-工艺三重接近型"
    if ingredient_level == "高" and nutrition_level == "高" and process_level == "低":
        return "同类配方同营养，工艺分化型"
    if ingredient_level == "高" and nutrition_level == "低" and process_level == "高":
        return "同类配方同工艺，营养强度分化型"
    if ingredient_level == "低" and nutrition_level == "高" and process_level == "高":
        return "异源配方，营养与工艺收敛型"
    return "三维结构参考型"


def diagnosis_explanation(row: Dict[str, Any]) -> str:
    difference = as_dict(row.get("difference_summary"))
    text = str(row.get("business_interpretation") or difference.get("business_interpretation") or "").strip()
    if text:
        return text
    pattern = diagnosis_type(row)
    return f"该候选 SKU 的组合标签为【{pattern}】。建议结合配方骨架、营养压力和工艺路径三维证据判断相似关系。"


def diagnosis_best_for(row: Dict[str, Any]) -> str:
    ingredient_level = three_level(row.get("ingredient_similarity"))
    nutrition_level = three_level(row.get("nutrition_similarity"))
    process_level = three_level(row.get("process_structure_similarity"))
    if ingredient_level == "高" and nutrition_level == "高" and process_level == "高":
        return "同类产品是否出现相同反馈，以及问题反馈迁移观察。"
    if ingredient_level == "高" and nutrition_level == "高" and process_level == "低":
        return "判断反馈差异是否来自工艺路径。"
    if ingredient_level == "高" and nutrition_level == "低" and process_level == "高":
        return "判断问题来自配方类别，还是来自配比强度。"
    if ingredient_level == "低" and nutrition_level == "高" and process_level == "高":
        return "观察共同压力路径，而不是直接归因到某个具体原料。"
    return "形成相似 SKU 的观察假设，并辅助后续反馈和质检排查。"


def observation_dicts(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    difference = as_dict(row.get("difference_summary"))
    raw_points = as_list(row.get("observation_points") or difference.get("observation_points"))
    result = []
    for idx, item in enumerate(raw_points[:8], start=1):
        if isinstance(item, dict):
            result.append({
                "title": item.get("title") or f"观察项 {idx}",
                "reason": item.get("reason") or item.get("description") or "",
                "check_items": as_list(item.get("check_items") or item.get("items")),
            })
        else:
            text = str(item).strip()
            if text:
                result.append({"title": f"观察项 {idx}", "reason": text, "check_items": []})
    return result


def evidence_summary(row: Dict[str, Any]) -> Dict[str, str]:
    ingredient_level = three_level(row.get("ingredient_similarity"))
    nutrition_level = three_level(row.get("nutrition_similarity"))
    process_level = three_level(row.get("process_structure_similarity"))
    difference = as_dict(row.get("difference_summary"))
    high_nutrition_parts = as_list(
        row.get("high_nutrition_similarity_parts")
        or difference.get("high_nutrition_similarity_parts")
    )
    nutrition_focus = "、".join(str(item) for item in high_nutrition_parts[:6]) or "脂肪、蛋白、碳水、纤维、益生元、抗氧化"

    if nutrition_level == "高" and not (ingredient_level == "高" and process_level == "高"):
        return {
            "相似类型": "共同营养压力观察型",
            "含义": "两款产品的营养结构高度接近，说明它们可能形成了相似的蛋白、脂肪、碳水、纤维、益生元或抗氧化压力。原料和工艺只是部分相似，暂不适合直接归因为共同原料或共同工艺。",
            "适合看": "如果两款产品出现相同症状，优先判断问题是否来自共同营养压力；再观察原料和工艺是否存在放大因素。",
            "重点观察": f"{nutrition_focus}。",
            "辅助观察": "共同原料类别、油脂后喷、表油、氧化控制、淀粉成型。",
        }

    if ingredient_level == "高" and nutrition_level == "高" and process_level == "高":
        return {
            "相似类型": "高度同类参考型",
            "含义": "两款产品在配方骨架、营养压力和工艺路径上都高度接近，适合作为同类 SKU 参考。",
            "适合看": "如果两款产品出现相同反馈，可以优先观察共同结构是否存在稳定风险。",
            "重点观察": "共同原料结构、共同营养压力、共同工艺风险。",
            "辅助观察": "批次差异、原料品质、喷涂均匀度、氧化控制、用户反馈集中点。",
        }

    if ingredient_level == "高" and nutrition_level == "高" and process_level != "高":
        return {
            "相似类型": "同配方同营养，工艺差异观察型",
            "含义": "两款产品的原料类别和营养压力接近，但工艺路径没有达到高度相似。",
            "适合看": "如果反馈不同，优先判断差异是否来自成型、后喷、干燥或氧化控制等工艺变量。",
            "重点观察": "工艺路径、油脂后喷、颗粒成型、干燥控制、氧化控制。",
            "辅助观察": "共同原料类别、脂肪压力、蛋白压力、碳水压力。",
        }

    if ingredient_level == "高" and process_level == "高" and nutrition_level != "高":
        return {
            "相似类型": "同配方同工艺，营养强度观察型",
            "含义": "两款产品的原料类别和工艺路径接近，但营养结构存在差异。",
            "适合看": "判断问题到底来自配方类别本身，还是来自脂肪、蛋白、碳水、纤维等配比强度。",
            "重点观察": "脂肪强度、蛋白强度、碳水压力、纤维支持、益生元支持。",
            "辅助观察": "共同原料类别、共同工艺路径、油脂后喷、颗粒成型。",
        }

    if process_level == "高":
        return {
            "相似类型": "共同工艺路径观察型",
            "含义": "两款产品的工艺结构高度接近，但原料或营养不一定高度相似。",
            "适合看": "如果两款产品出现相似反馈，优先观察是否存在共同生产路径或质量控制变量。",
            "重点观察": "膨化成型、油脂后喷、表油残留、氧化控制、颗粒稳定性。",
            "辅助观察": "脂肪压力、碳水压力、共同原料类别、批次一致性。",
        }

    if ingredient_level == "高":
        return {
            "相似类型": "共同配方骨架观察型",
            "含义": "两款产品的原料类别较接近，但营养压力和工艺路径未形成强一致。",
            "适合看": "适合观察同类原料骨架下，不同配比和不同工艺是否带来反馈差异。",
            "重点观察": "共同动物蛋白、共同碳水来源、共同脂肪来源、功能添加结构。",
            "辅助观察": "营养强度、工艺路径、表油、氧化控制、颗粒成型。",
        }

    return {
        "相似类型": "弱结构参考型",
        "含义": "两款产品在配方、营养和工艺上没有形成明确的强相似主轴。",
        "适合看": "仅适合作为辅助参考，不建议直接做问题归因迁移。",
        "重点观察": "是否存在某个单项维度达到中度相似。",
        "辅助观察": "用户反馈、批次信息、原料品质、工艺质量指标。",
    }


def render_dimension_top_products(
    product_rows: List[Dict[str, Any]],
    target_sku: Optional[Dict[str, Any]] = None,
    score_reference: Optional[Dict[str, Any]] = None,
    ingredient_reference: Optional[Dict[str, Dict[str, float]]] = None,
):
    top_specs = [
        ("营养结构相似 Top3", "nutrition_similarity", "按营养结构相似项输出候选产品。", nutrition_same_diff_labels),
    ]

    for title, score_field, caption, label_func in top_specs:
        rows = sorted(
            product_rows,
            key=lambda item: safe_float(item.get(score_field), 0.0),
            reverse=True,
        )[:3]
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.caption(caption)
            display_rows = []
            for idx, row in enumerate(rows, start=1):
                if score_field == "nutrition_similarity":
                    same_labels, diff_labels = label_func(row, target_sku, score_reference)
                else:
                    same_labels, diff_labels = label_func(row, target_sku, ingredient_reference)
                display_rows.append({
                    "排名": idx,
                    "SKU": row.get("sku_id"),
                    "品牌产品": f"{row.get('brand') or ''} {row.get('product_name') or ''}".strip(),
                    "相似度": percent_text(row.get(score_field)),
                    "相同标签": unique_join(same_labels) or "暂无",
                    "差异标签": format_nutrition_diff_labels(diff_labels),
                    "综合": percent_text(row.get("overall_similarity")),
                })
            st.dataframe(pd.DataFrame(display_rows), width="stretch", hide_index=True)


def render_formula_skeleton_module_diagnosis(
    product_rows: List[Dict[str, Any]],
    target_sku: Dict[str, Any],
    score_reference: Dict[str, Any],
    ingredient_reference: Dict[str, Dict[str, float]],
    material_reference: Dict[str, List[str]],
    path_mapping_rules: Dict[str, List[Dict[str, Any]]],
):
    rows = sorted(
        product_rows,
        key=lambda item: safe_float(item.get("nutrition_similarity"), 0.0),
        reverse=True,
    )[:3]
    st.markdown("### 配方骨架差异拆解")
    st.caption("基于营养结构相似项，对蛋白质、碳水、脂肪、纤维、益生元、抗氧化、适口性喷涂七个模块按「强度 -> 形态 -> 来源」拆解。")
    for row in rows:
        title = f"{row.get('sku_id')} | {row.get('brand') or ''} {row.get('product_name') or ''} | 营养相似 {percent_text(row.get('nutrition_similarity'))}"
        with st.expander(title, expanded=False):
            module_sections = build_seven_module_differences(
                row,
                target_sku,
                score_reference,
                ingredient_reference,
                material_reference,
                path_mapping_rules,
            )
            if not module_sections:
                st.caption("暂无明确模块差异。")
                continue
            for section in module_sections:
                module_title = section["模块"]
                if section.get("差异标签"):
                    module_title = f"{module_title}（{section['差异标签']}）"
                st.markdown(f"**{module_title}**")
                st.dataframe(pd.DataFrame(section["对比明细"]), width="stretch", hide_index=True)


def render_dimension_card(title: str, score: Any, shared: List[Any], differences: List[Any], key: str):
    level = three_level(score)
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"**{title}**")
            st.caption(dimension_meaning(key, level))
        with c2:
            st.metric(level, percent_text(score))
        st.progress(min(1.0, max(0.0, safe_float(score, 0.0))))
        rows = [
            {
                "层级": level,
                "共同点": unique_join([str(x) for x in shared[:8]]),
                "差异点": unique_join([str(x) for x in differences[:8]]),
            }
        ]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def render_similarity_details(product_rows: List[Dict[str, Any]], target_sku: Dict[str, Any]):
    detailed_rows = [
        row for row in product_rows
        if row.get("overall_similarity") is not None
    ]
    if not detailed_rows:
        st.info("当前结果没有相似度分项。请使用「目标 SKU」模式重新运行一次分析。")
        return

    score_reference = load_nutrition_score_reference()
    ingredient_reference = load_ingredient_role_reference()
    material_reference = load_material_path_reference()
    path_mapping_rules = load_path_mapping_rules()
    render_dimension_top_products(
        detailed_rows,
        target_sku,
        score_reference,
        ingredient_reference,
    )
    render_formula_skeleton_module_diagnosis(
        detailed_rows,
        target_sku,
        score_reference,
        ingredient_reference,
        material_reference,
        path_mapping_rules,
    )


def render_result(result: Dict[str, Any]):
    if not result:
        return

    product_rows = result.get("product_search_result", [])
    target_sku = result.get("target_sku") or {}
    if not target_sku and result.get("target_sku_id"):
        target_sku = {"sku_id": result.get("target_sku_id")}
    comparison_mode = str(result.get("comparison_mode") or "")
    if not result.get("preserve_product_rows"):
        product_rows = filter_cross_brand_products(product_rows, target_sku, result.get("target_sku_id"))
    score_reference = load_nutrition_score_reference()
    ingredient_reference = load_ingredient_role_reference()
    material_reference = load_material_path_reference()
    path_mapping_rules = load_path_mapping_rules()
    process_pressure_result = infer_process_pressure_from_skeleton_differences(
        product_rows,
        target_sku,
        score_reference,
        ingredient_reference,
        material_reference,
        path_mapping_rules,
    )
    failure_rows = process_pressure_result.get("process_pressures", [])
    quality_rows = process_pressure_result.get("recommended_indicators", [])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("相似产品", len(product_rows))
    c2.metric("问题聚类", len(DISPLAY_SECONDARY_SYMPTOM_FIELDS) if product_rows else 0)
    c3.metric("工艺压力", len(failure_rows))
    c4.metric("观测指标", len(quality_rows))

    tabs = st.tabs(["产品画像", "配方明细", "相似度详情", "问题聚类", "翻车点", "质检指标", "原始 JSON"])
    with tabs[0]:
        if comparison_mode == "pair":
            st.caption("两品对比中，产品A作为基准产品，产品B作为对比产品；下方画像会同时展示两款产品。")
        render_similar_products_with_formula_profile(product_rows, target_sku=target_sku, comparison_mode=comparison_mode)

    with tabs[1]:
        render_formula_details(target_sku, product_rows, comparison_mode=comparison_mode)

    with tabs[2]:
        render_similarity_details(product_rows, target_sku)

    with tabs[3]:
        render_problem_cluster_from_similarity(product_rows, target_sku)

    with tabs[4]:
        render_process_pressure_result(process_pressure_result)

    with tabs[5]:
        render_quality_indicators_by_candidate(process_pressure_result)

    with tabs[6]:
        st.code(json_preview(result), language="json")


def _formula_profile_dataset(limit: int = 5000) -> tuple[pd.DataFrame, pd.DataFrame]:
    sku_options = load_all_sku_options(limit)
    if sku_options.empty:
        return pd.DataFrame(), pd.DataFrame()

    sku_ids = tuple(str(row.get("sku_id") or "").strip() for _, row in sku_options.iterrows() if row.get("sku_id"))
    profiles: Dict[str, Dict[str, Any]] = {}
    base_tag_df = load_formula_base_tags_from_feature_tables()
    if not base_tag_df.empty:
        base_tag_df = base_tag_df[base_tag_df["sku_id"].isin(sku_ids)].copy()
        capability_df = sku_function_structure_rules.generate_sku_formula_capabilities(base_tag_df)
        if not capability_df.empty:
            for profile_sku_id, group in capability_df.groupby("sku_id"):
                profiles[str(profile_sku_id)] = profile_from_capability_records(group.to_dict("records"))

    if not profiles:
        profiles = load_formula_capability_profiles(sku_ids)

    product_rows: List[Dict[str, Any]] = []
    capability_rows: List[Dict[str, Any]] = []
    for _, sku_row in sku_options.iterrows():
        row = sku_row.to_dict()
        sku_id = str(row.get("sku_id") or "").strip()
        brand_origin = classify_brand_origin(row.get("brand"), row.get("importer"))
        profile = profiles.get(sku_id) or fallback_formula_profile_from_product_row(row)
        all_capabilities = profile.get("all_capabilities", []) if profile else []
        has_profile = bool(all_capabilities)
        product_rows.append({
            "sku_id": sku_id,
            "品牌": row.get("brand") or "",
            "品牌类型": brand_origin,
            "产品名": row.get("product_name") or "",
            "是否有画像": "是" if has_profile else "否",
            "画像名称": profile.get("main_text") if profile else "暂无",
            "一级标签": profile.get("assist_text") if profile else "暂无",
            "风险短板": profile.get("risk_text") if profile else "暂无",
            "证据标签": profile.get("evidence_text") if profile else "暂无",
        })

        for item in all_capabilities:
            capability_name = item.get("capability_name") or ""
            capability_rows.append({
                "sku_id": sku_id,
                "品牌": row.get("brand") or "",
                "品牌类型": brand_origin,
                "产品名": row.get("product_name") or "",
                "能力角色": item.get("display_role") or "",
                "配方能力": capability_name,
                "能力类型": classify_formula_capability_type(capability_name),
                "能力等级": item.get("capability_level") or "",
                "能力分": safe_float(item.get("capability_score"), 0.0),
            })

    return pd.DataFrame(product_rows), pd.DataFrame(capability_rows)


def classify_formula_capability_type(capability_name: Any) -> str:
    name = str(capability_name or "")
    if "风险短板" in name or "不足" in name or "风险" in name or "压力偏高" in name:
        return "风险短板"
    if any(keyword in name for keyword in ["脂肪", "鱼油", "Omega", "动物脂肪", "鱼源皮毛", "皮毛支持"]):
        return "脂肪结构能力"
    if any(keyword in name for keyword in ["蛋白", "肉源", "肉粉", "鲜肉", "冻肉", "植物蛋白", "同源", "多肉源"]):
        return "蛋白结构能力"
    if any(keyword in name for keyword in ["碳水", "无谷", "豆薯", "淀粉", "谷物"]):
        return "碳水结构能力"
    if any(keyword in name for keyword in ["纤维", "肠胃", "便便成形", "菌群", "益生元", "供菌"]):
        return "纤维结构能力"
    if any(keyword in name for keyword in ["抗氧化", "炎症缓冲", "维生素", "植物抗氧化"]):
        return "抗氧化结构能力"
    return "其他结构能力"


def render_formula_profile_dashboard():
    st.title("配方画像")
    st.caption("对库里产品做配方画像统计。画像判断优先使用 protein_source_aggregate、catfood_fat_material_features、catfood_fiber_feature_json 三张特征表。")

    with st.sidebar:
        st.header("配方画像参数")
        limit = st.number_input("统计产品上限", min_value=100, max_value=20000, value=5000, step=100)
        keyword = st.text_input("筛选品牌 / 产品 / SKU", "")
        brand_origin_filter = st.multiselect("品牌类型", ["进口品牌", "国产品牌", "未识别"], default=[])
        type_filter = st.multiselect(
            "能力类型",
            ["蛋白结构能力", "脂肪结构能力", "碳水结构能力", "纤维结构能力", "抗氧化结构能力", "其他结构能力"],
            default=[],
        )
        role_filter = st.multiselect("展示层级", ["主配方能力", "辅助配方能力", "风险短板", "补充配方能力"], default=[])

    product_df, capability_df = _formula_profile_dataset(int(limit))
    if product_df.empty:
        st.info("暂无可统计的产品。")
        return

    keyword = keyword.strip()
    if keyword:
        product_mask = (
            product_df["sku_id"].astype(str).str.contains(keyword, case=False, na=False)
            | product_df["品牌"].astype(str).str.contains(keyword, case=False, na=False)
            | product_df["产品名"].astype(str).str.contains(keyword, case=False, na=False)
        )
        product_df = product_df[product_mask].copy()
        if not capability_df.empty:
            capability_mask = (
                capability_df["sku_id"].astype(str).str.contains(keyword, case=False, na=False)
                | capability_df["品牌"].astype(str).str.contains(keyword, case=False, na=False)
                | capability_df["产品名"].astype(str).str.contains(keyword, case=False, na=False)
            )
            capability_df = capability_df[capability_mask].copy()

    if brand_origin_filter:
        product_df = product_df[product_df["品牌类型"].isin(brand_origin_filter)].copy()
        if not capability_df.empty:
            capability_df = capability_df[capability_df["品牌类型"].isin(brand_origin_filter)].copy()

    if role_filter and not capability_df.empty:
        capability_df = capability_df[capability_df["能力角色"].isin(role_filter)].copy()

    if type_filter and not capability_df.empty:
        capability_df = capability_df[capability_df["能力类型"].isin(type_filter)].copy()

    profiled_count = int((product_df["是否有画像"] == "是").sum()) if not product_df.empty else 0
    capability_count = int(capability_df["配方能力"].nunique()) if not capability_df.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("统计产品数", len(product_df))
    c2.metric("已有画像产品", profiled_count)
    c3.metric("画像覆盖率", f"{profiled_count / len(product_df) * 100:.1f}%" if len(product_df) else "0%")
    c4.metric("配方能力种类", capability_count)

    origin_summary = product_df.groupby("品牌类型", dropna=False).agg(产品数=("sku_id", "nunique")).reset_index()
    if not origin_summary.empty:
        st.dataframe(origin_summary, width="stretch", hide_index=True)

    tab_overview, tab_products, tab_raw = st.tabs(["画像统计", "产品画像明细", "能力明细"])
    with tab_overview:
        if capability_df.empty:
            st.info("当前筛选条件下暂无能力统计。")
        else:
            structure_capability_df = capability_df[
                (capability_df["能力角色"] != "风险短板")
                & (capability_df["能力类型"] != "风险短板")
            ].copy()
            structure_capability_df = structure_capability_df[
                structure_capability_df["能力角色"].isin(["主配方能力", "辅助配方能力"])
            ].copy()
            main_profile_df = structure_capability_df[
                structure_capability_df["能力角色"] == "主配方能力"
            ].copy()

            if structure_capability_df.empty:
                st.info("当前筛选条件下暂无结构能力统计。风险短板已从可视化图表中排除。")
            else:
                role_counts = structure_capability_df.groupby(["能力类型", "能力角色", "配方能力"], dropna=False).agg(
                    产品数=("sku_id", "nunique"),
                    平均能力分=("能力分", "mean"),
                ).reset_index().sort_values(["能力类型", "能力角色", "产品数"], ascending=[True, True, False])

                main_profile_counts = main_profile_df.groupby(["能力类型", "配方能力"], dropna=False).agg(
                    产品数=("sku_id", "nunique"),
                    平均能力分=("能力分", "mean"),
                ).reset_index().sort_values("产品数", ascending=False)
                main_profile_total = int(main_profile_counts["产品数"].sum()) if not main_profile_counts.empty else 0
                main_profile_counts["占比"] = main_profile_counts["产品数"].apply(
                    lambda value: (float(value) / main_profile_total * 100) if main_profile_total else 0.0
                )
                main_profile_counts["数量占比"] = main_profile_counts.apply(
                    lambda row: f"{int(row['产品数'])}（{float(row['占比']):.2f}%）",
                    axis=1,
                )

                coverage_type_summary = structure_capability_df.groupby("能力类型", dropna=False).agg(
                    产品数=("sku_id", "nunique"),
                    能力记录数=("配方能力", "count"),
                ).reset_index()
                coverage_type_summary["能力类型"] = coverage_type_summary["能力类型"].replace("", "未分组")

                main_type_summary = main_profile_df.groupby("能力类型", dropna=False).agg(
                    产品数=("sku_id", "nunique"),
                ).reset_index()
                main_type_summary["能力类型"] = main_type_summary["能力类型"].replace("", "未分组")
                main_type_total = int(main_type_summary["产品数"].sum()) if not main_type_summary.empty else 0
                main_type_summary["占比"] = main_type_summary["产品数"].apply(
                    lambda value: (float(value) / main_type_total * 100) if main_type_total else 0.0
                )

                top_capabilities = (
                    role_counts.groupby(["能力类型", "配方能力"], dropna=False)
                    .agg(产品数=("产品数", "sum"), 平均能力分=("平均能力分", "mean"))
                    .reset_index()
                    .sort_values("产品数", ascending=False)
                    .head(15)
                )

                st.markdown("### 主画像分布")
                st.caption("互斥口径：每个产品只计入 1 个主画像，合计等于当前筛选后的产品数。")
                col_main_type, col_main_profile = st.columns([1, 1.45])
                with col_main_type:
                    main_type_fig = px.pie(
                        main_type_summary,
                        names="能力类型",
                        values="产品数",
                        hole=0.48,
                        color_discrete_sequence=px.colors.qualitative.Set2,
                    )
                    main_type_fig.update_traces(
                        textposition="inside",
                        texttemplate="%{label}<br>%{percent:.2%}",
                    )
                    main_type_fig.update_layout(height=390, margin=dict(l=10, r=10, t=20, b=20), showlegend=False)
                    st.plotly_chart(main_type_fig, width="stretch")

                with col_main_profile:
                    main_profile_fig = px.bar(
                        main_profile_counts.head(15),
                        x="产品数",
                        y="配方能力",
                        orientation="h",
                        color="能力类型",
                        color_discrete_sequence=px.colors.qualitative.Set2,
                        text="数量占比",
                        hover_data=["能力类型", "平均能力分", "占比"],
                    )
                    main_profile_fig.update_yaxes(categoryorder="array", categoryarray=main_profile_counts["配方能力"].head(15).tolist()[::-1])
                    main_profile_fig.update_traces(textposition="outside", cliponaxis=False)
                    main_profile_fig.update_layout(
                        height=390,
                        xaxis_title="产品数",
                        yaxis_title="",
                        margin=dict(l=10, r=40, t=20, b=30),
                        legend_title_text="能力类型",
                    )
                    st.plotly_chart(main_profile_fig, width="stretch")

                st.markdown("### 结构能力覆盖")
                st.caption("覆盖口径：同一产品可命中多个结构能力，各标签覆盖产品数不能相加；风险短板不进入下列图表。")
                col_type, col_top = st.columns([1, 1.45])
                with col_type:
                    coverage_type_fig = px.pie(
                        coverage_type_summary,
                        names="能力类型",
                        values="能力记录数",
                        hole=0.48,
                        color_discrete_sequence=px.colors.qualitative.Set2,
                    )
                    coverage_type_fig.update_traces(textposition="inside", textinfo="percent+label")
                    coverage_type_fig.update_layout(height=420, margin=dict(l=10, r=10, t=20, b=20), showlegend=False)
                    st.plotly_chart(coverage_type_fig, width="stretch")

                with col_top:
                    st.markdown("#### Top 配方能力覆盖")
                    top_fig = px.bar(
                        top_capabilities,
                        x="产品数",
                        y="配方能力",
                        orientation="h",
                        color="能力类型",
                        color_discrete_sequence=px.colors.qualitative.Set2,
                        text="产品数",
                        hover_data=["能力类型", "平均能力分"],
                    )
                    top_fig.update_yaxes(categoryorder="array", categoryarray=top_capabilities["配方能力"].tolist()[::-1])
                    top_fig.update_traces(textposition="outside", cliponaxis=False)
                    top_fig.update_layout(
                        height=420,
                        xaxis_title="覆盖产品数",
                        yaxis_title="",
                        margin=dict(l=10, r=40, t=20, b=30),
                        legend_title_text="能力类型",
                    )
                    st.plotly_chart(top_fig, width="stretch")

                heatmap_df = structure_capability_df.pivot_table(
                    index="配方能力",
                    columns="能力等级",
                    values="sku_id",
                    aggfunc=pd.Series.nunique,
                    fill_value=0,
                )
                if not heatmap_df.empty:
                    heatmap_df["合计"] = heatmap_df.sum(axis=1)
                    heatmap_df = heatmap_df.sort_values("合计", ascending=False).head(20).drop(columns=["合计"])
                    st.markdown("### 结构能力等级热力图")
                    heatmap_fig = px.imshow(
                        heatmap_df,
                        text_auto=True,
                        aspect="auto",
                        color_continuous_scale="YlGnBu",
                        labels=dict(x="能力等级", y="配方能力", color="产品数"),
                    )
                    heatmap_fig.update_layout(height=520, margin=dict(l=10, r=10, t=20, b=30))
                    st.plotly_chart(heatmap_fig, width="stretch")

                with st.expander("查看统计明细表"):
                    st.dataframe(role_counts, width="stretch", hide_index=True)

    with tab_products:
        st.dataframe(product_df, width="stretch", hide_index=True)

    with tab_raw:
        if capability_df.empty:
            st.info("当前筛选条件下暂无能力明细。")
        else:
            st.dataframe(capability_df, width="stretch", hide_index=True)


def render_entry_home():
    st.title("接单工作台")
    st.caption("选择一个功能进入。")

    col_profile, col_order = st.columns(2)
    with col_profile:
        with st.container(border=True):
            st.subheader("配方画像")
            st.write("对库里所有产品进行配方能力统计、画像覆盖查看和产品画像明细浏览。")
            if st.button("进入配方画像", type="primary", width="stretch"):
                st.session_state["b2b_workspace_page"] = "formula_profile"
                st.rerun()

    with col_order:
        with st.container(border=True):
            st.subheader("产品相似度分析")
            st.write("按目标 SKU 或条件检索，输出产品相似度分析结果。")
            if st.button("进入产品相似度分析", type="primary", width="stretch"):
                st.session_state["b2b_workspace_page"] = "order_analysis"
                st.rerun()


def render_b2b_order_analysis():
    st.title("产品相似度分析")

    with st.sidebar:
        st.header("分析参数")
        mode = st.radio("分析入口", ["目标 SKU", "条件检索"], horizontal=True)
        top_n = st.slider("返回产品数量", 3, 50, 3, 1)

        selected_sku = ""
        recall_mode = "default"
        run_target_clicked = False
        run_filter_clicked = False
        filter_payload: Dict[str, Any] = {}

        if mode == "目标 SKU":
            options = load_all_sku_options()

            if options.empty:
                st.warning("没有匹配的 SKU。")
            else:
                labels = {
                    row["sku_id"]: f"{row['sku_id']} | {row.get('brand') or ''} {row.get('product_name') or ''}"
                    for _, row in options.iterrows()
                }
                selected_sku = st.selectbox(
                    "目标 SKU",
                    list(labels.keys()),
                    format_func=lambda sku_id: labels.get(sku_id, sku_id),
                )

            min_similarity = st.slider("最低相似度", 0.30, 0.95, 0.50, 0.05)
            st.selectbox("召回模式", ["营养结构相似项输出"], disabled=True)
            recall_mode = "nutrition_structure"
            run_target_clicked = st.button("运行 SKU 分析", type="primary", width="stretch")

        elif mode == "条件检索":
            filter_options = load_filter_options()

            st.subheader("工厂业务标签筛选区")
            factory_tags = st.multiselect("工厂业务标签", filter_options["factory_tags"])

            st.subheader("营养指标筛选区")
            protein_range = st.text_input("粗蛋白（min/max）%", placeholder="例如 35-48")
            fat_range = st.text_input("粗脂肪（min/max）%", placeholder="例如 14-22")
            fiber_range = st.text_input("粗纤维（min/max）%", placeholder="例如 1-6")

            protein_min, protein_max = parse_optional_range(protein_range)
            fat_min, fat_max = parse_optional_range(fat_range)
            fiber_min, fiber_max = parse_optional_range(fiber_range)

            filter_payload = {
                "required_factory_tags": factory_tags or None,
                "crude_protein_min": protein_min,
                "crude_protein_max": protein_max,
                "crude_fat_min": fat_min,
                "crude_fat_max": fat_max,
                "crude_fiber_min": fiber_min,
                "crude_fiber_max": fiber_max,
            }
            run_filter_clicked = st.button("运行条件分析", type="primary", width="stretch")

    if run_target_clicked and selected_sku:
        with st.spinner("正在分析并写入结果表..."):
            result = run_analysis(selected_sku, top_n, min_similarity, recall_mode)
        st.success(f"分析完成：{result['analysis_id']}")
        render_result(result)
        return

    if run_filter_clicked:
        with st.spinner("正在按筛选条件检索并写入结果表..."):
            result = run_filter_analysis(filter_payload, top_n)
        st.success(f"分析完成：{result['analysis_id']}")
        render_result(result)
        return

    if not selected_sku:
        with get_conn() as conn:
            default_sku = fetch_default_target_sku_id(conn)
        if default_sku:
            st.caption(f"当前默认可分析 SKU：{default_sku}")


def main():
    st.set_page_config(page_title="接单工作台", layout="wide")
    st.session_state.setdefault("b2b_workspace_page", "home")

    with st.sidebar:
        if st.session_state["b2b_workspace_page"] != "home":
            if st.button("返回功能入口", width="stretch"):
                st.session_state["b2b_workspace_page"] = "home"
                st.rerun()

    if st.session_state["b2b_workspace_page"] == "formula_profile":
        render_formula_profile_dashboard()
    elif st.session_state["b2b_workspace_page"] == "order_analysis":
        render_b2b_order_analysis()
    else:
        render_entry_home()


if __name__ == "__main__":
    main()
