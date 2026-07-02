# -*- coding: utf-8 -*-
import os
import re
import pandas as pd
import numpy as np
from sqlalchemy import Numeric, create_engine, text

# =========================
# 1. 数据库连接
# =========================
DB_USER = os.getenv("MYSQL_USER", "root")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DB_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
DB_PORT = os.getenv("MYSQL_PORT", "3306")
DB_NAME = os.getenv("MYSQL_DATABASE", "protein_feature_platform")

TABLE_NAME = "catfood_fat_material_features"
OUTPUT_TABLE = "catfood_fat_material_features_scored"
OUTPUT_IF_EXISTS = os.getenv("FAT_SCORE_IF_EXISTS", "append")
SCORE_DECIMALS = 3
LOW_LEVEL_CUT = 1.0 / 3.0
HIGH_LEVEL_CUT = 2.0 / 3.0

engine = create_engine(
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
)

# =========================
# 2. 读取数据
# =========================
df = pd.read_sql(f"SELECT * FROM {TABLE_NAME}", engine)
if os.getenv("FORMULA_ID"):
    df = df[pd.to_numeric(df["formula_id"], errors="coerce") == int(os.environ["FORMULA_ID"])].copy()

OPTIONAL_INPUT_DEFAULTS = {
    "ingredient_composition": "",
    "needs_review": "",
    "review_reason": "",
    "created_at": pd.NaT,
}

for optional_col, default_value in OPTIONAL_INPUT_DEFAULTS.items():
    if optional_col not in df.columns:
        df[optional_col] = default_value


# =========================
# 3. 通用工具函数
# =========================
def normalize_text(text):
    if pd.isna(text) or text is None:
        return ""
    return str(text).strip()


def split_items(text):
    text = normalize_text(text)
    if text == "":
        return []
    text = (
        text.replace("，", ",")
            .replace("、", ",")
            .replace("；", ",")
            .replace("|", ",")
    )
    return [x.strip() for x in text.split(",") if x.strip()]


def normalize_score(series):
    s = pd.to_numeric(series, errors="coerce").fillna(0)
    min_val = s.min()
    max_val = s.max()
    if max_val == min_val:
        return pd.Series([0.0] * len(s), index=s.index)
    return (s - min_val) / (max_val - min_val)


def parse_position_mapping(text):
    text = normalize_text(text)
    if not text:
        return {}

    text = text.replace("，", ",").replace("；", ",").replace("：", ":")
    parts = [p.strip() for p in text.split(",") if p.strip()]

    result = {}
    for part in parts:
        match = re.match(r"(.+?)\s*[:=]\s*(\d+)", part)
        if match:
            name = match.group(1).strip()
            pos = int(match.group(2))
            result[name] = pos
    return result


def get_position_weight(position):
    """
    前段（1-5）   = 1.0
    中段（6-10）  = 0.6
    后段（11+）   = 0.3
    缺失          = 0.5
    """
    if position is None or pd.isna(position):
        return 0.5
    try:
        p = int(position)
    except Exception:
        return 0.5

    if p <= 5:
        return 1.0
    elif p <= 10:
        return 0.6
    else:
        return 0.3


def match_best_position(item, position_map):
    if not position_map:
        return None

    if item in position_map:
        return position_map[item]

    for k, v in position_map.items():
        if item in k or k in item:
            return v

    return None


def unique_keep_order(values):
    result = []
    seen = set()
    for value in values:
        value = normalize_text(value)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


# =========================
# 4. 关键词体系
# =========================
EXPLICIT_FISH_OIL_KEYWORDS = [
    "鱼油", "三文鱼油", "鲑鱼油", "鳕鱼油", "鱈鱼油", "沙丁鱼油",
    "凤尾鱼油", "鲱鱼油", "金枪鱼油", "明太鱼油", "海洋鱼油", "深海鱼油",
    "磷虾油", "虾油"
]

