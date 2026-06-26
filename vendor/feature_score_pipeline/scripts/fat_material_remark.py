import re
import os
import pymysql
import pandas as pd

from brand_normalizer import build_product_key as build_corrected_product_key
from brand_normalizer import correct_brand


# =====================================
# 1. 数据库配置
# =====================================
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
    "cursorclass": pymysql.cursors.Cursor,
}

# 跨库表名
SOURCE_TABLE = os.getenv(
    "CATFOOD_FORMULA_INPUT_FQ",
    "csv_labeling.catfood_formula_feature_input",
)
TARGET_TABLE = "protein_feature_platform.catfood_fat_material_features"


# =====================================
# 2. 文本预处理
# =====================================
def clean_text(text):
    if text is None:
        return ""
    text = str(text)
    # 保留括号内的配方说明，例如“鸡油（保存于天然维生素E）”，
    # 否则会漏掉抗氧化物和植物脂肪的关键信息。
    text = re.sub(r"[（）()\[\]［］【】]", "", text)
    text = text.replace("，", "、").replace(",", "、").replace("；", "、").replace(";", "、")
    text = re.sub(r"\s+", "", text)
    return text.strip()


def split_ingredients(text):
    text = clean_text(text)
    if not text:
        return []
    return [x for x in text.split("、") if x]


def normalize_nullable_text(value):
    if value is None or pd.isna(value):
        return None
    value = str(value).strip()
    return value or None


def mysql_safe_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def build_product_key(row):
    brand = normalize_nullable_text(row.get("brand"))
    product_name = normalize_nullable_text(row.get("product_name"))
    brand = correct_brand(
        brand,
        product_name,
        row.get("image_name"),
        row.get("image_path"),
    )
    return build_corrected_product_key(brand, product_name) or None

def build_product_key_legacy(row):
    brand = normalize_nullable_text(row.get("brand"))
    product_name = normalize_nullable_text(row.get("product_name"))
    if brand and product_name:
        return f"{brand}||{product_name}"
    if brand:
        return brand
    if product_name:
        return product_name

    source_id = row.get("source_id")
    if source_id is not None and not pd.isna(source_id):
        return str(int(source_id))

    source_row_id = row.get("source_row_id")
    if source_row_id is not None and not pd.isna(source_row_id):
        return str(int(source_row_id))

    return None


# =====================================
# 3. 规则词典
# =====================================

# ---------- 油脂来源 ----------
FAT_SOURCE_RULES = [
    # 明确动物脂肪
    {"pattern": r"鸡油|鸡脂", "source": "鸡油", "type": "动物脂肪"},
    {"pattern": r"鸭油|鸭脂", "source": "鸭油", "type": "动物脂肪"},
    {"pattern": r"鹅油|鹅脂", "source": "鹅油", "type": "动物脂肪"},
    {"pattern": r"牛油|牛脂", "source": "牛油", "type": "动物脂肪"},
    {"pattern": r"羊油|羊脂", "source": "羊油", "type": "动物脂肪"},
    {"pattern": r"猪油|猪脂", "source": "猪油", "type": "动物脂肪"},
    {"pattern": r"鱼油|海洋鱼油|深海鱼油", "source": "鱼油", "type": "动物脂肪"},
    {"pattern": r"三文鱼油|鲑鱼油", "source": "三文鱼油", "type": "动物脂肪"},
    {"pattern": r"磷虾油|虾油", "source": "磷虾油", "type": "动物脂肪"},
    {"pattern": r"磷虾粉", "source": "磷虾粉", "type": "动物脂肪"},

    # 藻类/植物脂肪
    {"pattern": r"海藻油", "source": "海藻油", "type": "藻类/植物脂肪"},
    {"pattern": r"亚麻籽油", "source": "亚麻籽油", "type": "植物脂肪"},
    {"pattern": r"紫苏籽油|紫苏籽", "source": "紫苏籽", "type": "植物脂肪"},
    {"pattern": r"葵花籽油|葵花油", "source": "葵花籽油", "type": "植物脂肪"},
    {"pattern": r"玉米油", "source": "玉米油", "type": "植物脂肪"},
    {"pattern": r"大豆油|黄豆油|豆油", "source": "大豆油", "type": "植物脂肪"},
    {"pattern": r"菜籽油|芥花油|芥花籽油", "source": "菜籽油", "type": "植物脂肪"},
    {"pattern": r"椰子油", "source": "椰子油", "type": "植物脂肪"},
    {"pattern": r"橄榄油", "source": "橄榄油", "type": "植物脂肪"},
    {"pattern": r"植物油", "source": "植物油", "type": "模糊植物脂肪来源"},

    # 模糊脂肪
    {"pattern": r"动物脂肪", "source": "动物脂肪", "type": "模糊脂肪来源"},
    {"pattern": r"禽脂|禽类脂肪", "source": "禽脂", "type": "模糊脂肪来源"},
]

