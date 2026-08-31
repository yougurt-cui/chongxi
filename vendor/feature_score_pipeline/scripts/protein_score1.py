# -*- coding: utf-8 -*-
from __future__ import print_function

import os
import json
import re
import sys
from pathlib import Path

if sys.version_info[0] < 3:
    raise RuntimeError("请使用 Python 3 运行：python3 protein_score1.py")

import numpy as np
import pandas as pd
from sqlalchemy import Numeric, create_engine, text
from sqlalchemy.engine import URL


APP_DIR = Path(__file__).resolve().parents[3]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

try:
    from app_config import get_feature_mysql_config
except ImportError:
    get_feature_mysql_config = None


# =========================
# 1. 数据库连接
# =========================
DB_USER = os.getenv("MYSQL_USER", "root")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DB_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
DB_PORT = os.getenv("MYSQL_PORT", "3306")
DB_NAME = os.getenv("MYSQL_DATABASE", "protein_feature_platform")

SOURCE_TABLE = os.getenv(
    "PROTEIN_SCORE_SOURCE_TABLE",
    "protein_source_aggregate",
)
OUTPUT_TABLE = os.getenv(
    "PROTEIN_SCORE_OUTPUT_TABLE",
    "protein_business_cluster_product_details_scored",
)
OUTPUT_IF_EXISTS = os.getenv("PROTEIN_SCORE_IF_EXISTS", "replace")
SCORE_CONFIG_DB = os.getenv("PROTEIN_SCORE_CONFIG_DB", "csv_labeling")
SCORE_CONFIG_VERSION = os.getenv("PROTEIN_SCORE_CONFIG_VERSION", "protein_v1")

VALID_IF_EXISTS = {"fail", "replace", "append"}
SCORE_DECIMALS = 3

PROTEIN_STRUCTURE_COMPONENT_SCORE_COLS = [
    "meat_source_complexity_score",
    "main_protein_form_score",
    "secondary_protein_form_score",
    "form_mix_complexity_score",
    "hydrolyzed_protein_load_score",
    "plant_protein_interference_score",
    "protein_content_score",
]

SCORED_OUTPUT_COLUMNS = [
    "formula_id",
    "source_id",
    "product_key",
    "brand_name",
    "product_name",
    "meat_source_complexity_score",
    "main_protein_form_score",
    "secondary_protein_form_score",
    "form_mix_complexity_score",
    "hydrolyzed_protein_relief_score",
    "plant_protein_interference_norm",
    "plant_protein_interference_score",
    "protein_content_score",
    "protein_structure_score",
    "protein_quality_score",
]


# =========================
# 2. 分值映射规则
# =========================

# 2.1 肉源复杂度分
# 越高表示肉源越复杂、结构越重
MEAT_SOURCE_COMPLEXITY_LEVELS = {
    "单一肉源": 1, "单一来源": 1, "同类双源": 2,
    "同类多源": 3, "跨类双源": 4, "跨类多源": 5,
}
MEAT_SOURCE_COMPLEXITY_LEVEL_SCORES = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0, 5: 5.0}
MEAT_SOURCE_COMPLEXITY_MAP = {
    label: MEAT_SOURCE_COMPLEXITY_LEVEL_SCORES[level]
    for label, level in MEAT_SOURCE_COMPLEXITY_LEVELS.items()
}

COMPLEXITY_LEVEL_SCORE_MAP = {
    "1": 1.0,
    "2": 2.0,
    "3": 3.0,
    "4": 4.0,
    "5": 5.0,
}

# 2.2 主要蛋白形式分
# 越高表示形式越偏“重结构”
MAIN_PROTEIN_FORM_LEVELS = {
    "水解蛋白为主": 1, "鲜肉为主": 2, "鲜肉/冻肉为主": 3,
    "冻肉为主": 4, "肉粉为主": 5,
}
MAIN_PROTEIN_FORM_LEVEL_SCORES = {1: 0.5, 2: 1.0, 3: 1.25, 4: 1.5, 5: 2.0}
MAIN_PROTEIN_FORM_MAP = {
    label: MAIN_PROTEIN_FORM_LEVEL_SCORES[level]
    for label, level in MAIN_PROTEIN_FORM_LEVELS.items()
}

# 2.3 次要蛋白形式分
# 水解蛋白不再抬高总分，保持低值
SECONDARY_PROTEIN_FORM_LEVELS = {
    "无": 1, "水解蛋白": 1, "鲜肉": 2, "冻肉": 2,
    "鲜肉/冻肉": 2, "肉粉": 3,
}
SECONDARY_PROTEIN_FORM_LEVEL_SCORES = {1: 0.0, 2: 0.5, 3: 1.0}
SECONDARY_PROTEIN_FORM_MAP = {
    label: SECONDARY_PROTEIN_FORM_LEVEL_SCORES[level]
    for label, level in SECONDARY_PROTEIN_FORM_LEVELS.items()
}