FISH_SOURCE_KEYWORDS = [
    "鱼类脂肪", "三文鱼", "鳕鱼", "鱈鱼", "鲱鱼", "沙丁鱼", "凤尾鱼",
    "金枪鱼", "鲭鱼", "比目鱼", "白鱼", "明太鱼", "海鱼", "磷虾", "磷虾粉"
]

ANIMAL_FAT_KEYWORDS = [
    "鸡油", "鸡脂", "鸡脂肪", "鸭油", "鸭脂", "鸭脂肪", "牛油", "牛脂", "牛脂肪",
    "羊油", "羊脂", "羊脂肪", "猪油", "猪脂", "猪脂肪", "鹅油", "鹅脂", "鹅脂肪",
    "动物脂肪", "动物油", "磷虾油", "虾油", "磷虾粉"
]

PLANT_FAT_KEYWORDS = [
    "植物脂肪", "植物油", "亚麻籽油", "亚麻籽", "椰子油", "葵花籽油",
    "葵花油", "菜籽油", "芥花油", "芥花籽油", "大豆油", "豆油",
    "玉米油", "橄榄油", "紫苏籽油", "紫苏籽", "海藻油"
]

TRACE_ELEMENT_KEYWORDS = ["锌", "铁", "铜", "锰", "硒", "碘", "微量元素"]


# =========================
# 5. 配料位置推断
# =========================
def clean_ingredient_token(text):
    text = normalize_text(text)
    if not text:
        return ""

    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\([^)]*?\d+(?:\.\d+)?\s*%[^)]*?\)", "", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*%", "", text)
    text = re.sub(r"\s+", "", text)
    return text.strip()


def parse_ingredient_positions(text):
    text = normalize_text(text)
    if not text:
        return []

    text = (
        text.replace("，", "、")
            .replace(",", "、")
            .replace("；", "、")
            .replace(";", "、")
            .replace("\n", "、")
    )
    parts = [clean_ingredient_token(x) for x in text.split("、")]
    return [(idx + 1, part) for idx, part in enumerate(parts) if part]


def item_position_keywords(item):
    item = normalize_text(item)
    keywords = [item]

    if item in {"鱼油", "鱼类脂肪"} or "鱼油" in item:
        keywords.extend(EXPLICIT_FISH_OIL_KEYWORDS)
    if "鱼类脂肪" in item:
        keywords.extend(FISH_SOURCE_KEYWORDS)

    if item in {"动物脂肪", "动物油"}:
        keywords.extend(ANIMAL_FAT_KEYWORDS)
    if item in {"植物脂肪", "植物油"}:
        keywords.extend(PLANT_FAT_KEYWORDS)

    oil_aliases = {
        "鸡油": ["鸡油", "鸡脂", "鸡脂肪"],
        "鸭油": ["鸭油", "鸭脂", "鸭脂肪"],
        "牛油": ["牛油", "牛脂", "牛脂肪"],
        "羊油": ["羊油", "羊脂", "羊脂肪"],
        "猪油": ["猪油", "猪脂", "猪脂肪"],
        "大豆油": ["大豆油", "豆油"],
        "菜籽油": ["菜籽油", "芥花油", "芥花籽油"],
        "葵花籽油": ["葵花籽油", "葵花油"],
        "紫苏籽": ["紫苏籽", "紫苏籽油"],
        "海藻油": ["海藻油"],
    }
    keywords.extend(oil_aliases.get(item, []))

    if item == "矿物质":
        keywords.append("矿物质")
    if item == "微量元素":
        keywords.extend(TRACE_ELEMENT_KEYWORDS)

    return unique_keep_order(keywords)


def keyword_matches_token(keyword, token):
    keyword = clean_ingredient_token(keyword)
    token = clean_ingredient_token(token)
    if not keyword or not token:
        return False

    if keyword in token:
        return True
    if len(token) >= 2 and token in keyword:
        return True
    return False


def find_item_position(item, ingredient_positions):
    keywords = item_position_keywords(item)
    for position, token in ingredient_positions:
        for keyword in keywords:
            if keyword_matches_token(keyword, token):
                return position
    return None


