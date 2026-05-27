import argparse
import os
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, create_engine, text


# =========================================================
# 1. 数据库配置
# =========================================================

DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "protein_feature_platform"),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
}


# =========================================================
# 2. 模型配置
# =========================================================

# 软便 M2：加入 p_form / p_bulk / p_buffer / q_feed / q_scfa，不使用 g 字段
# FAT_B 表示脂肪分采用互斥机制项：油脂负载 / Omega 失衡 / 来源复杂度各计入一次。
SCORE_MODEL_VERSION = "SOFT_STOOL_M2_PQ_NO_G_FAT_B"
REPLACE_SCORE_MODEL_VERSIONS = [
    SCORE_MODEL_VERSION,
    "SOFT_STOOL_M2_PQ_NO_G",
]

# 软便专用特征版本，对应 sku_feature_input.feature_version
DEFAULT_FEATURE_VERSION = "soft_v1"

# 沿用老表字段：软便解释分仍写入 black_chin_explain_score
EXPLAIN_SCORE_OUTPUT_COL = "black_chin_explain_score"

# 如果存在脂肪特征表，则优先用修正后的 fat_score
FAT_FEATURE_TABLE = "catfood_fat_material_features_scored"
FIBER_FEATURE_TABLE = "catfood_fiber_feature_score"


# =========================================================
# 3. 工具函数
# =========================================================

def get_engine():
    url = (
        f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
        f"?charset={DB_CONFIG['charset']}"
    )
    return create_engine(url)