# 2.4 植物蛋白干扰分
PLANT_PROTEIN_INTERFERENCE_LEVELS = {
    "无植物蛋白": 1, "1级｜单一温和型植物蛋白": 2,
    "2级｜单一高浓缩型植物蛋白": 3, "3级｜多源植物蛋白补强型": 4,
    "4级｜豆类主导混合型植物蛋白": 5,
}
PLANT_PROTEIN_INTERFERENCE_LEVEL_SCORES = {1: 0.0, 2: 0.25, 3: 0.5, 4: 0.75, 5: 1.0}
PLANT_PROTEIN_INTERFERENCE_MAP = {
    label: PLANT_PROTEIN_INTERFERENCE_LEVEL_SCORES[level]
    for label, level in PLANT_PROTEIN_INTERFERENCE_LEVELS.items()
}

CONCENTRATED_PLANT_PROTEIN_TERMS = [
    "玉米蛋白粉",
    "大米蛋白粉",
    "豌豆蛋白",
    "马铃薯蛋白",
    "小麦蛋白",
    "谷朊粉",
    "豆粕",
    "大豆蛋白",
]

# 2.5 水解蛋白缓释分（减分项）
# 越高表示越温和，对“配方负载分”有下拉作用
HYDROLYZED_PROTEIN_RELIEF_LEVELS = {"无": 1, "次要出现": 2, "主要出现": 3}
HYDROLYZED_PROTEIN_RELIEF_LEVEL_SCORES = {1: 0.0, 2: 0.5, 3: 1.0}
HYDROLYZED_PROTEIN_RELIEF_MAP = {
    label: HYDROLYZED_PROTEIN_RELIEF_LEVEL_SCORES[level]
    for label, level in HYDROLYZED_PROTEIN_RELIEF_LEVELS.items()
}

PROTEIN_QUALITY_WEIGHTS = {
    "animal_protein_dominance": 0.30,
    "source_clarity": 0.25,
    "digestibility": 0.20,
    "low_plant_interference": 0.15,
    "protein_content_suitability": 0.10,
}

PROTEIN_PRESSURE_WEIGHTS = {
    component: 1.0 / len(PROTEIN_STRUCTURE_COMPONENT_SCORE_COLS)
    for component in PROTEIN_STRUCTURE_COMPONENT_SCORE_COLS
}

# Fixed domains make single-formula incremental scoring comparable with batch runs.
# Values outside a domain are clipped instead of changing every other product's score.
PROTEIN_COMPONENT_RANGES = {
    "meat_source_complexity_score": (0.0, 5.0),
    "main_protein_form_score": (0.0, 2.0),
    "secondary_protein_form_score": (0.0, 1.0),
    "form_mix_complexity_score": (0.0, 3.0),
    "hydrolyzed_protein_load_score": (0.0, 1.0),
    "plant_protein_interference_score": (0.0, 1.0),
    "protein_content_score": (0.0, 1.0),
}
CRUDE_PROTEIN_RANGE = (20.0, 60.0)

DEFAULT_LABEL_SCORE_MAPS = {
    "meat_source_complexity": MEAT_SOURCE_COMPLEXITY_MAP,
    "main_protein_form": MAIN_PROTEIN_FORM_MAP,
    "secondary_protein_form": SECONDARY_PROTEIN_FORM_MAP,
    "plant_protein_interference": PLANT_PROTEIN_INTERFERENCE_MAP,
    "hydrolyzed_protein_role": HYDROLYZED_PROTEIN_RELIEF_MAP,
}


# =========================
# 3. 工具函数
# =========================
def quote_identifier(name):
    """只允许普通表名，避免把表名直接拼进 SQL 时产生意外。"""
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        raise ValueError("非法表名：{}".format(name))
    return "`{}`".format(name)


def get_engine():
    central = get_feature_mysql_config() if get_feature_mysql_config else {}
    url = URL.create(
        "mysql+pymysql",
        username=os.getenv("MYSQL_USER") or central.get("user") or DB_USER,
        password=os.getenv("MYSQL_PASSWORD") or central.get("password") or DB_PASSWORD,
        host=os.getenv("MYSQL_HOST") or central.get("host") or DB_HOST,
        port=int(os.getenv("MYSQL_PORT") or central.get("port") or DB_PORT),
        database=os.getenv("MYSQL_DATABASE") or central.get("database") or DB_NAME,
        query={"charset": central.get("charset") or "utf8mb4"},
    )
    return create_engine(url, pool_pre_ping=True, future=True)