def format_position_mapping(position_map):
    return ", ".join(
        f"{item}:{position}"
        for item, position in position_map.items()
        if position is not None
    )


def infer_positions_text(items_text, ingredient_composition):
    items = split_items(items_text)
    ingredient_positions = parse_ingredient_positions(ingredient_composition)
    if not items or not ingredient_positions:
        return ""

    position_map = {}
    for item in items:
        position = find_item_position(item, ingredient_positions)
        if position is not None:
            position_map[item] = position

    return format_position_mapping(position_map)


def position_match_count(positions_text):
    return len(parse_position_mapping(positions_text))


def get_positions_text(row, explicit_col, inferred_col):
    explicit_positions = normalize_text(row.get(explicit_col, ""))
    if explicit_positions:
        return explicit_positions
    return row.get(inferred_col, "")


# =========================
# 6. 类型识别
# =========================
def text_contains_any(text, keywords):
    text = normalize_text(text)
    return any(kw in text for kw in keywords)


def row_contains_any(row, columns, keywords):
    for col in columns:
        if text_contains_any(row.get(col, ""), keywords):
            return True
    return False


def has_explicit_fish_oil(row):
    review_reason = normalize_text(row.get("review_reason", ""))
    if "无明确鱼油支持" in review_reason:
        return False

    fields = [
        "fat_sources",
        "omega3_sources",
        "ingredient_composition",
    ]
    return row_contains_any(row, fields, EXPLICIT_FISH_OIL_KEYWORDS)


def has_fish_source(row):
    fields = [
        "fat_sources",
        "fat_source_types",
        "omega3_sources",
        "ingredient_composition",
    ]
    return row_contains_any(row, fields, FISH_SOURCE_KEYWORDS)


def has_animal_fat(row):
    fields = [
        "fat_sources",
        "fat_source_types",
        "omega6_sources",
        "ingredient_composition",
    ]
    return row_contains_any(row, fields, ANIMAL_FAT_KEYWORDS)


def has_plant_fat(row):
    fields = [
        "fat_sources",
        "fat_source_types",
        "omega6_sources",
        "ingredient_composition",
    ]
    return row_contains_any(row, fields, PLANT_FAT_KEYWORDS)


def infer_fat_structure_type(row):
    has_animal = int(row.get("has_animal_fat", 0)) == 1
    has_plant = int(row.get("has_plant_fat", 0)) == 1
    has_fish_oil = int(row.get("has_explicit_fish_oil", 0)) == 1
    has_fish = int(row.get("has_fish_source", 0)) == 1

    if has_animal and not has_plant and not has_fish_oil and not has_fish:
        return "纯动物脂肪型"
    if has_plant and not has_animal and not has_fish_oil and not has_fish:
        return "纯植物脂肪型"
    if has_animal and has_plant and not has_fish_oil:
        return "动物+植物混合型"
    if has_animal and has_fish_oil and not has_plant:
        return "动物脂肪+鱼油型"
    if has_animal and has_plant and has_fish_oil:
        return "动物+植物+鱼油复合型"
    if has_fish_oil and not has_animal and not has_plant:
        return "鱼油支持型"
    if has_fish and not has_fish_oil:
        return "鱼类脂肪来源型"
    return "其他脂肪结构型"


# =========================
# 7. 基础分函数
# =========================
def animal_fat_item_base_score(item):
    item = normalize_text(item)
    if text_contains_any(item, ["鸡油", "鸭油", "牛油", "羊油", "猪油", "鹅油"]):
        return 2.0
    if text_contains_any(item, ["磷虾油", "虾油"]):
        return 1.4
    if text_contains_any(item, ["磷虾粉"]):
        return 0.8
    if text_contains_any(item, ["动物脂肪", "动物油", "鸡脂", "鸭脂", "牛脂", "羊脂", "猪脂", "鹅脂"]):
        return 1.8
    return 0.0