def get_table_columns(engine, table_name):
    """读取表字段；如果表不存在，返回空集合。"""
    sql = """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :schema_name
          AND TABLE_NAME = :table_name
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text(sql),
            {"schema_name": DB_CONFIG["database"], "table_name": table_name},
        ).fetchall()
    return {row[0] for row in rows}


def calculate_reference_percentile(value, reference_values):
    """
    计算 value 在 reference_values 中的历史分位数。
    例如 value 高于或等于 80% 的历史 SKU，则返回 0.80。
    """
    reference_values = pd.to_numeric(reference_values, errors="coerce").dropna()

    if pd.isna(value) or len(reference_values) == 0:
        return np.nan

    return (reference_values <= value).mean()


def assign_risk_level(percentile):
    """根据历史分位数划分软便风险等级。"""
    if pd.isna(percentile):
        return "未知风险"

    if percentile >= 0.80:
        return "高软便配方风险"
    elif percentile >= 0.60:
        return "中高软便配方风险"
    elif percentile >= 0.40:
        return "中软便配方风险"
    elif percentile >= 0.20:
        return "中低软便配方风险"
    else:
        return "低软便配方风险"


def assign_batch_priority_level(batch_percentile):
    """本批新品内部优先级。"""
    if pd.isna(batch_percentile):
        return "无批次优先级"

    if batch_percentile >= 0.80:
        return "本批高优先级"
    elif batch_percentile >= 0.60:
        return "本批中高优先级"
    elif batch_percentile >= 0.40:
        return "本批中优先级"
    elif batch_percentile >= 0.20:
        return "本批中低优先级"
    else:
        return "本批低优先级"


def _safe_float(value):
    try:
        if pd.isna(value):
            return np.nan
        return float(value)
    except Exception:
        return np.nan


def value_ge(row, key, threshold):
    value = _safe_float(row.get(key, np.nan))
    return pd.notna(value) and value >= threshold


def value_le(row, key, threshold):
    value = _safe_float(row.get(key, np.nan))
    return pd.notna(value) and value <= threshold


def merge_tag_list(tags, extra_tags_text=None):
    """合并逗号分隔标签，并去重保序。"""
    seen = set()
    result = []

    for tag in tags:
        tag = str(tag).strip()
        if tag and tag not in seen:
            result.append(tag)
            seen.add(tag)

    if pd.notna(extra_tags_text):
        for tag in str(extra_tags_text).replace("，", ",").split(","):
            tag = tag.strip()
            if tag and tag not in seen:
                result.append(tag)
                seen.add(tag)

    return result


def format_tag_list(tags, fallback=None):
    cleaned = merge_tag_list(tags)
    if not cleaned and fallback:
        cleaned = [fallback]
    return ",".join(cleaned)


def validate_required_columns(df, required_columns, table_name):
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        raise ValueError(f"{table_name} 缺少必要字段: {missing}")


# =========================================================
# 4. 读取 SKU 特征和基准池
# =========================================================

def build_sku_feature_sql(engine):
    """
    构造 sku_feature_input 查询 SQL。

    软便 M2 必需字段：
    - protein_score / carb_score / fat_score / antioxidant_score
    - p_form_score / p_bulk_score / p_buffer_score / q_feed_score / q_scfa_score

    注意：fiber_score 不再直接进入核心公式，但仍可保留用于兼容观察。
    """
    return f"""
        SELECT
            s.sku_id,
            s.sku_name,
            s.brand_name,
            s.batch_id,
            s.data_type,
            s.feature_version,

            s.protein_score,
            s.carb_score,
            s.fiber_score,
            COALESCE(f.fat_score, s.fat_score) AS fat_score,
            s.prebiotic_score,
            s.antioxidant_score,

            fiber.p_form_score,
            fiber.p_bulk_score,
            COALESCE(s.p_buffer, fiber.p_buffer_score) AS p_buffer_score,
            COALESCE(s.q_feed, fiber.q_feed_score) AS q_feed_score,
            COALESCE(s.q_scfa, fiber.q_scfa_score) AS q_scfa_score

        FROM sku_feature_input s
        LEFT JOIN {FAT_FEATURE_TABLE} f
          ON s.sku_id = f.product_key
          OR (
              COALESCE(TRIM(s.sku_name), '') <> ''
              AND COALESCE(TRIM(s.sku_name), '') = COALESCE(TRIM(f.product_name), '')
          )
        LEFT JOIN {FIBER_FEATURE_TABLE} fiber
          ON s.sku_id = fiber.product_key
          OR (
              COALESCE(TRIM(s.sku_name), '') <> ''
              AND COALESCE(TRIM(s.sku_name), '') = COALESCE(TRIM(fiber.product_name), '')
          )
        WHERE s.feature_version = :feature_version
    """


def get_active_reference_pool_version(engine, feature_version):
    """
    优先取当前 feature_version 下 is_active_pool = 1 的最新 reference_pool_version。
    如果没有 active 版本，则回退到当前 feature_version 下最新的基准池版本。

    如果你同时跑黑下巴和软便，强烈建议命令行显式传：
    --reference_pool_version SOFT_STOOL_REF_V2
    """
    sql = """
        SELECT reference_pool_version
        FROM reference_sku_pool_snapshot
        WHERE is_active_pool = 1
          AND feature_version = :feature_version
        GROUP BY reference_pool_version
        ORDER BY MAX(pool_created_at) DESC
        LIMIT 1
    """
    df = pd.read_sql(text(sql), engine, params={"feature_version": feature_version})

    if not df.empty:
        return df.iloc[0]["reference_pool_version"]

    fallback_sql = """
        SELECT reference_pool_version
        FROM reference_sku_pool_snapshot
        WHERE feature_version = :feature_version
        GROUP BY reference_pool_version
        ORDER BY MAX(pool_created_at) DESC
        LIMIT 1
    """
    df = pd.read_sql(
        text(fallback_sql),
        engine,
        params={"feature_version": feature_version},
    )

    if df.empty:
        raise ValueError(f"没有找到 feature_version={feature_version} 的基准池版本")

    print(f"当前特征版本没有 active 基准池，自动使用最新基准池: {df.iloc[0]['reference_pool_version']}")

    return df.iloc[0]["reference_pool_version"]


def load_sku_feature_input(engine, feature_version):
    sql = build_sku_feature_sql(engine)
    return pd.read_sql(text(sql), engine, params={"feature_version": feature_version})


def load_reference_pool(engine, reference_pool_version, feature_version):
    """
    沿用 reference_sku_pool_snapshot + sku_feature_input 的老 join 方式。
    通过 reference_pool_version + feature_version 区分软便基准池。
    """
    sql = f"""
        SELECT
            f.sku_id,
            f.sku_name,
            f.brand_name,
            f.batch_id,
            f.data_type,
            f.feature_version,

            f.protein_score,
            f.carb_score,
            f.fiber_score,
            COALESCE(ff.fat_score, f.fat_score) AS fat_score,
            f.prebiotic_score,
            f.antioxidant_score,

            fiber.p_form_score,
            fiber.p_bulk_score,
            COALESCE(f.p_buffer, fiber.p_buffer_score) AS p_buffer_score,
            COALESCE(f.q_feed, fiber.q_feed_score) AS q_feed_score,
            COALESCE(f.q_scfa, fiber.q_scfa_score) AS q_scfa_score

        FROM reference_sku_pool_snapshot p
        JOIN sku_feature_input f
          ON p.sku_id = f.sku_id
         AND p.feature_version = f.feature_version
        LEFT JOIN {FAT_FEATURE_TABLE} ff
          ON f.sku_id = ff.product_key
        LEFT JOIN {FIBER_FEATURE_TABLE} fiber
          ON f.sku_id = fiber.product_key
        WHERE p.reference_pool_version = :reference_pool_version
          AND p.feature_version = :feature_version
    """

    return pd.read_sql(
        text(sql),
        engine,
        params={
            "reference_pool_version": reference_pool_version,
            "feature_version": feature_version,
        },
    )


# =========================================================
# 5. 品牌软便反馈
# =========================================================

def load_brand_symptom_stats(engine, symptom_type="soft_stool"):
    """
    读取品牌软便反馈。

    当前软便品牌表：brand_soft_stool_stats。
    """
    cols = get_table_columns(engine, "brand_soft_stool_stats")

    if {
        "brand",
        "soft_stool_aggravation_count",
        "total_clue_count",
        "soft_stool_aggravation_ratio",
    }.issubset(cols):
        sql = """
            SELECT
                brand AS brand_name,
                'soft_stool' AS symptom_type,
                soft_stool_aggravation_count AS symptom_count,
                total_clue_count AS total_comment_count,
                soft_stool_aggravation_ratio AS symptom_rate,
                'soft_gt3_20260429' AS stat_version
            FROM brand_soft_stool_stats
        """
        return pd.read_sql(text(sql), engine)

    return pd.DataFrame(
        columns=[
            "brand_name",
            "symptom_type",
            "symptom_count",
            "total_comment_count",
            "symptom_rate",
            "stat_version",
        ]
    )


def assign_brand_soft_stool_level(row):
    """
    根据品牌软便占比生成品牌反馈等级。

    为了沿用老结果表：
    brand_black_chin_rate  实际存 brand_soft_stool_rate
    brand_black_chin_count 实际存 brand_soft_stool_count
    brand_black_chin_level 实际存 brand_soft_stool_level
    """
    rate = row.get("brand_black_chin_rate")
    total_count = row.get("brand_total_comment_count")

    if pd.isna(rate):
        return "无品牌反馈数据"

    if pd.isna(total_count) or total_count < 30:
        return "低可信品牌样本"

    if rate >= 0.15:
        return "高品牌软便反馈"
    elif rate >= 0.10:
        return "中高品牌软便反馈"
    elif rate >= 0.05:
        return "中品牌软便反馈"
    else:
        return "低品牌软便反馈"


def attach_brand_symptom(scored_df, brand_symptom_df):
    """把品牌软便占比 join 到结果里。"""
    df = scored_df.copy()

    if brand_symptom_df.empty:
        df["brand_black_chin_rate"] = np.nan
        df["brand_black_chin_count"] = np.nan
        df["brand_total_comment_count"] = np.nan
        df["brand_symptom_stat_version"] = None
        df["brand_black_chin_level"] = "无品牌反馈数据"
        return df

    brand_df = brand_symptom_df.copy()

    brand_df = brand_df.rename(
        columns={
            "symptom_count": "brand_black_chin_count",
            "total_comment_count": "brand_total_comment_count",
            "symptom_rate": "brand_black_chin_rate",
            "stat_version": "brand_symptom_stat_version",
        }
    )

    brand_df = (
        brand_df.sort_values("brand_symptom_stat_version")
        .drop_duplicates(subset=["brand_name"], keep="last")
    )

    df = df.merge(
        brand_df[
            [
                "brand_name",
                "brand_black_chin_count",
                "brand_total_comment_count",
                "brand_black_chin_rate",
                "brand_symptom_stat_version",
            ]
        ],
        on="brand_name",
        how="left",
    )

    df["brand_black_chin_level"] = df.apply(assign_brand_soft_stool_level, axis=1)

    return df


# =========================================================
# 6. 软便 M2 核心计算逻辑：p/q，无 g
# =========================================================

def add_derived_pq_scores(df):
    """
    根据 p_form / p_bulk / p_buffer / q_feed / q_scfa 生成机制中间分。

    1）stool_forming_support_score:
       成形保护能力。p_form 权重最高，p_bulk 次之，p_buffer 辅助。

    2）microbiome_support_score:
       菌群代谢支持。q_scfa 更偏保护，q_feed 是供菌底物。

    3）feed_excess_pressure_score:
       不使用 g 的情况下，用 max(q_feed - q_scfa, 0) 表示“供菌底物相对过量”。
       逻辑：如果 q_feed 很高，但 q_scfa 支持不足，则更可能体现为发酵底物压力。
    """
    result = df.copy()

    pq_cols = [
        "p_form_score",
        "p_bulk_score",
        "p_buffer_score",
        "q_feed_score",
        "q_scfa_score",
    ]

    for col in pq_cols:
        if col not in result.columns:
            result[col] = np.nan
        result[col] = pd.to_numeric(result[col], errors="coerce")

    result["stool_forming_support_score"] = (
        0.45 * result["p_form_score"]
        + 0.30 * result["p_bulk_score"]
        + 0.25 * result["p_buffer_score"]
    )

    result["microbiome_support_score"] = (
        0.55 * result["q_scfa_score"]
        + 0.45 * result["q_feed_score"]
    )

    result["feed_excess_pressure_score"] = (
        result["q_feed_score"] - result["q_scfa_score"]
    ).clip(lower=0)

    return result


def get_percentile_config(df):
    """
    软便 M2 使用的分位数字段。
    """
    return {
        # 配方结构压力
        "protein_score": "protein_history_percentile",
        "carb_score": "carb_history_percentile",
        "fat_score": "fat_history_percentile",
        "antioxidant_score": "antioxidant_history_percentile",

        # p/q 原始机制分
        "p_form_score": "p_form_history_percentile",
        "p_bulk_score": "p_bulk_history_percentile",
        "p_buffer_score": "p_buffer_history_percentile",
        "q_feed_score": "q_feed_history_percentile",
        "q_scfa_score": "q_scfa_history_percentile",

        # p/q 派生机制分
        "stool_forming_support_score": "stool_forming_support_history_percentile",
        "microbiome_support_score": "microbiome_support_history_percentile",
        "feed_excess_pressure_score": "feed_excess_pressure_history_percentile",
    }


def add_percentile_columns(df, reference_df):
    result = df.copy()
    reference = reference_df.copy()
    percentile_config = get_percentile_config(result)

    for raw_col, pct_col in percentile_config.items():
        if raw_col not in result.columns:
            result[raw_col] = np.nan
        if raw_col not in reference.columns:
            reference[raw_col] = np.nan

        result[raw_col] = pd.to_numeric(result[raw_col], errors="coerce")
        reference[raw_col] = pd.to_numeric(reference[raw_col], errors="coerce")

        result[pct_col] = result[raw_col].apply(
            lambda x: calculate_reference_percentile(x, reference[raw_col])
        )

    return result


def calculate_gut_buffer_score(df):
    """
    肠道缓冲分。

    不再使用 prebiotic_history_percentile 作为核心公式项；
    因为 q_feed / q_scfa 已经拆解了益生元相关功能。
    """
    return (
        0.60 * df["stool_forming_support_history_percentile"]
        + 0.30 * df["microbiome_support_history_percentile"]
        + 0.10 * df["antioxidant_history_percentile"]
    )


def calculate_soft_stool_explain_score(df):
    """
    软便解释分 M2，无 g 字段。

    风险项：
    - carb_score：主碳水/淀粉/豆类结构压力
    - protein_score：蛋白消化压力
    - fat_score：脂肪消化负担
    - feed_excess_pressure_score：供菌底物相对过量压力，替代 g

    保护项：
    - gut_buffer_score：成形支持 + 菌群支持 + 抗氧化支持
    """
    return (
        0.26 * df["carb_history_percentile"]
        + 0.22 * df["protein_history_percentile"]
        + 0.18 * df["fat_history_percentile"]
        + 0.19 * df["feed_excess_pressure_history_percentile"]
        - 0.25 * df["buffer_score"]
    )


def add_score_contributions(df):
    """拆解主要贡献项。"""
    result = df.copy()

    result["carb_risk_contribution"] = 0.26 * result["carb_history_percentile"]
    result["protein_risk_contribution"] = 0.22 * result["protein_history_percentile"]
    result["fat_risk_contribution"] = 0.18 * result["fat_history_percentile"]
    result["feed_excess_risk_contribution"] = 0.19 * result["feed_excess_pressure_history_percentile"]
    result["buffer_protection_contribution"] = -0.25 * result["buffer_score"]

    return result


def assign_main_risk_driver(row):
    """给出主要拉高项。"""
    drivers = {
        "碳水结构压力": row.get("carb_risk_contribution", 0),
        "蛋白消化压力": row.get("protein_risk_contribution", 0),
        "脂肪消化负担": row.get("fat_risk_contribution", 0),
        "供菌底物相对过量": row.get("feed_excess_risk_contribution", 0),
    }

    cleaned = {}
    for k, v in drivers.items():
        try:
            cleaned[k] = 0 if pd.isna(v) else float(v)
        except Exception:
            cleaned[k] = 0

    if not cleaned or max(cleaned.values()) <= 0:
        return "暂无明显主导项"

    main_driver = max(cleaned, key=cleaned.get)

    weak_buffers = []
    if row.get("stool_forming_support_history_percentile", np.nan) <= 0.30:
        weak_buffers.append("成形支持弱")
    if row.get("microbiome_support_history_percentile", np.nan) <= 0.30:
        weak_buffers.append("菌群支持弱")
    if row.get("p_buffer_history_percentile", np.nan) <= 0.30:
        weak_buffers.append("刺激缓冲弱")

    if weak_buffers:
        return main_driver + "主导 + " + "/".join(weak_buffers)

    return main_driver + "主导"


def calculate_current_pool_explain_scores(current_pool_df, reference_df):
    """
    给当前全量池计算软便 explain_score。
    各特征分位数依然基于 reference_df 计算。
    """
    df = add_derived_pq_scores(current_pool_df.copy())
    reference_df = add_derived_pq_scores(reference_df.copy())

    score_cols = [
        "protein_score",
        "carb_score",
        "fat_score",
        "antioxidant_score",
        "p_form_score",
        "p_bulk_score",
        "p_buffer_score",
        "q_feed_score",
        "q_scfa_score",
        "stool_forming_support_score",
        "microbiome_support_score",
        "feed_excess_pressure_score",
    ]

    for col in score_cols:
        if col not in df.columns:
            df[col] = np.nan
        if col not in reference_df.columns:
            reference_df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
        reference_df[col] = pd.to_numeric(reference_df[col], errors="coerce")

    df = add_percentile_columns(df, reference_df)
    df["buffer_score"] = calculate_gut_buffer_score(df)
    df[EXPLAIN_SCORE_OUTPUT_COL] = calculate_soft_stool_explain_score(df)

    return df


def calculate_scores(target_df, reference_df, current_pool_df=None):
    """
    target_df:
        要计算软便风险的 SKU，可以是历史 SKU，也可以是一批新品。

    reference_df:
        软便历史基准池。

    current_pool_df:
        当前软便全量池，用来计算 current_pool_percentile。
    """
    df = add_derived_pq_scores(target_df.copy())
    reference_df = add_derived_pq_scores(reference_df.copy())

    score_cols = [
        "protein_score",
        "carb_score",
        "fat_score",
        "antioxidant_score",
        "p_form_score",
        "p_bulk_score",
        "p_buffer_score",
        "q_feed_score",
        "q_scfa_score",
        "stool_forming_support_score",
        "microbiome_support_score",
        "feed_excess_pressure_score",
    ]

    for col in score_cols:
        if col not in df.columns:
            df[col] = np.nan
        if col not in reference_df.columns:
            reference_df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
        reference_df[col] = pd.to_numeric(reference_df[col], errors="coerce")

    # 1. 计算目标 SKU 各特征在软便基准池中的分位数
    df = add_percentile_columns(df, reference_df)

    # 2. 肠道缓冲分
    df["buffer_score"] = calculate_gut_buffer_score(df)

    # 3. 软便解释分；为了兼容老表，写入 black_chin_explain_score
    df[EXPLAIN_SCORE_OUTPUT_COL] = calculate_soft_stool_explain_score(df)

    # 4. reference_df 自身也计算 explain_score，作为历史风险分位数参照
    ref_scored = add_percentile_columns(reference_df.copy(), reference_df.copy())
    ref_scored["buffer_score"] = calculate_gut_buffer_score(ref_scored)
    ref_scored[EXPLAIN_SCORE_OUTPUT_COL] = calculate_soft_stool_explain_score(ref_scored)

    # 5. 历史分位数和风险等级
    df["history_percentile"] = df[EXPLAIN_SCORE_OUTPUT_COL].apply(
        lambda x: calculate_reference_percentile(
            x,
            ref_scored[EXPLAIN_SCORE_OUTPUT_COL],
        )
    )
    df["history_risk_level"] = df["history_percentile"].apply(assign_risk_level)

    # 6. 当前全量分位数
    if current_pool_df is None:
        current_pool_df = pd.concat([reference_df, target_df], ignore_index=True)

    current_pool_scored = calculate_current_pool_explain_scores(
        current_pool_df=current_pool_df,
        reference_df=reference_df,
    )

    df["current_pool_percentile"] = df[EXPLAIN_SCORE_OUTPUT_COL].apply(
        lambda x: calculate_reference_percentile(
            x,
            current_pool_scored[EXPLAIN_SCORE_OUTPUT_COL],
        )
    )
    df["current_pool_risk_level"] = df["current_pool_percentile"].apply(assign_risk_level)

    # 7. 贡献项和主导项
    df = add_score_contributions(df)
    df["main_risk_driver"] = df.apply(assign_main_risk_driver, axis=1)

    # 8. 本批内部排名
    if "batch_id" in df.columns:
        df["batch_rank"] = (
            df.groupby("batch_id")[EXPLAIN_SCORE_OUTPUT_COL]
            .rank(method="dense", ascending=False)
            .astype("Int64")
        )

        df["batch_percentile"] = (
            df.groupby("batch_id")[EXPLAIN_SCORE_OUTPUT_COL]
            .rank(pct=True, method="average")
        )

        df["batch_priority_level"] = df["batch_percentile"].apply(assign_batch_priority_level)
    else:
        df["batch_rank"] = np.nan
        df["batch_percentile"] = np.nan
        df["batch_priority_level"] = "无批次优先级"

    return df


# =========================================================
# 7. 标签和最终类型
# =========================================================

def build_main_reason_tags(row):
    """软便主解释标签。"""
    tags = []

    if value_ge(row, "carb_history_percentile", 0.70):
        tags.append("碳水结构压力偏高")

    if value_ge(row, "protein_history_percentile", 0.70):
        tags.append("蛋白消化压力偏高")

    if value_ge(row, "fat_history_percentile", 0.70):
        tags.append("脂肪消化负担偏高")

    if value_ge(row, "feed_excess_pressure_history_percentile", 0.70):
        tags.append("供菌底物相对过量")

    if value_le(row, "stool_forming_support_history_percentile", 0.30):
        tags.append("便便成形支持不足")

    if value_le(row, "microbiome_support_history_percentile", 0.30):
        tags.append("菌群代谢支持不足")

    if value_le(row, "buffer_score", 0.30):
        tags.append("肠道缓冲支持不足")

    return format_tag_list(tags, fallback="暂无明显软便主风险标签")


def build_support_reason_tags(row):
    """软便辅助观察标签。"""
    tags = []

    if value_le(row, "p_form_history_percentile", 0.30):
        tags.append("吸水成形能力偏弱")

    if value_le(row, "p_bulk_history_percentile", 0.30):
        tags.append("粪便骨架支持偏弱")

    if value_le(row, "p_buffer_history_percentile", 0.30):
        tags.append("刺激缓冲能力偏弱")

    if value_ge(row, "q_feed_history_percentile", 0.70):
        tags.append("供菌底物偏多")

    if value_le(row, "q_scfa_history_percentile", 0.30):
        tags.append("SCFA支持偏弱")

    if value_ge(row, "q_feed_history_percentile", 0.70) and value_le(row, "q_scfa_history_percentile", 0.40):
        tags.append("供菌强但代谢支持不足")

    return format_tag_list(tags, fallback="暂无明显辅助观察标签")


def build_fat_detail_tags(row):
    """
    兼容老字段 fat_detail_tags。
    """
    tags = []

    if value_ge(row, "fat_history_percentile", 0.70):
        tags.append("脂肪负担可能影响便便成形")

    return format_tag_list(tags, fallback="暂无明显脂肪相关软便风险")


def build_all_reason_tags(row):
    tags = []

    for field, fallback in [
        ("main_reason_tags", "暂无明显软便主风险标签"),
        ("support_reason_tags", "暂无明显辅助观察标签"),
        ("fat_detail_tags", "暂无明显脂肪相关软便风险"),
    ]:
        text_value = row.get(field, "")
        if pd.isna(text_value):
            continue
        for tag in str(text_value).replace("，", ",").split(","):
            tag = tag.strip()
            if tag and tag != fallback:
                tags.append(tag)

    return format_tag_list(tags, fallback="暂无明显软便高风险标签")


def assign_final_risk_type(row):
    """
    软便最终风险类型：
    - 有品牌软便反馈：品牌反馈 × SKU 配方风险
    - 无品牌反馈：冷启动配方风险
    """
    brand_level = row.get("brand_black_chin_level")
    history_level = row.get("history_risk_level")

    high_brand = brand_level in ["高品牌软便反馈", "中高品牌软便反馈"]
    mid_or_low_brand = brand_level in ["中品牌软便反馈", "低品牌软便反馈"]
    low_brand = brand_level == "低品牌软便反馈"

    high_sku = history_level in ["高软便配方风险", "中高软便配方风险"]
    low_sku = history_level in ["中低软便配方风险", "低软便配方风险"]

    if brand_level == "无品牌反馈数据":
        if history_level == "高软便配方风险":
            return "冷启动高软便风险预警型"
        elif history_level == "中高软便配方风险":
            return "冷启动中高软便风险型"
        elif history_level == "中软便配方风险":
            return "冷启动软便观察型"
        elif history_level in ["中低软便配方风险", "低软便配方风险"]:
            return "冷启动低软便风险型"
        else:
            return "冷启动未知型"

    if brand_level == "低可信品牌样本":
        if high_sku:
            return "低可信高软便配方风险观察型"
        else:
            return "低可信软便观察型"

    if high_brand and high_sku:
        return "高可信高软便风险型"

    if high_brand and low_sku:
        return "品牌软便反馈驱动型"

    if mid_or_low_brand and high_sku:
        return "潜在软便预警型"

    if low_brand and low_sku:
        return "低软便风险型"

    return "软便中间观察型"


def finalize_result(df, reference_pool_version, feature_version):
    result = df.copy()

    result["reference_pool_version"] = reference_pool_version
    result["feature_version"] = feature_version
    result["score_model_version"] = SCORE_MODEL_VERSION

    result["final_risk_type"] = result.apply(assign_final_risk_type, axis=1)

    result["main_reason_tags"] = result.apply(build_main_reason_tags, axis=1)
    result["support_reason_tags"] = result.apply(build_support_reason_tags, axis=1)
    result["fat_detail_tags"] = result.apply(build_fat_detail_tags, axis=1)
    result["all_reason_tags"] = result.apply(build_all_reason_tags, axis=1)
    result["reason_tags"] = result["main_reason_tags"]

    result["calculated_at"] = datetime.now()

    return result


def build_reference_monitor_batch_id(batch_id, reference_pool_version):
    if batch_id:
        return f"{batch_id}__soft_stool_m2_reference_monitor"
    return f"{reference_pool_version}__soft_stool_m2_reference_monitor"


def calculate_reference_monitor_results(
    reference_df,
    reference_pool_version,
    feature_version,
    current_pool_df,
    brand_symptom_df,
    monitor_batch_id,
):
    monitor_df = reference_df.copy()
    monitor_df["batch_id"] = monitor_batch_id
    monitor_df["data_type"] = "reference_pool_monitor"

    scored_df = calculate_scores(
        target_df=monitor_df,
        reference_df=reference_df,
        current_pool_df=current_pool_df,
    )
    scored_df = attach_brand_symptom(scored_df, brand_symptom_df)

    return finalize_result(
        scored_df,
        reference_pool_version=reference_pool_version,
        feature_version=feature_version,
    )


# =========================================================
# 8. 写入结果表
# =========================================================

def ensure_result_table_schema(engine):
    """
    沿用老 sku_risk_score_result 表，只补齐脚本会写入的字段。
    不新增 symptom_type，不新增 soft_stool_explain_score。
    """
    expected_columns = {
        "brand_black_chin_rate": "DECIMAL(10,4) NULL",
        "brand_black_chin_count": "INT NULL",
        "brand_total_comment_count": "INT NULL",
        "brand_black_chin_level": "VARCHAR(50) NULL",
        "brand_symptom_stat_version": "VARCHAR(50) NULL",
        "main_reason_tags": "VARCHAR(500) NULL",
        "support_reason_tags": "VARCHAR(500) NULL",
        "fat_detail_tags": "VARCHAR(500) NULL",
        "all_reason_tags": "VARCHAR(1000) NULL",
    }

    sql = """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :schema_name
          AND TABLE_NAME = 'sku_risk_score_result'
    """

    with engine.begin() as conn:
        existing_columns = {
            row[0]
            for row in conn.execute(text(sql), {"schema_name": DB_CONFIG["database"]})
        }

        for column_name, column_type in expected_columns.items():
            if column_name not in existing_columns:
                conn.execute(
                    text(
                        f"ALTER TABLE sku_risk_score_result "
                        f"ADD COLUMN {column_name} {column_type}"
                    )
                )


def delete_existing_results(
    engine,
    batch_id,
    reference_pool_version,
    feature_version,
    score_model_version,
):
    """
    防止同一批次重复插入。
    通过 score_model_version 区分黑下巴与软便 M2。
    """
    sql = """
        DELETE FROM sku_risk_score_result
        WHERE batch_id = :batch_id
          AND reference_pool_version = :reference_pool_version
          AND feature_version = :feature_version
          AND score_model_version = :score_model_version
    """

    with engine.begin() as conn:
        conn.execute(
            text(sql),
            {
                "batch_id": batch_id,
                "reference_pool_version": reference_pool_version,
                "feature_version": feature_version,
                "score_model_version": score_model_version,
            },
        )


def delete_existing_results_for_scope(
    engine,
    batch_ids,
    reference_pool_version,
    feature_version,
    score_model_versions,
):
    """
    重跑时按本次目标范围删除旧结果，避免全量或多批次重跑时重复追加。
    """
    cleaned_batch_ids = [
        str(batch_id)
        for batch_id in pd.Series(batch_ids).dropna().unique().tolist()
        if str(batch_id).strip()
    ]

    if not cleaned_batch_ids:
        return

    sql = """
        DELETE FROM sku_risk_score_result
        WHERE batch_id IN :batch_ids
          AND reference_pool_version = :reference_pool_version
          AND feature_version = :feature_version
          AND score_model_version IN :score_model_versions
    """

    with engine.begin() as conn:
        conn.execute(
            text(sql).bindparams(
                bindparam("batch_ids", expanding=True),
                bindparam("score_model_versions", expanding=True),
            ),
            {
                "batch_ids": cleaned_batch_ids,
                "reference_pool_version": reference_pool_version,
                "feature_version": feature_version,
                "score_model_versions": score_model_versions,
            },
        )


def write_results(engine, result_df):
    """
    写入 sku_risk_score_result。
    注意：软便解释分写入老字段 black_chin_explain_score。
    """
    insert_columns = [
        "sku_id",
        "sku_name",
        "brand_name",
        "batch_id",

        "reference_pool_version",
        "feature_version",
        "score_model_version",

        "buffer_score",
        "black_chin_explain_score",

        "history_percentile",
        "history_risk_level",

        "current_pool_percentile",
        "current_pool_risk_level",

        "batch_rank",
        "batch_percentile",
        "batch_priority_level",

        "brand_black_chin_rate",
        "brand_black_chin_count",
        "brand_total_comment_count",
        "brand_black_chin_level",
        "brand_symptom_stat_version",

        "final_risk_type",
        "reason_tags",
        "main_reason_tags",
        "support_reason_tags",
        "fat_detail_tags",
        "all_reason_tags",

        "calculated_at",
    ]

    for col in insert_columns:
        if col not in result_df.columns:
            result_df[col] = np.nan

    output_df = result_df[insert_columns].copy()
    output_df = output_df.replace({pd.NA: None, np.nan: None})

    ensure_result_table_schema(engine)

    output_df.to_sql(
        "sku_risk_score_result",
        con=engine,
        if_exists="append",
        index=False,
        chunksize=1000,
    )


# =========================================================
# 9. 主流程
# =========================================================

def run(
    batch_id=None,
    data_type=None,
    feature_version=DEFAULT_FEATURE_VERSION,
    reference_pool_version=None,
    recalculate_all=False,
    delete_before_insert=True,
    monitor_reference_pool=True,
):
    engine = get_engine()

    if reference_pool_version is None:
        reference_pool_version = get_active_reference_pool_version(engine, feature_version)

    print(f"使用基准池版本: {reference_pool_version}")
    print(f"使用特征版本: {feature_version}")
    print(f"使用评分模型版本: {SCORE_MODEL_VERSION}")
    print("注意：软便解释分将写入老表字段 black_chin_explain_score。")

    sku_df = load_sku_feature_input(engine, feature_version=feature_version)
    reference_df = load_reference_pool(
        engine,
        reference_pool_version=reference_pool_version,
        feature_version=feature_version,
    )
    brand_symptom_df = load_brand_symptom_stats(engine, symptom_type="soft_stool")

    required_feature_cols = [
        "sku_id",
        "sku_name",
        "brand_name",
        "batch_id",
        "data_type",
        "feature_version",
        "protein_score",
        "carb_score",
        "fat_score",
        "antioxidant_score",
        "p_form_score",
        "p_bulk_score",
        "p_buffer_score",
        "q_feed_score",
        "q_scfa_score",
    ]

    validate_required_columns(sku_df, required_feature_cols, "sku_feature_input")
    validate_required_columns(
        reference_df,
        [
            "sku_id",
            "protein_score",
            "carb_score",
            "fat_score",
            "antioxidant_score",
            "p_form_score",
            "p_bulk_score",
            "p_buffer_score",
            "q_feed_score",
            "q_scfa_score",
        ],
        "reference_sku_pool_snapshot + sku_feature_input",
    )

    if reference_df.empty:
        raise ValueError(f"基准池 {reference_pool_version} 没有 SKU，请检查 reference_sku_pool_snapshot")

    if recalculate_all:
        target_df = sku_df.copy()
        print("计算范围: 全量 SKU")
    else:
        target_df = sku_df.copy()

        if batch_id is not None:
            target_df = target_df[target_df["batch_id"] == batch_id]

        if data_type is not None:
            target_df = target_df[target_df["data_type"] == data_type]

        print(f"计算范围: batch_id={batch_id}, data_type={data_type}")

    if target_df.empty:
        print("没有需要计算的 SKU，流程结束。")
        return

    print(f"待计算 SKU 数量: {len(target_df)}")
    print(f"软便基准池 SKU 数量: {len(reference_df)}")
    print(f"品牌软便反馈记录数: {len(brand_symptom_df)}")

    current_pool_df = sku_df.copy()

    scored_df = calculate_scores(
        target_df=target_df,
        reference_df=reference_df,
        current_pool_df=current_pool_df,
    )

    scored_df = attach_brand_symptom(scored_df, brand_symptom_df)

    result_df = finalize_result(
        scored_df,
        reference_pool_version=reference_pool_version,
        feature_version=feature_version,
    )

    if delete_before_insert:
        delete_existing_results_for_scope(
            engine=engine,
            batch_ids=result_df["batch_id"],
            reference_pool_version=reference_pool_version,
            feature_version=feature_version,
            score_model_versions=REPLACE_SCORE_MODEL_VERSIONS,
        )

    write_results(engine, result_df)

    print("写入 sku_risk_score_result 完成。")
    print("\n软便 M2 最终风险类型分布：")
    print(result_df["final_risk_type"].value_counts(dropna=False))

    print("\n软便 M2 历史风险等级分布：")
    print(result_df["history_risk_level"].value_counts(dropna=False))

    print("\n软便 M2 主风险标签分布 Top 20：")
    print(result_df["main_reason_tags"].value_counts(dropna=False).head(20))

    if monitor_reference_pool and batch_id is not None:
        monitor_batch_id = build_reference_monitor_batch_id(
            batch_id=batch_id,
            reference_pool_version=reference_pool_version,
        )
        monitor_result_df = calculate_reference_monitor_results(
            reference_df=reference_df,
            reference_pool_version=reference_pool_version,
            feature_version=feature_version,
            current_pool_df=current_pool_df,
            brand_symptom_df=brand_symptom_df,
            monitor_batch_id=monitor_batch_id,
        )

        if delete_before_insert:
            delete_existing_results_for_scope(
                engine=engine,
                batch_ids=monitor_result_df["batch_id"],
                reference_pool_version=reference_pool_version,
                feature_version=feature_version,
                score_model_versions=REPLACE_SCORE_MODEL_VERSIONS,
            )

        write_results(engine, monitor_result_df)

        print(f"\n软便 M2 基准池监控结果已写入 batch_id={monitor_batch_id}。")
        print("软便 M2 基准池 current_pool_risk_level 分布：")
        print(monitor_result_df["current_pool_risk_level"].value_counts(dropna=False))

        print("\n软便 M2 基准池 final_risk_type 分布：")
        print(monitor_result_df["final_risk_type"].value_counts(dropna=False))


# =========================================================
# 10. 命令行入口
# =========================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="计算 SKU 软便 M2 风险结果并写入 sku_risk_score_result"
    )

    parser.add_argument(
        "--batch_id",
        type=str,
        default=None,
        help="只计算某个批次，例如 B001。不传则根据其他条件计算。",
    )

    parser.add_argument(
        "--data_type",
        type=str,
        default=None,
        help="只计算某类数据，例如 new / historical / verified_new。",
    )

    parser.add_argument(
        "--feature_version",
        type=str,
        default=DEFAULT_FEATURE_VERSION,
        help="特征版本，默认 soft_stool_v2。",
    )

    parser.add_argument(
        "--reference_pool_version",
        type=str,
        default=None,
        help="指定软便基准池版本，例如 SOFT_STOOL_REF_V2。不传则自动使用 is_active_pool = 1 的最新版本。",
    )

    parser.add_argument(
        "--recalculate_all",
        action="store_true",
        help="是否重算当前 feature_version 下所有 SKU。",
    )

    parser.add_argument(
        "--no_delete_before_insert",
        action="store_true",
        help="不删除旧结果，直接追加写入。",
    )

    parser.add_argument(
        "--no_reference_monitor",
        action="store_true",
        help="不回写基准池 SKU 在当前全量池中的监控结果。",
    )

    args = parser.parse_args()

    run(
        batch_id=args.batch_id,
        data_type=args.data_type,
        feature_version=args.feature_version,
        reference_pool_version=args.reference_pool_version,
        recalculate_all=args.recalculate_all,
        delete_before_insert=not args.no_delete_before_insert,
        monitor_reference_pool=not args.no_reference_monitor,
    )