# ---------- 抗氧化来源 ----------
ANTIOXIDANT_RULES = [
    # 天然抗氧化
    {"pattern": r"新鲜混合生育酚|混合生育酚|生育酚", "source": "混合生育酚", "type": "天然抗氧化物"},
    {"pattern": r"天然维生素E|维生素E|维他命E", "source": "维生素E", "type": "天然抗氧化物"},
    {"pattern": r"维生素C|抗坏血酸|L-抗坏血酸-2-聚磷酸盐|L-抗坏血酸-2-多磷酸盐|L-抗坏血酸-2-磷酸酯", "source": "维生素C", "type": "天然抗氧化物"},
    {"pattern": r"柠檬酸", "source": "柠檬酸", "type": "天然抗氧化物"},
    {"pattern": r"迷迭香提取物|迷迭香萃取物|迷迭香萃取|迷迭香|迷选香萃取物|迷选香提取物|迷选香", "source": "迷迭香提取物", "type": "天然抗氧化物"},
    {"pattern": r"姜黄|姜黄粉|姜黄根", "source": "姜黄", "type": "植物抗氧化来源"},
    {"pattern": r"西红柿|番茄|番茄粉", "source": "番茄", "type": "植物抗氧化来源"},
    {"pattern": r"西蓝花|西兰花|西兰花粉|西蓝花粉", "source": "西蓝花", "type": "植物抗氧化来源"},

    # 人工抗氧化
    {"pattern": r"\bBHA\b", "source": "BHA", "type": "人工抗氧化物"},
    {"pattern": r"\bBHT\b", "source": "BHT", "type": "人工抗氧化物"},
    {"pattern": r"乙氧基喹", "source": "乙氧基喹", "type": "人工抗氧化物"},
    {"pattern": r"TBHQ", "source": "TBHQ", "type": "人工抗氧化物"},

    # 模糊抗氧化标识
    {"pattern": r"抗氧化剂|防腐剂", "source": "抗氧化剂", "type": "模糊抗氧化来源"},
]

# ---------- 微量元素来源 ----------
MICRONUTRIENT_RULES = [
    # 动物内脏
    {"pattern": r"鸡肝|火鸡肝|鸭肝|鹅肝|牛肝|羊肝|猪肝|兔肝", "source": "肝脏", "type": "动物内脏"},
    {"pattern": r"鸡肾|火鸡肾|鸭肾|牛肾|羊肾|猪肾", "source": "肾脏", "type": "动物内脏"},
    {"pattern": r"鸡心|火鸡心|鸭心|牛心|羊心|猪心", "source": "心脏", "type": "动物组织来源"},
    {"pattern": r"脾", "source": "脾脏", "type": "动物内脏"},
    {"pattern": r"蛋黄|全蛋|鸡蛋|鸭蛋|鹌鹑蛋", "source": "蛋类", "type": "天然微量元素来源"},

    # 微量元素强化
    {"pattern": r"蛋白锌|锌蛋白盐|硫酸锌|氧化锌|葡萄糖酸锌", "source": "锌强化", "type": "微量元素强化"},
    {"pattern": r"蛋白铜|硫酸铜|氧化铜|葡萄糖酸铜", "source": "铜强化", "type": "微量元素强化"},
    {"pattern": r"蛋白铁|硫酸亚铁|焦磷酸铁|葡萄糖酸铁", "source": "铁强化", "type": "微量元素强化"},
    {"pattern": r"亚硒酸钠|酵母硒|蛋白硒", "source": "硒强化", "type": "微量元素强化"},
    {"pattern": r"碘酸钙|碘化钾", "source": "碘强化", "type": "微量元素强化"},
    {"pattern": r"锰", "source": "锰强化", "type": "微量元素强化"},

    # 矿物质来源
    {"pattern": r"碳酸钙|磷酸氢钙|磷酸二钙|氯化钾|氯化钠|硫酸钙", "source": "矿物质补充", "type": "矿物质来源"},
    {"pattern": r"海带", "source": "海带", "type": "天然矿物质来源"},
]