def plant_fat_item_base_score(item):
    item = normalize_text(item)
    if text_contains_any(item, ["植物脂肪", "植物油"]):
        return 1.2
    if text_contains_any(item, ["大豆油", "豆油", "玉米油", "菜籽油", "芥花油", "葵花籽油", "葵花油"]):
        return 1.0
    if text_contains_any(item, ["亚麻籽油", "亚麻籽", "紫苏籽油", "紫苏籽", "橄榄油", "海藻油"]):
        return 0.8
    if text_contains_any(item, ["椰子油"]):
        return 0.9
    return 0.0


def fish_oil_item_base_score(item):
    item = normalize_text(item)
    if text_contains_any(item, EXPLICIT_FISH_OIL_KEYWORDS):
        return 2.0
    return 0.0


def fish_source_item_base_score(item):
    item = normalize_text(item)
    if "鱼类脂肪" in item:
        return 1.2
    if text_contains_any(item, FISH_SOURCE_KEYWORDS):
        return 1.0
    return 0.0


def antioxidant_item_base_score(item):
    item = normalize_text(item)
    if item == "":
        return 0.0
    return 1.0


def micronutrient_item_base_score(item):
    item = normalize_text(item)
    if "矿物质" in item:
        return 0.5
    if "微量元素" in item:
        return 0.5
    return 0.3


def omega3_item_base_score(item):
    item = normalize_text(item)
    if text_contains_any(item, EXPLICIT_FISH_OIL_KEYWORDS):
        return 2.0
    if "鱼类脂肪" in item:
        return 1.1
    if text_contains_any(item, FISH_SOURCE_KEYWORDS):
        return 0.9
    if "海洋" in item:
        return 0.9
    return 0.0


def omega6_animal_item_base_score(item):
    item = normalize_text(item)
    if text_contains_any(item, ["鸡油", "鸭油", "牛油", "羊油", "猪油", "鹅油"]):
        return 2.0
    if text_contains_any(item, ["动物脂肪", "动物油", "鸡脂", "鸭脂", "牛脂", "羊脂", "猪脂", "鹅脂"]):
        return 1.8
    return 0.0


def omega6_plant_item_base_score(item):
    item = normalize_text(item)
    if text_contains_any(item, ["植物脂肪", "植物油"]):
        return 1.5
    if text_contains_any(item, ["大豆油", "豆油", "玉米油", "菜籽油", "芥花油", "葵花籽油", "葵花油"]):
        return 1.4
    if text_contains_any(item, ["亚麻籽油", "亚麻籽", "紫苏籽油", "紫苏籽", "橄榄油"]):
        return 0.8
    if text_contains_any(item, ["椰子油", "海藻油"]):
        return 0.6
    return 0.0


# =========================
# 8. 通用加权打分
# =========================
def calc_weighted_item_score(items_text, positions_text, base_score_func):
    items = split_items(items_text)
    position_map = parse_position_mapping(positions_text)

    score = 0.0
    for item in items:
        base_score = base_score_func(item)
        if base_score <= 0:
            continue
        pos = match_best_position(item, position_map)
        weight = get_position_weight(pos)
        score += base_score * weight
    return score


# =========================
# 9. 具体子分
# =========================
def calc_animal_fat_load_score(row):
    fat_sources = row.get("fat_sources", "")
    fat_source_positions = get_positions_text(
        row, "fat_source_positions", "fat_source_positions_inferred"
    )
    return calc_weighted_item_score(
        fat_sources, fat_source_positions, animal_fat_item_base_score
    )


def calc_plant_fat_interference_score(row):
    fat_sources = row.get("fat_sources", "")
    fat_source_positions = get_positions_text(
        row, "fat_source_positions", "fat_source_positions_inferred"
    )
    return calc_weighted_item_score(
        fat_sources, fat_source_positions, plant_fat_item_base_score
    )