def normalize_text(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def first_non_empty(row, columns):
    for col in columns:
        if col in row:
            value = normalize_text(row.get(col))
            if value:
                return value
    return ""


def split_form_tokens(value):
    s = normalize_text(value)
    if not s:
        return set()

    s = (
        s.replace("｜", "|")
        .replace("＋", "+")
        .replace("、", "|")
        .replace("，", "|")
        .replace(",", "|")
    )
    parts = re.split(r"[|+/]", s)
    return {part.strip() for part in parts if part.strip()}


def map_score(value, score_map, default=np.nan):
    s = normalize_text(value)
    if not s:
        return default
    return score_map.get(s, default)


def infer_complexity_label(row):
    s = first_non_empty(
        row,
        [
            "meat_source_complexity",
            "source_complexity_label",
            "complexity_level",
        ],
    )

    if not s:
        return ""

    match = re.match(r"(\d+)", s)
    if match:
        level = match.group(1)
        label_by_level = {
            "1": "单一来源",
            "2": "同类双源",
            "3": "同类多源",
            "4": "跨类双源",
            "5": "跨类多源",
        }
        return label_by_level.get(level, s)

    for label in MEAT_SOURCE_COMPLEXITY_MAP:
        if label in s:
            return label

    return s


def calc_complexity_score(value):
    s = normalize_text(value)
    if not s:
        return np.nan

    match = re.match(r"(\d+)", s)
    if match:
        return COMPLEXITY_LEVEL_SCORE_MAP.get(match.group(1), np.nan)

    for label, score in MEAT_SOURCE_COMPLEXITY_MAP.items():
        if label in s:
            return score

    return np.nan


def infer_main_protein_form(row):
    raw = first_non_empty(
        row,
        [
            "main_protein_form",
            "business_meat_direction",
            "meat_form_std",
            "primary_meat_source_type",
        ],
    )
    if not raw:
        return ""

    tokens = split_form_tokens(raw)

    if "肉粉" in raw or "肉粉" in tokens:
        return "肉粉为主"
    if "水解" in raw:
        return "水解蛋白为主"
    if "鲜肉/冻肉" in raw or ({"鲜肉", "冻肉"} <= tokens):
        return "鲜肉/冻肉为主"
    if "冻肉" in raw or "冻肉" in tokens:
        return "冻肉为主"
    if "鲜肉" in raw or "鲜肉" in tokens:
        return "鲜肉为主"

    return raw


def infer_secondary_protein_form(row):
    raw = first_non_empty(
        row,
        [
            "secondary_protein_form",
            "secondary_meat_source_type",
        ],
    )
    if not raw:
        return "无"

    tokens = split_form_tokens(raw)

    if "水解" in raw:
        return "水解蛋白"
    if "肉粉" in raw or "肉粉" in tokens:
        return "肉粉"
    if {"鲜肉", "冻肉"} <= tokens:
        return "鲜肉/冻肉"
    if "冻肉" in raw or "冻肉" in tokens:
        return "冻肉"
    if "鲜肉" in raw or "鲜肉" in tokens:
        return "鲜肉"

    return "无"


def calc_main_protein_form_score(row, score_map=None):
    score_map = score_map or MAIN_PROTEIN_FORM_MAP
    raw = normalize_text(row.get("primary_meat_source_type", ""))
    shares = row.get("protein_form_contribution_shares") or {}
    if isinstance(shares, str):
        try:
            shares = json.loads(shares)
        except (TypeError, ValueError, json.JSONDecodeError):
            shares = {}
    main_forms = [token for token in re.split(r"[/、|+]", raw) if token]
    if isinstance(shares, dict) and main_forms:
        weighted = []
        label_by_form = {
            "水解蛋白": "水解蛋白为主", "鲜肉": "鲜肉为主",
            "冻肉": "冻肉为主", "肉粉": "肉粉为主",
        }
        for form in main_forms:
            weight = parse_number(shares.get(form))
            label = label_by_form.get(form)
            if label and not pd.isna(weight) and weight > 0:
                weighted.append((float(weight), map_score(label, score_map, default=0.0)))
        total = sum(weight for weight, _ in weighted)
        if total > 0:
            return sum(weight * score for weight, score in weighted) / total
    normalized = infer_main_protein_form(row)
    return map_score(normalized, score_map, default=np.nan)


def collect_protein_forms(row):
    forms = set()
    fields = [
        "main_protein_form",
        "main_protein_form_norm",
        "primary_meat_source_type",
        "meat_form_std",
        "secondary_protein_form",
        "secondary_protein_form_norm",
        "secondary_meat_source_type",
    ]

    for field in fields:
        value = normalize_text(row.get(field, ""))
        if not value:
            continue
        if "鲜肉" in value:
            forms.add("鲜肉")
        if "冻肉" in value:
            forms.add("冻肉")
        if "肉粉" in value:
            forms.add("肉粉")
        if "水解" in value:
            forms.add("水解蛋白")

    return forms


def calc_form_mix_complexity(row):
    """
    形式混合复杂度分：
    - 1 种 = 1
    - 2 种 = 2
    - 3 种及以上 = 3
    """
    n = len(collect_protein_forms(row))

    if n <= 1:
        return 1.0
    if n == 2:
        return 2.0
    return 3.0


def calc_hydrolyzed_protein_relief_score(row, score_map=None):
    """
    水解蛋白缓释分：作为减分项
    """
    role = normalize_text(row.get("hydrolyzed_protein_role", ""))
    if role:
        return map_score(role, score_map or HYDROLYZED_PROTEIN_RELIEF_MAP, default=0.0)

    primary = normalize_text(row.get("primary_meat_source_type", ""))
    main_form = normalize_text(row.get("main_protein_form", ""))
    main_norm = normalize_text(row.get("main_protein_form_norm", ""))
    secondary = normalize_text(row.get("secondary_meat_source_type", ""))
    secondary_form = normalize_text(row.get("secondary_protein_form", ""))
    secondary_norm = normalize_text(row.get("secondary_protein_form_norm", ""))

    if "水解" in primary or "水解" in main_form or "水解" in main_norm:
        return 1.0
    if "水解" in secondary or "水解" in secondary_form or "水解" in secondary_norm:
        return 0.5
    return 0.0


def infer_plant_protein_interference(row):
    explicit = normalize_text(row.get("plant_protein_interference", ""))
    if explicit:
        return explicit

    binary = row.get("plant_protein_binary", None)
    if binary is True or binary == 1 or normalize_text(binary).lower() == "true":
        return "有植物蛋白"
    if binary is False or binary == 0 or normalize_text(binary).lower() == "false":
        return "无植物蛋白"

    label = normalize_text(row.get("plant_protein_label", ""))
    labels = normalize_text(row.get("plant_protein_labels", ""))
    if not label and labels:
        tokens = [token for token in re.split(r"[、,，;；/\\s]+", labels) if token]
        concentrated_count = sum(
            1
            for token in tokens
            if any(term in token for term in CONCENTRATED_PLANT_PROTEIN_TERMS)
        )
        if concentrated_count >= 2:
            return "3级｜多源植物蛋白补强型"
        if concentrated_count == 1:
            return "2级｜单一高浓缩型植物蛋白"
        if tokens:
            return "1级｜单一温和型植物蛋白"

    if not label or "无植物蛋白" in label:
        return "无植物蛋白"
    return label


def parse_number(value):
    if value is None or pd.isna(value):
        return np.nan
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)

    match = re.search(r"(\d+(?:\.\d+)?)", str(value))
    if match:
        return float(match.group(1))
    return np.nan