# ---------- omega-6 来源 ----------
OMEGA6_RULES = [
    {"pattern": r"鸡油|鸡脂", "source": "鸡油"},
    {"pattern": r"鸭油|鸭脂", "source": "鸭油"},
    {"pattern": r"禽脂|动物脂肪", "source": "动物脂肪"},
    {"pattern": r"玉米油", "source": "玉米油"},
    {"pattern": r"大豆油", "source": "大豆油"},
    {"pattern": r"葵花籽油", "source": "葵花籽油"},
    {"pattern": r"蛋黄|鸡蛋|鸭蛋|鹌鹑蛋", "source": "蛋类"},
    {"pattern": r"肝", "source": "动物内脏"},
]

# ---------- omega-3 来源 ----------
OMEGA3_RULES = [
    # 明确脂肪型
    {"pattern": r"鱼油|海洋鱼油|深海鱼油", "source": "鱼油", "strength": "明确脂肪型omega-3"},
    {"pattern": r"三文鱼油|鲑鱼油", "source": "三文鱼油", "strength": "明确脂肪型omega-3"},
    {"pattern": r"磷虾油|虾油", "source": "磷虾油", "strength": "明确脂肪型omega-3"},
    {"pattern": r"海藻油", "source": "海藻油", "strength": "明确脂肪型omega-3"},

    # 鱼类原料型
    {"pattern": r"磷虾粉|磷虾", "source": "磷虾", "strength": "鱼类原料型omega-3"},
    {"pattern": r"鲱鱼|鲱鱼粉", "source": "鲱鱼", "strength": "鱼类原料型omega-3"},
    {"pattern": r"沙丁鱼|沙丁鱼粉", "source": "沙丁鱼", "strength": "鱼类原料型omega-3"},
    {"pattern": r"鲭鱼|鲭鱼粉", "source": "鲭鱼", "strength": "鱼类原料型omega-3"},
    {"pattern": r"鳕鱼|鳕鱼粉|蓝鳕鱼", "source": "鳕鱼", "strength": "鱼类原料型omega-3"},
    {"pattern": r"比目鱼", "source": "比目鱼", "strength": "鱼类原料型omega-3"},
    {"pattern": r"三文鱼|鲑鱼|白鮭鱼", "source": "三文鱼", "strength": "鱼类原料型omega-3"},
    {"pattern": r"白鱼", "source": "白鱼", "strength": "弱支持/模糊鱼类omega-3"},

    # 植物型
    {"pattern": r"亚麻籽油", "source": "亚麻籽油", "strength": "植物型omega-3"},
    {"pattern": r"紫苏籽|紫苏籽油", "source": "紫苏籽", "strength": "植物型omega-3"},
]


# =====================================
# 4. 通用识别函数
# =====================================
def extract_by_rules(ingredients, rules, with_type=True, type_key="type", allow_multiple_matches=False):
    found_sources = []
    found_types = []

    for ing in ingredients:
        for rule in rules:
            if re.search(rule["pattern"], ing):
                found_sources.append(rule["source"])
                if with_type:
                    found_types.append(rule[type_key])
                if not allow_multiple_matches:
                    break

    return sorted(set(found_sources)), sorted(set(found_types))


def extract_omega_sources(ingredients, rules, type_key="strength"):
    found_sources = []
    found_types = []

    for ing in ingredients:
        for rule in rules:
            if re.search(rule["pattern"], ing):
                found_sources.append(rule["source"])
                found_types.append(rule[type_key])
                break

    return sorted(set(found_sources)), sorted(set(found_types))