def calc_fish_oil_load_score(row):
    fat_sources = row.get("fat_sources", "")
    fat_source_positions = get_positions_text(
        row, "fat_source_positions", "fat_source_positions_inferred"
    )

    score = calc_weighted_item_score(
        fat_sources, fat_source_positions, fish_oil_item_base_score
    )

    # 若 fat_sources 未显式列出鱼油，但其他字段明确有鱼油，补一个较弱分
    if has_explicit_fish_oil(row):
        if not text_contains_any(fat_sources, EXPLICIT_FISH_OIL_KEYWORDS):
            score += 1.0

    return score


def calc_fish_source_score(row):
    fat_sources = row.get("fat_sources", "")
    fat_source_positions = get_positions_text(
        row, "fat_source_positions", "fat_source_positions_inferred"
    )

    score = calc_weighted_item_score(
        fat_sources, fat_source_positions, fish_source_item_base_score
    )

    # 若 fat_sources 里没有鱼类脂肪，但配方中存在鱼类来源，补低分
    if has_fish_source(row) and not has_explicit_fish_oil(row):
        if score == 0:
            score += 0.8

    return score


def calc_antioxidant_protection_score(row):
    antioxidant_sources = row.get("antioxidant_sources", "")
    antioxidant_positions = get_positions_text(
        row, "antioxidant_positions", "antioxidant_positions_inferred"
    )
    return calc_weighted_item_score(
        antioxidant_sources, antioxidant_positions, antioxidant_item_base_score
    )


def calc_micronutrient_support_score(row):
    micronutrient_sources = row.get("micronutrient_sources", "")
    micronutrient_positions = get_positions_text(
        row, "micronutrient_positions", "micronutrient_positions_inferred"
    )
    return calc_weighted_item_score(
        micronutrient_sources, micronutrient_positions, micronutrient_item_base_score
    )


def calc_omega3_raw_score(row):
    omega3_sources = row.get("omega3_sources", "")
    omega3_positions = get_positions_text(
        row, "omega3_positions", "omega3_positions_inferred"
    )

    score = calc_weighted_item_score(
        omega3_sources, omega3_positions, omega3_item_base_score
    )

    if has_explicit_fish_oil(row):
        if not text_contains_any(omega3_sources, EXPLICIT_FISH_OIL_KEYWORDS):
            score += 1.4
    elif has_fish_source(row):
        if score == 0:
            score += 0.7

    return score


def calc_omega6_animal_raw_score(row):
    omega6_sources = row.get("omega6_sources", "")
    omega6_positions = get_positions_text(
        row, "omega6_positions", "omega6_positions_inferred"
    )

    score = calc_weighted_item_score(
        omega6_sources, omega6_positions, omega6_animal_item_base_score
    )

    # 如果 omega6_sources 没写，但 fat_sources 中有明显动物脂肪，也补一些
    if score == 0 and has_animal_fat(row):
        fat_sources = row.get("fat_sources", "")
        fat_positions = get_positions_text(
            row, "fat_source_positions", "fat_source_positions_inferred"
        )
        score += 0.7 * calc_weighted_item_score(
            fat_sources, fat_positions, omega6_animal_item_base_score
        )

    return score


def calc_omega6_plant_raw_score(row):
    omega6_sources = row.get("omega6_sources", "")
    omega6_positions = get_positions_text(
        row, "omega6_positions", "omega6_positions_inferred"
    )

    score = calc_weighted_item_score(
        omega6_sources, omega6_positions, omega6_plant_item_base_score
    )

    if score == 0 and has_plant_fat(row):
        fat_sources = row.get("fat_sources", "")
        fat_positions = get_positions_text(
            row, "fat_source_positions", "fat_source_positions_inferred"
        )
        score += 0.7 * calc_weighted_item_score(
            fat_sources, fat_positions, omega6_plant_item_base_score
        )

    return score


def calc_fat_mix_complexity_score(row):
    count = 0
    if int(row.get("has_animal_fat", 0)) == 1:
        count += 1
    if int(row.get("has_plant_fat", 0)) == 1:
        count += 1
    if int(row.get("has_explicit_fish_oil", 0)) == 1 or int(row.get("has_fish_source", 0)) == 1:
        count += 1

    if count <= 1:
        return 1.0
    elif count == 2:
        return 2.0
    return 3.0