def fixed_range_scale(series, lower, upper):
    """
    Normalize against a stable business domain, independent of batch contents.
    """
    s = series.apply(parse_number)
    width = float(upper) - float(lower)
    if width <= 0:
        raise ValueError("固定评分区间上限必须大于下限")
    return ((s.fillna(lower) - float(lower)) / width).clip(0.0, 1.0)


def normalize_score_series(series, lower=0.0, upper=1.0):
    return fixed_range_scale(series, lower, upper)


def clamp01(value):
    if pd.isna(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def calc_animal_protein_dominance_score(row):
    """Score animal-source presence and specificity independently.

    Plant-protein interference is intentionally excluded here.  It already has
    its own quality component (low_plant_interference), so subtracting it here
    would count the same position-weighted signal twice.
    """
    explicit = parse_number(row.get("animal_protein_dominance_score"))
    if not pd.isna(explicit):
        return clamp01(explicit)

    level1 = normalize_text(row.get("animal_source_level1_categories", ""))
    level2 = normalize_text(row.get("animal_source_level2_sources", ""))
    animal_sources = normalize_text(row.get("animal_sources", ""))
    text_pool = " ".join([level1, level2, animal_sources])

    if not text_pool:
        return 0.0

    score = 0.65
    if level2:
        score += 0.20
    if "蛋类" in level1 or "鸡蛋" in text_pool or "鸭蛋" in text_pool:
        score += 0.10
    if "鱼类" in level1 or "鱼" in text_pool:
        score += 0.05

    return clamp01(score)


def calc_source_clarity_score(row):
    fields = [
        "animal_source_level2_sources",
        "primary_meat_source_species",
        "secondary_meat_source_species",
        "primary_meat_source_type",
        "secondary_meat_source_type",
        "protein_source_details",
    ]
    values = [normalize_text(row.get(field, "")) for field in fields]
    filled_count = sum(1 for value in values if value)

    if filled_count == 0:
        return 0.0

    score = min(1.0, 0.35 + 0.13 * filled_count)
    text_pool = " ".join(values)
    vague_terms = ["动物蛋白", "肉类及其制品", "禽类", "鱼类", "混合肉源", "未明确"]
    if any(term in text_pool for term in vague_terms):
        score -= 0.20
    if any(term in text_pool for term in ["鸡", "鸭", "火鸡", "三文鱼", "鳕鱼", "牛", "羊", "兔", "蛋"]):
        score += 0.15
    return clamp01(score)


def calc_digestibility_score(row):
    main_form = normalize_text(row.get("main_protein_form_norm", ""))
    secondary_form = normalize_text(row.get("secondary_protein_form_norm", ""))
    text_pool = " ".join(
        [
            main_form,
            secondary_form,
            normalize_text(row.get("protein_source_details", "")),
            normalize_text(row.get("animal_source_level2_sources", "")),
        ]
    )

    if "水解" in text_pool:
        score = 0.95
    elif "鲜肉/冻肉" in main_form:
        score = 0.90
    elif "鲜肉" in main_form:
        score = 0.92
    elif "冻肉" in main_form:
        score = 0.85
    elif "肉粉" in main_form:
        score = 0.72
    elif main_form:
        score = 0.55
    else:
        score = 0.35

    if "蛋" in text_pool:
        score += 0.08
    if "肉粉" in secondary_form:
        score -= 0.05
    return clamp01(score)


def calc_protein_content_suitability_score(value):
    protein_pct = parse_number(value)
    if pd.isna(protein_pct):
        return 0.5
    if protein_pct <= 1:
        protein_pct *= 100

    if protein_pct < 25:
        return clamp01(0.50 * protein_pct / 25)
    if protein_pct <= 45:
        return clamp01(0.75 + 0.25 * (protein_pct - 25) / 20)
    if protein_pct <= 55:
        return clamp01(1.00 - 0.15 * (protein_pct - 45) / 10)
    return clamp01(0.85 - 0.35 * (protein_pct - 55) / 25)


def calc_protein_quality_score(row, weights=None):
    weights = weights or PROTEIN_QUALITY_WEIGHTS
    animal_score = calc_animal_protein_dominance_score(row)
    clarity_score = calc_source_clarity_score(row)
    digestibility_score = calc_digestibility_score(row)
    low_plant_interference_score = 1.0 - clamp01(
        row.get("plant_protein_interference_score", 0.0)
    )
    content_suitability_score = calc_protein_content_suitability_score(
        row.get("crude_protein_for_score")
    )

    components = {
        "animal_protein_dominance": animal_score,
        "source_clarity": clarity_score,
        "digestibility": digestibility_score,
        "low_plant_interference": low_plant_interference_score,
        "protein_content_suitability": content_suitability_score,
    }
    return sum(float(weights[key]) * value for key, value in components.items())


def load_score_config(engine):
    """Load one active version of label score maps and quality weights."""
    config_db = quote_identifier(SCORE_CONFIG_DB)
    params = {"config_version": SCORE_CONFIG_VERSION}
    label_sql = """
        SELECT l.dimension_code, l.value_name, s.score_value
        FROM {db}.catfood_label_score_config s
        JOIN {db}.catfood_feature_label l ON l.label_id=s.label_id
        WHERE s.active=1 AND l.active=1
          AND s.config_version=:config_version
          AND l.config_version=:config_version
          AND s.score_code='protein_structure_score'
    """.format(db=config_db)
    weight_sql = """
        SELECT score_code, component_code, weight
        FROM {db}.catfood_score_weight_config
        WHERE active=1 AND config_version=:config_version
          AND score_code IN ('protein_quality_score', 'protein_structure_score')
    """.format(db=config_db)
    try:
        label_rows = pd.read_sql(text(label_sql), engine, params=params)
        weight_rows = pd.read_sql(text(weight_sql), engine, params=params)
    except Exception as exc:
        print("评分配置读取失败，使用代码默认值：{}".format(exc))
        return (
            dict(DEFAULT_LABEL_SCORE_MAPS),
            dict(PROTEIN_QUALITY_WEIGHTS),
            dict(PROTEIN_PRESSURE_WEIGHTS),
        )

    label_maps = {key: dict(value) for key, value in DEFAULT_LABEL_SCORE_MAPS.items()}
    for row in label_rows.to_dict("records"):
        label_maps.setdefault(row["dimension_code"], {})[row["value_name"]] = float(row["score_value"])

    # Generic editable configuration takes precedence over legacy protein tables.
    enum_sql = """
        SELECT dimension_code, enum_value, score_value
        FROM {db}.catfood_score_enum_config
        WHERE active=1 AND config_version='v1'
          AND domain_code='protein' AND score_value IS NOT NULL
    """.format(db=config_db)
    try:
        enum_rows = pd.read_sql(text(enum_sql), engine, params=params)
        for row in enum_rows.to_dict("records"):
            label_maps.setdefault(row["dimension_code"], {})[row["enum_value"]] = float(row["score_value"])
    except Exception as exc:
        print("通用枚举评分配置读取失败，沿用蛋白默认配置：{}".format(exc))

    weight_records = weight_rows.to_dict("records")
    weights = {
        row["component_code"]: float(row["weight"])
        for row in weight_records if row["score_code"] == "protein_quality_score"
    }
    expected_components = set(PROTEIN_QUALITY_WEIGHTS)
    if set(weights) != expected_components:
        raise ValueError("蛋白质量权重组件不完整：expected={} actual={}".format(sorted(expected_components), sorted(weights)))
    if abs(sum(weights.values()) - 1.0) > 0.000001:
        raise ValueError("蛋白质量权重合计必须为1，当前为{}".format(sum(weights.values())))
    pressure_weights = {
        row["component_code"]: float(row["weight"])
        for row in weight_records if row["score_code"] == "protein_structure_score"
    }
    expected_pressure = set(PROTEIN_STRUCTURE_COMPONENT_SCORE_COLS)
    if set(pressure_weights) != expected_pressure:
        raise ValueError("蛋白压力权重组件不完整：expected={} actual={}".format(
            sorted(expected_pressure), sorted(pressure_weights)
        ))
    if abs(sum(pressure_weights.values()) - 1.0) > 0.000001:
        raise ValueError("蛋白压力权重合计必须为1，当前为{}".format(sum(pressure_weights.values())))
    return label_maps, weights, pressure_weights


def choose_crude_protein_series(df):
    candidates = [
        "crude_protein",
        "guarantee_crude_protein_num",
        "guarantee_crude_protein_value",
    ]
    for col in candidates:
        if col in df.columns:
            return col, df[col]
    raise ValueError("缺少蛋白含量字段，候选字段：{}".format(candidates))


def validate_config():
    if OUTPUT_IF_EXISTS not in VALID_IF_EXISTS:
        raise ValueError(
            "PROTEIN_SCORE_IF_EXISTS 只能是 {}，当前是 {}".format(
                sorted(VALID_IF_EXISTS),
                OUTPUT_IF_EXISTS,
            )
        )


def round_score_columns(df):
    """
    对所有数值型得分字段统一保留 3 位小数
    """
    rounded_df = df.copy()

    for col in rounded_df.columns:
        if not is_score_value_column(col):
            continue

        series = rounded_df[col]
        if is_numeric_score_series(series):
            rounded_df[col] = series.round(SCORE_DECIMALS)
            continue

        numeric_series = pd.to_numeric(series, errors="coerce")
        non_null_count = series.notna().sum()
        numeric_count = numeric_series.notna().sum()

        if non_null_count > 0 and numeric_count == non_null_count:
            rounded_df[col] = numeric_series.round(SCORE_DECIMALS)

    return rounded_df


def is_score_value_column(col):
    lower_col = col.lower()
    if "score" not in lower_col:
        return False
    if "scored_at" in lower_col:
        return False
    if lower_col.endswith("_id"):
        return False
    return True


def is_numeric_score_series(series):
    if pd.api.types.is_bool_dtype(series):
        return False
    if pd.api.types.is_datetime64_any_dtype(series):
        return False
    if pd.api.types.is_timedelta64_dtype(series):
        return False
    return pd.api.types.is_numeric_dtype(series)


def score_column_sql_types(df):
    return {
        col: Numeric(12, SCORE_DECIMALS)
        for col in df.columns
        if is_score_value_column(col) and is_numeric_score_series(df[col])
    }


def ensure_output_table_metadata(engine):
    with engine.begin() as conn:
        columns = conn.execute(
            text(
                """
                SELECT COLUMN_NAME, COLUMN_KEY, EXTRA
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                """
            ),
            {"schema_name": DB_NAME, "table_name": OUTPUT_TABLE},
        ).mappings().all()

        column_map = {row["COLUMN_NAME"]: row for row in columns}
        id_info = column_map.get("id")

        if id_info is None:
            conn.execute(
                text(
                    "ALTER TABLE {} ADD COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST".format(
                        quote_identifier(OUTPUT_TABLE)
                    )
                )
            )
        elif id_info["COLUMN_KEY"] != "PRI" or "auto_increment" not in id_info["EXTRA"]:
            conn.execute(
                text(
                    "ALTER TABLE {} MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY".format(
                        quote_identifier(OUTPUT_TABLE)
                    )
                )
            )

        if "created_at" not in column_map:
            conn.execute(
                text(
                    "ALTER TABLE {} ADD COLUMN created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP".format(
                        quote_identifier(OUTPUT_TABLE)
                    )
                )
            )
        else:
            conn.execute(
                text(
                    "ALTER TABLE {} MODIFY COLUMN created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP".format(
                        quote_identifier(OUTPUT_TABLE)
                    )
                )
            )

        formula_id_index = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                  AND INDEX_NAME = 'uq_formula_id'
                """
            ),
            {"schema_name": DB_NAME, "table_name": OUTPUT_TABLE},
        ).scalar()
        if not formula_id_index and "formula_id" in column_map:
            conn.execute(
                text(
                    "ALTER TABLE {} ADD UNIQUE KEY uq_formula_id (formula_id)".format(
                        quote_identifier(OUTPUT_TABLE)
                    )
                )
            )


def table_exists(engine, table_name):
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
                {"schema_name": DB_NAME, "table_name": table_name},
            ).scalar()
        )


def upsert_scored_dataframe(engine, scored_df):
    if OUTPUT_IF_EXISTS in {"replace", "fail"} or not table_exists(engine, OUTPUT_TABLE):
        with engine.begin() as conn:
            scored_df.to_sql(
                OUTPUT_TABLE,
                conn,
                if_exists=OUTPUT_IF_EXISTS if table_exists(engine, OUTPUT_TABLE) else "replace",
                index=False,
                dtype=score_column_sql_types(scored_df),
            )
        ensure_output_table_metadata(engine)
        return

    ensure_output_table_metadata(engine)
    temp_table = "__tmp_{}_{}".format(OUTPUT_TABLE, os.getpid())
    quoted_temp = quote_identifier(temp_table)
    quoted_output = quote_identifier(OUTPUT_TABLE)
    columns = list(scored_df.columns)
    insert_columns = ", ".join(quote_identifier(col) for col in columns)
    select_columns = ", ".join(quote_identifier(col) for col in columns)
    update_columns = [col for col in columns if col != "formula_id"]
    update_sql = ", ".join(
        "{0}=VALUES({0})".format(quote_identifier(col)) for col in update_columns
    )

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS {}".format(quoted_temp)))
        scored_df.to_sql(
            temp_table,
            conn,
            if_exists="replace",
            index=False,
            dtype=score_column_sql_types(scored_df),
        )
        conn.execute(
            text(
                """
                INSERT INTO {output} ({insert_columns})
                SELECT {select_columns}
                FROM {temp}
                ON DUPLICATE KEY UPDATE {update_sql}
                """.format(
                    output=quoted_output,
                    insert_columns=insert_columns,
                    select_columns=select_columns,
                    temp=quoted_temp,
                    update_sql=update_sql,
                )
            )
        )
        conn.execute(text("DROP TABLE IF EXISTS {}".format(quoted_temp)))


def add_score_columns(
    df,
    label_score_maps=None,
    quality_weights=None,
    pressure_weights=None,
):
    if df.empty:
        return df.copy()

    scored_df = df.copy()
    label_score_maps = label_score_maps or DEFAULT_LABEL_SCORE_MAPS
    quality_weights = quality_weights or PROTEIN_QUALITY_WEIGHTS
    pressure_weights = pressure_weights or PROTEIN_PRESSURE_WEIGHTS

    scored_df["meat_source_complexity_norm"] = scored_df.apply(
        infer_complexity_label,
        axis=1,
    )
    scored_df["main_protein_form_norm"] = scored_df.apply(
        infer_main_protein_form,
        axis=1,
    )
    scored_df["secondary_protein_form_norm"] = scored_df.apply(
        infer_secondary_protein_form,
        axis=1,
    )
    scored_df["plant_protein_interference_norm"] = scored_df.apply(
        infer_plant_protein_interference,
        axis=1,
    )

    crude_col, crude_series = choose_crude_protein_series(scored_df)
    scored_df["crude_protein_for_score"] = crude_series.apply(parse_number)

    scored_df["meat_source_complexity_score"] = scored_df[
        "meat_source_complexity_norm"
    ].apply(lambda x: map_score(x, label_score_maps["meat_source_complexity"], default=np.nan))

    scored_df["main_protein_form_score"] = scored_df.apply(
        lambda row: calc_main_protein_form_score(row, label_score_maps["main_protein_form"]), axis=1
    )

    scored_df["secondary_protein_form_score"] = scored_df[
        "secondary_protein_form_norm"
    ].apply(lambda x: map_score(x, label_score_maps["secondary_protein_form"], default=0.0))

    scored_df["form_mix_complexity_score"] = scored_df.apply(
        calc_form_mix_complexity,
        axis=1,
    )

    scored_df["hydrolyzed_protein_relief_score"] = scored_df.apply(
        lambda row: calc_hydrolyzed_protein_relief_score(
            row, label_score_maps["hydrolyzed_protein_role"]
        ), axis=1,
    )

    scored_df["plant_protein_interference_score"] = scored_df[
        "plant_protein_interference_norm"
    ].apply(lambda x: map_score(x, label_score_maps["plant_protein_interference"], default=np.nan))

    scored_df["protein_content_score"] = fixed_range_scale(
        scored_df[crude_col], *CRUDE_PROTEIN_RANGE
    )

    # Ingredient-derived components use the same database-configured position
    # weights as fat, fiber, carbohydrate, prebiotic and probiotic ingredients.
    position_columns = {
        "meat_source_complexity_score": "protein_position_weight",
        "main_protein_form_score": "main_protein_position_weight",
        "secondary_protein_form_score": "secondary_protein_position_weight",
        "form_mix_complexity_score": "protein_position_weight",
        "plant_protein_interference_score": "plant_protein_position_weight",
    }
    for score_column, weight_column in position_columns.items():
        if weight_column in scored_df.columns:
            weights = pd.to_numeric(scored_df[weight_column], errors="coerce").fillna(0.5)
            scored_df[score_column] = scored_df[score_column] * weights

    score_cols = [
        "meat_source_complexity_score",
        "main_protein_form_score",
        "secondary_protein_form_score",
        "form_mix_complexity_score",
        "hydrolyzed_protein_relief_score",
        "plant_protein_interference_score",
        "protein_content_score",
    ]

    for col in score_cols:
        scored_df[col] = scored_df[col].fillna(0.0)

    # 水解蛋白原本是减分项；转成同向的“负载分”后再与其他分子等权平均。
    scored_df["hydrolyzed_protein_load_score"] = (
        1.0 - scored_df["hydrolyzed_protein_relief_score"]
    ).clip(0.0, 1.0)

    for col in PROTEIN_STRUCTURE_COMPONENT_SCORE_COLS:
        scored_df[col + "_normalized"] = normalize_score_series(
            scored_df[col], *PROTEIN_COMPONENT_RANGES[col]
        )

    # =========================
    # 4. 蛋白结构强度分 / 配方负载分
    # 分越高 = 越复杂、越重、越偏重配方
    # 分越低 = 越温和、越简单
    # =========================
    scored_df["protein_structure_score"] = sum(
        scored_df[col + "_normalized"] * float(pressure_weights[col])
        for col in PROTEIN_STRUCTURE_COMPONENT_SCORE_COLS
    )

    scored_df["protein_structure_score_method"] = (
        "component_fixed_range_normalized_weighted"
    )

    scored_df["protein_structure_scored_at"] = pd.Timestamp.now()
    scored_df["protein_quality_score"] = scored_df.apply(
        lambda row: calc_protein_quality_score(row, quality_weights), axis=1,
    )

    output_columns = [col for col in SCORED_OUTPUT_COLUMNS if col in scored_df.columns]
    return round_score_columns(scored_df[output_columns].copy())


def main():
    validate_config()

    engine = get_engine()
    source_table = quote_identifier(SOURCE_TABLE)
    quote_identifier(OUTPUT_TABLE)

    df = pd.read_sql("SELECT * FROM {}".format(source_table), engine)
    formula_id = os.getenv("FORMULA_ID")
    if formula_id:
        df = df[pd.to_numeric(df["formula_id"], errors="coerce") == int(formula_id)].copy()
    if (
        "protein_profile_status" in df.columns
        and os.getenv("PROTEIN_SCORE_INCLUDE_BLOCKED", "0").strip().lower()
        not in {"1", "true", "yes"}
    ):
        blocked_count = int((df["protein_profile_status"] != "ready").sum())
        if blocked_count:
            print("跳过蛋白画像未就绪记录：{}".format(blocked_count))
        df = df[df["protein_profile_status"] == "ready"].copy()
    if df.empty:
        print("源表 {} 无数据，未写入结果表。".format(SOURCE_TABLE))
        return

    label_score_maps, quality_weights, pressure_weights = load_score_config(engine)
    scored_df = add_score_columns(
        df,
        label_score_maps=label_score_maps,
        quality_weights=quality_weights,
        pressure_weights=pressure_weights,
    )

    upsert_scored_dataframe(engine, scored_df)

    print("=" * 80)
    print("蛋白结构评分完成")
    print("源表：{}".format(SOURCE_TABLE))
    print("结果表：{}（if_exists={}）".format(OUTPUT_TABLE, OUTPUT_IF_EXISTS))
    print("写入行数：{}".format(len(scored_df)))
    print("=" * 80)
    print(scored_df[[col for col in SCORED_OUTPUT_COLUMNS if col in scored_df.columns]].head())


if __name__ == "__main__":
    main()