# =====================================
# 5. 审核规则
# =====================================
def review_flags(ingredient_text, fat_sources, omega3_sources):
    reasons = []
    text = clean_text(ingredient_text)

    if "动物脂肪" in fat_sources or "禽脂" in fat_sources:
        reasons.append("存在模糊脂肪来源")

    if "白鱼" in omega3_sources:
        reasons.append("存在模糊鱼类omega-3来源")

    if re.search(r"鲱鱼|沙丁鱼|鲭鱼|鳕鱼|三文鱼|白鱼|比目鱼", text) and "鱼油" not in fat_sources and "三文鱼油" not in fat_sources:
        reasons.append("有鱼类原料但无明确鱼油支持")

    if reasons:
        return 1, "；".join(reasons)
    return 0, None


# =====================================
# 6. 单条识别
# =====================================
def parse_material_features(ingredient_text):
    ingredients = split_ingredients(ingredient_text)

    fat_sources, fat_types = extract_by_rules(ingredients, FAT_SOURCE_RULES, with_type=True, type_key="type")
    antioxidant_sources, antioxidant_types = extract_by_rules(
        ingredients,
        ANTIOXIDANT_RULES,
        with_type=True,
        type_key="type",
        allow_multiple_matches=True,
    )
    micronutrient_sources, micronutrient_types = extract_by_rules(ingredients, MICRONUTRIENT_RULES, with_type=True, type_key="type")
    omega6_sources, _ = extract_by_rules(ingredients, OMEGA6_RULES, with_type=False)
    omega3_sources, _ = extract_omega_sources(ingredients, OMEGA3_RULES, type_key="strength")

    needs_review, review_reason = review_flags(
        ingredient_text,
        fat_sources,
        omega3_sources
    )

    return {
        "fat_sources": "、".join(fat_sources) if fat_sources else None,
        "fat_source_types": "、".join(fat_types) if fat_types else None,
        "antioxidant_sources": "、".join(antioxidant_sources) if antioxidant_sources else None,
        "antioxidant_types": "、".join(antioxidant_types) if antioxidant_types else None,
        "micronutrient_sources": "、".join(micronutrient_sources) if micronutrient_sources else None,
        "micronutrient_types": "、".join(micronutrient_types) if micronutrient_types else None,
        "omega6_sources": "、".join(omega6_sources) if omega6_sources else None,
        "omega3_sources": "、".join(omega3_sources) if omega3_sources else None,
        "needs_review": needs_review,
        "review_reason": review_reason,
    }