def calc_needs_review_flag(row):
    existing = row.get("needs_review", None)
    if pd.notna(existing) and str(existing).strip() != "":
        return existing

    fat_empty = len(split_items(row.get("fat_sources", ""))) == 0
    omega3_empty = len(split_items(row.get("omega3_sources", ""))) == 0
    omega6_empty = len(split_items(row.get("omega6_sources", ""))) == 0

    if fat_empty or (omega3_empty and omega6_empty):
        return 1
    return 0


def build_fat_reason_tags(row):
    """
    脂肪结构原因标签。

    注意：
    - 这里用的是标准化后的 0-1 子分，不是原始分。
    - 阈值 0.70/0.30 是解释阈值，不是医学诊断阈值。
    """
    tags = []

    if row.get("animal_fat_load_score", 0) >= 0.70:
        tags.append("动物脂肪负担偏高")

    if row.get("plant_fat_interference_score", 0) >= 0.70:
        tags.append("植物油脂干扰偏高")

    if row.get("omega6_score", 0) >= 0.70:
        tags.append("Omega-6压力偏高")

    if row.get("omega3_score", 0) <= 0.30:
        tags.append("Omega-3支持不足")

    if row.get("omega_imbalance_score", 0) >= 0.70:
        tags.append("Omega脂肪酸比例偏失衡")

    if row.get("fat_mix_complexity_score", 0) >= 0.70:
        tags.append("脂肪来源较复杂")

    if row.get("fat_regulation_score", 0) <= 0.30:
        tags.append("脂肪调节支持不足")

    if not tags:
        tags.append("暂无明显脂肪结构风险")

    return ",".join(tags)


# =========================
# 10. 输出格式辅助
# =========================
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


def round_score_columns(frame):
    rounded = frame.copy()
    for col in rounded.columns:
        if not is_score_value_column(col):
            continue

        series = rounded[col]
        if is_numeric_score_series(series):
            rounded[col] = series.round(SCORE_DECIMALS)
            continue

        numeric_series = pd.to_numeric(series, errors="coerce")
        non_null_count = series.notna().sum()
        numeric_count = numeric_series.notna().sum()
        if non_null_count > 0 and numeric_count == non_null_count:
            rounded[col] = numeric_series.round(SCORE_DECIMALS)

    return rounded


def score_column_sql_types(frame):
    return {
        col: Numeric(12, SCORE_DECIMALS)
        for col in frame.columns
        if is_score_value_column(col) and is_numeric_score_series(frame[col])
    }


def quote_identifier(name):
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        raise ValueError(f"非法表名：{name}")
    return f"`{name}`"