# =====================================
# 7. 目标表结构保障
# =====================================
def ensure_target_table(conn):
    create_sql = f"""
        CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
            id BIGINT NOT NULL,
            formula_id BIGINT UNSIGNED NULL,
            source_row_id BIGINT NULL,
            source_id BIGINT NULL,
            source_table VARCHAR(128) NULL,
            product_key VARCHAR(1024) NULL,
            brand VARCHAR(255) NULL,
            product_name VARCHAR(512) NULL,
            image_path VARCHAR(1024) NULL,
            image_name VARCHAR(255) NULL,
            file_sha256 CHAR(64) NULL,
            parse_batch_id VARCHAR(32) NULL,
            source_parse_ts DATETIME NULL,
            source_updated_ts DATETIME NULL,
            ingredient_composition LONGTEXT NULL,
            fat_sources TEXT NULL,
            fat_source_types VARCHAR(255) NULL,
            antioxidant_sources TEXT NULL,
            antioxidant_types VARCHAR(255) NULL,
            micronutrient_sources TEXT NULL,
            micronutrient_types VARCHAR(255) NULL,
            omega6_sources TEXT NULL,
            omega3_sources TEXT NULL,
            needs_review TINYINT DEFAULT 0,
            review_reason VARCHAR(255) NULL,
            created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id)
        )
    """

    required_columns = {
        "formula_id": "BIGINT UNSIGNED NULL",
        "source_row_id": "BIGINT NULL",
        "source_id": "BIGINT NULL",
        "source_table": "VARCHAR(128) NULL",
        "product_key": "VARCHAR(1024) NULL",
        "brand": "VARCHAR(255) NULL",
        "product_name": "VARCHAR(512) NULL",
        "image_path": "VARCHAR(1024) NULL",
        "image_name": "VARCHAR(255) NULL",
        "file_sha256": "CHAR(64) NULL",
        "parse_batch_id": "VARCHAR(32) NULL",
        "source_parse_ts": "DATETIME NULL",
        "source_updated_ts": "DATETIME NULL",
        "ingredient_composition": "LONGTEXT NULL",
        "fat_sources": "TEXT NULL",
        "fat_source_types": "VARCHAR(255) NULL",
        "antioxidant_sources": "TEXT NULL",
        "antioxidant_types": "VARCHAR(255) NULL",
        "micronutrient_sources": "TEXT NULL",
        "micronutrient_types": "VARCHAR(255) NULL",
        "omega6_sources": "TEXT NULL",
        "omega3_sources": "TEXT NULL",
        "guarantee_crude_fat_value": "DECIMAL(18,2) NULL",
        "guarantee_crude_fat_unit": "VARCHAR(50) NULL",
        "guarantee_crude_fat_operator": "VARCHAR(10) NULL",
        "guarantee_crude_fat_basis": "VARCHAR(50) NULL",
        "guarantee_crude_fat_raw_text": "VARCHAR(255) NULL",
        "needs_review": "TINYINT DEFAULT 0",
        "review_reason": "VARCHAR(255) NULL",
        "created_at": "TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    }
    required_indexes = {
        "uk_source_id": "ALTER TABLE {table} ADD UNIQUE KEY uk_source_id (source_id)",
        "idx_source_row_id": "ALTER TABLE {table} ADD KEY idx_source_row_id (source_row_id)",
        "idx_brand": "ALTER TABLE {table} ADD KEY idx_brand (brand)",
        "idx_product_name": "ALTER TABLE {table} ADD KEY idx_product_name (product_name)",
        "idx_file_sha256": "ALTER TABLE {table} ADD KEY idx_file_sha256 (file_sha256)",
    }

    with conn.cursor() as cursor:
        cursor.execute(create_sql)

        cursor.execute(f"SHOW COLUMNS FROM {TARGET_TABLE}")
        existing_columns = {row[0] for row in cursor.fetchall()}
        for column_name, column_ddl in required_columns.items():
            if column_name not in existing_columns:
                cursor.execute(f"ALTER TABLE {TARGET_TABLE} ADD COLUMN {column_name} {column_ddl}")

        cursor.execute(f"SHOW INDEX FROM {TARGET_TABLE}")
        existing_indexes = {row[2] for row in cursor.fetchall()}
        for index_name, index_sql in required_indexes.items():
            if index_name not in existing_indexes:
                cursor.execute(index_sql.format(table=TARGET_TABLE))

    conn.commit()


# =====================================
# 8. 读取源表
# =====================================
def fetch_source_data(conn):
    sql = f"""
        WITH ranked_fat_guarantee AS (
            SELECT
                g.source_id,
                g.operator_symbol,
                g.metric_value,
                g.metric_unit,
                g.basis,
                g.raw_text,
                ROW_NUMBER() OVER (
                    PARTITION BY g.source_id
                    ORDER BY
                        CASE WHEN g.basis = '干物质' THEN 1 ELSE 0 END DESC,
                        g.id DESC
                ) AS rn
            FROM csv_labeling.product_guarantee g
            WHERE g.metric_name = '粗脂肪'
        )
        SELECT
            s.id,
            s.formula_id,
            s.id AS source_row_id,
            s.source_id,
            s.brand,
            s.product_name,
            s.image_path,
            s.image_name,
            s.file_sha256,
            s.parse_batch_id,
            s.parse_ts AS source_parse_ts,
            s.updated_ts AS source_updated_ts,
            s.ingredient_composition,
            rg.metric_value AS guarantee_crude_fat_value,
            rg.metric_unit AS guarantee_crude_fat_unit,
            rg.operator_symbol AS guarantee_crude_fat_operator,
            rg.basis AS guarantee_crude_fat_basis,
            rg.raw_text AS guarantee_crude_fat_raw_text
        FROM {SOURCE_TABLE} s
        LEFT JOIN ranked_fat_guarantee rg
          ON rg.source_id = s.source_id
         AND rg.rn = 1
        LEFT JOIN {TARGET_TABLE} t
          ON t.source_row_id = s.id
          OR (s.source_id IS NOT NULL AND t.source_id = s.source_id)
        WHERE s.ingredient_composition IS NOT NULL
          AND TRIM(s.ingredient_composition) <> ''
          AND (
              t.id IS NULL
              OR t.source_updated_ts IS NULL
              OR s.updated_ts > t.source_updated_ts
              OR (
                  rg.metric_value IS NOT NULL
                  AND (
                      t.guarantee_crude_fat_value IS NULL
                      OR t.guarantee_crude_fat_value <> rg.metric_value
                      OR COALESCE(t.guarantee_crude_fat_unit, '') <> COALESCE(rg.metric_unit, '')
                      OR COALESCE(t.guarantee_crude_fat_operator, '') <> COALESCE(rg.operator_symbol, '')
                      OR COALESCE(t.guarantee_crude_fat_basis, '') <> COALESCE(rg.basis, '')
                      OR COALESCE(t.guarantee_crude_fat_raw_text, '') <> COALESCE(rg.raw_text, '')
                  )
              )
          )
    """
    df = pd.read_sql(sql, conn)

    text_columns = [
        "brand",
        "product_name",
        "image_path",
        "image_name",
        "file_sha256",
        "parse_batch_id",
        "ingredient_composition",
        "guarantee_crude_fat_unit",
        "guarantee_crude_fat_operator",
        "guarantee_crude_fat_basis",
        "guarantee_crude_fat_raw_text",
    ]
    for column in text_columns:
        df[column] = df[column].apply(normalize_nullable_text)

    df["brand"] = df.apply(
        lambda row: correct_brand(
            row.get("brand"),
            row.get("product_name"),
            row.get("image_name"),
            row.get("image_path"),
        ),
        axis=1,
    )
    df["source_table"] = SOURCE_TABLE
    df["product_key"] = df.apply(build_product_key, axis=1)
    return df


# =====================================
# 9. 写入结果表
# =====================================
def upsert_results(conn, df_result):
    if "source_id" in df_result.columns and not df_result.empty:
        with_source_id = df_result[df_result["source_id"].notna()].copy()
        without_source_id = df_result[df_result["source_id"].isna()].copy()
        if not with_source_id.empty:
            sort_columns = [col for col in ("source_updated_ts", "id") if col in with_source_id.columns]
            if sort_columns:
                with_source_id = with_source_id.sort_values(sort_columns)
            with_source_id = with_source_id.drop_duplicates(subset=["source_id"], keep="last")
        df_result = pd.concat([with_source_id, without_source_id], ignore_index=True)

    sql = f"""
        INSERT INTO {TARGET_TABLE} (
            id,
            formula_id,
            source_row_id,
            source_id,
            source_table,
            product_key,
            brand,
            product_name,
            image_path,
            image_name,
            file_sha256,
            parse_batch_id,
            source_parse_ts,
            source_updated_ts,
            ingredient_composition,
            fat_sources,
            fat_source_types,
            antioxidant_sources,
            antioxidant_types,
            micronutrient_sources,
            micronutrient_types,
            omega6_sources,
            omega3_sources,
            guarantee_crude_fat_value,
            guarantee_crude_fat_unit,
            guarantee_crude_fat_operator,
            guarantee_crude_fat_basis,
            guarantee_crude_fat_raw_text,
            needs_review,
            review_reason
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            formula_id = VALUES(formula_id),
            source_row_id = VALUES(source_row_id),
            source_id = VALUES(source_id),
            source_table = VALUES(source_table),
            product_key = VALUES(product_key),
            brand = VALUES(brand),
            product_name = VALUES(product_name),
            image_path = VALUES(image_path),
            image_name = VALUES(image_name),
            file_sha256 = VALUES(file_sha256),
            parse_batch_id = VALUES(parse_batch_id),
            source_parse_ts = VALUES(source_parse_ts),
            source_updated_ts = VALUES(source_updated_ts),
            ingredient_composition = VALUES(ingredient_composition),
            fat_sources = VALUES(fat_sources),
            fat_source_types = VALUES(fat_source_types),
            antioxidant_sources = VALUES(antioxidant_sources),
            antioxidant_types = VALUES(antioxidant_types),
            micronutrient_sources = VALUES(micronutrient_sources),
            micronutrient_types = VALUES(micronutrient_types),
            omega6_sources = VALUES(omega6_sources),
            omega3_sources = VALUES(omega3_sources),
            guarantee_crude_fat_value = VALUES(guarantee_crude_fat_value),
            guarantee_crude_fat_unit = VALUES(guarantee_crude_fat_unit),
            guarantee_crude_fat_operator = VALUES(guarantee_crude_fat_operator),
            guarantee_crude_fat_basis = VALUES(guarantee_crude_fat_basis),
            guarantee_crude_fat_raw_text = VALUES(guarantee_crude_fat_raw_text),
            needs_review = VALUES(needs_review),
            review_reason = VALUES(review_reason)
    """

    data = [
        tuple(mysql_safe_value(value) for value in (
            row["id"],
            row["formula_id"],
            row["source_row_id"],
            row["source_id"],
            row["source_table"],
            row["product_key"],
            row["brand"],
            row["product_name"],
            row["image_path"],
            row["image_name"],
            row["file_sha256"],
            row["parse_batch_id"],
            row["source_parse_ts"].to_pydatetime() if pd.notna(row["source_parse_ts"]) else None,
            row["source_updated_ts"].to_pydatetime() if pd.notna(row["source_updated_ts"]) else None,
            row["ingredient_composition"],
            row["fat_sources"],
            row["fat_source_types"],
            row["antioxidant_sources"],
            row["antioxidant_types"],
            row["micronutrient_sources"],
            row["micronutrient_types"],
            row["omega6_sources"],
            row["omega3_sources"],
            row["guarantee_crude_fat_value"],
            row["guarantee_crude_fat_unit"],
            row["guarantee_crude_fat_operator"],
            row["guarantee_crude_fat_basis"],
            row["guarantee_crude_fat_raw_text"],
            row["needs_review"],
            row["review_reason"],
        ))
        for _, row in df_result.iterrows()
    ]

    with conn.cursor() as cursor:
        ids = [
            int(value)
            for value in df_result["id"].dropna().tolist()
            if str(value).strip() and str(value).replace(".", "", 1).isdigit()
        ]
        source_ids = [
            int(value)
            for value in df_result["source_id"].dropna().tolist()
            if str(value).strip() and str(value).replace(".", "", 1).isdigit()
        ]
        if ids or source_ids:
            clauses = []
            params = []
            if ids:
                clauses.append("id IN (" + ",".join(["%s"] * len(ids)) + ")")
                params.extend(ids)
            if source_ids:
                clauses.append("source_id IN (" + ",".join(["%s"] * len(source_ids)) + ")")
                params.extend(source_ids)
            cursor.execute(f"DELETE FROM {TARGET_TABLE} WHERE {' OR '.join(clauses)}", params)
        cursor.executemany(sql, data)
    conn.commit()


# =====================================
# 10. 主流程
# =====================================
def main():
    conn = pymysql.connect(**DB_CONFIG)
    try:
        ensure_target_table(conn)
        df = fetch_source_data(conn)

        if df.empty:
            print("没有可处理的数据。")
            return

        feature_df = df["ingredient_composition"].apply(
            lambda x: pd.Series(parse_material_features(x))
        )
        df_result = pd.concat([df, feature_df], axis=1)

        upsert_results(conn, df_result)

        print(f"处理完成，共写入/更新 {len(df_result)} 条。")
        print(df_result.head(10).to_string(index=False))

    finally:
        conn.close()


if __name__ == "__main__":
    main()