def ensure_output_table_metadata():
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

        required_columns = {
            "formula_id": "BIGINT UNSIGNED NULL",
            "source_id": "BIGINT NULL",
            "product_key": "VARCHAR(1024) NULL",
            "brand": "VARCHAR(255) NULL",
            "product_name": "VARCHAR(512) NULL",
        }
        for column_name, column_type in required_columns.items():
            if column_name not in column_map:
                conn.execute(
                    text(
                        "ALTER TABLE {} ADD COLUMN {} {}".format(
                            quote_identifier(OUTPUT_TABLE),
                            quote_identifier(column_name),
                            column_type,
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


def table_exists(table_name):
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


def upsert_output(output_df):
    if OUTPUT_IF_EXISTS in {"replace", "fail"} or not table_exists(OUTPUT_TABLE):
        output_df.to_sql(
            OUTPUT_TABLE,
            engine,
            if_exists=OUTPUT_IF_EXISTS if table_exists(OUTPUT_TABLE) else "replace",
            index=False,
            dtype=score_column_sql_types(output_df),
        )
        ensure_output_table_metadata()
        return

    ensure_output_table_metadata()
    temp_table = "__tmp_{}_{}".format(OUTPUT_TABLE, os.getpid())
    quoted_temp = quote_identifier(temp_table)
    quoted_output = quote_identifier(OUTPUT_TABLE)
    columns = list(output_df.columns)
    insert_columns = ", ".join(quote_identifier(col) for col in columns)
    select_columns = ", ".join(quote_identifier(col) for col in columns)
    update_columns = [col for col in columns if col != "id"]
    update_sql = ", ".join(
        "{0}=VALUES({0})".format(quote_identifier(col)) for col in update_columns
    )

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS {}".format(quoted_temp)))
        output_df.to_sql(
            temp_table,
            conn,
            if_exists="replace",
            index=False,
            dtype=score_column_sql_types(output_df),
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


def classify_score_level(score_value, low_label, middle_label, high_label):
    try:
        value = float(score_value)
    except (TypeError, ValueError):
        return ""

    if pd.isna(value):
        return ""
    if value < LOW_LEVEL_CUT:
        return low_label
    if value < HIGH_LEVEL_CUT:
        return middle_label
    return high_label


# =========================
# 11. 推断位置字段
# =========================
POSITION_SOURCE_COLS = [
    ("fat_sources", "fat_source_positions_inferred"),
    ("antioxidant_sources", "antioxidant_positions_inferred"),
    ("micronutrient_sources", "micronutrient_positions_inferred"),
    ("omega3_sources", "omega3_positions_inferred"),
    ("omega6_sources", "omega6_positions_inferred"),
]

for source_col, position_col in POSITION_SOURCE_COLS:
    df[position_col] = df.apply(
        lambda row, col=source_col: infer_positions_text(
            row.get(col, ""),
            row.get("ingredient_composition", ""),
        ),
        axis=1,
    )

df["fat_position_item_count"] = df.apply(
    lambda row: sum(len(split_items(row.get(source_col, ""))) for source_col, _ in POSITION_SOURCE_COLS),
    axis=1,
)
df["fat_position_match_count"] = df.apply(
    lambda row: sum(position_match_count(row.get(position_col, "")) for _, position_col in POSITION_SOURCE_COLS),
    axis=1,
)
df["fat_position_match_rate"] = np.where(
    df["fat_position_item_count"] > 0,
    df["fat_position_match_count"] / df["fat_position_item_count"],
    0.0,
)

# =========================
# 12. 类型字段
# =========================
df["has_explicit_fish_oil"] = df.apply(lambda row: 1 if has_explicit_fish_oil(row) else 0, axis=1)
df["has_fish_source"] = df.apply(lambda row: 1 if has_fish_source(row) else 0, axis=1)
df["has_animal_fat"] = df.apply(lambda row: 1 if has_animal_fat(row) else 0, axis=1)
df["has_plant_fat"] = df.apply(lambda row: 1 if has_plant_fat(row) else 0, axis=1)

df["fat_structure_type"] = df.apply(infer_fat_structure_type, axis=1)

# =========================
# 13. 原始子分
# =========================
df["animal_fat_load_score_raw"] = df.apply(calc_animal_fat_load_score, axis=1)
df["plant_fat_interference_score_raw"] = df.apply(calc_plant_fat_interference_score, axis=1)
df["fish_oil_load_score_raw"] = df.apply(calc_fish_oil_load_score, axis=1)
df["fish_source_score_raw"] = df.apply(calc_fish_source_score, axis=1)

df["antioxidant_protection_score_raw"] = df.apply(calc_antioxidant_protection_score, axis=1)
df["micronutrient_support_score_raw"] = df.apply(calc_micronutrient_support_score, axis=1)

df["omega3_score_raw"] = df.apply(calc_omega3_raw_score, axis=1)
df["omega6_animal_score_raw"] = df.apply(calc_omega6_animal_raw_score, axis=1)
df["omega6_plant_score_raw"] = df.apply(calc_omega6_plant_raw_score, axis=1)

df["fat_mix_complexity_score_raw"] = df.apply(calc_fat_mix_complexity_score, axis=1)

# 合成 omega6 总分，保留来源拆分
df["omega6_score_raw"] = (
    0.7 * df["omega6_animal_score_raw"] +
    0.3 * df["omega6_plant_score_raw"]
)

# =========================
# 14. 标准化
# =========================
norm_cols = [
    "animal_fat_load_score_raw",
    "plant_fat_interference_score_raw",
    "fish_oil_load_score_raw",
    "fish_source_score_raw",
    "antioxidant_protection_score_raw",
    "micronutrient_support_score_raw",
    "omega3_score_raw",
    "omega6_animal_score_raw",
    "omega6_plant_score_raw",
    "omega6_score_raw",
    "fat_mix_complexity_score_raw",
]

for col in norm_cols:
    df[col.replace("_raw", "")] = normalize_score(df[col])

# =========================
# 15. 双分制 + Omega失衡 + 脂肪风险分
# 定义：
# fat_oily_score        分越高 = 动物/植物脂肪负载越重
# fat_regulation_score  分越高 = 抗氧化/微量元素/Omega-3 调节能力越强
# omega_imbalance_score 分越高 = Omega-6 偏强、Omega-3 缓冲不足
# fat_score             分越高 = 越容易推高脂肪消化/结构负担
# =========================

# Omega-6 高 + Omega-3 低，才是真正需要被解释为“脂肪酸失衡”的结构。
df["omega_imbalance_score"] = (
    0.65 * df["omega6_score"] +
    0.35 * (1 - df["omega3_score"])
)

# B 方案：油脂负载、Omega 失衡、脂肪来源复杂度作为互斥机制项。
# Omega-6 不再直接进入 fat_oily_score，避免与 omega_imbalance_score 重复计入。
df["fat_oily_score"] = (
    0.70 * df["animal_fat_load_score"] +
    0.30 * df["plant_fat_interference_score"]
)

# 提高 Omega-3 在脂肪调节中的权重，让鱼油/海洋脂肪酸更多体现为缓冲项。
df["fat_regulation_score"] = (
    0.35 * df["antioxidant_protection_score"] +
    0.25 * df["micronutrient_support_score"] +
    0.30 * df["omega3_score"] +
    0.10 * (1 - df["omega6_score"])
)

# 可直接接入主风险模型 sku_feature_input.fat_score 的脂肪风险分。
df["fat_score"] = (
    0.50 * df["fat_oily_score"] +
    0.35 * df["omega_imbalance_score"] +
    0.15 * df["fat_mix_complexity_score"]
)

# 可解释标签：用于定位脂肪风险的主要来源。
df["fat_reason_tags"] = df.apply(build_fat_reason_tags, axis=1)

# =========================
# 16. 复核标记
# =========================
df["needs_review_flag"] = df.apply(calc_needs_review_flag, axis=1)

# 可选：时间戳
df["fat_scored_at"] = pd.Timestamp.now()

# 四舍五入
df = round_score_columns(df)

# =========================
# 17. 写出结果
# =========================
OUTPUT_COLUMNS = [
    "id",
    "formula_id",
    "source_id",
    "product_key",
    "brand",
    "product_name",

    "fat_structure_type",

    "animal_fat_load_score",
    "plant_fat_interference_score",
    "fish_oil_load_score",
    "fish_source_score",

    "omega3_score",
    "omega6_animal_score",
    "omega6_plant_score",
    "omega6_score",
    "omega_imbalance_score",

    "antioxidant_protection_score",
    "micronutrient_support_score",

    "fat_mix_complexity_score",
    "fat_oily_score",
    "fat_regulation_score",
    "fat_score",

    "fat_reason_tags",

    "needs_review_flag",
    "fat_position_match_rate",

    "created_at",
]

output_df = df[[column for column in OUTPUT_COLUMNS if column in df.columns]].copy()

upsert_output(output_df)

print(f"完成，结果已写入表: {OUTPUT_TABLE}")
print(output_df.head())
