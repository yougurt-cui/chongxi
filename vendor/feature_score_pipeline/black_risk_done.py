import argparse
import os
from datetime import datetime
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text


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

SCORE_MODEL_VERSION = "BLACK_CHIN_M2_FAT_OMEGA_FAT_B"

DEFAULT_FEATURE_VERSION = "v1"

# 脂肪特征结果表：来自 catfood_fat_material_features_scored_v2.py
# 默认按 sku_feature_input.sku_id = catfood_fat_material_features_scored.product_key 关联。
FAT_FEATURE_TABLE = "catfood_fat_material_features_scored"

# 当前版本黑下巴解释分公式：
# 如果有 omega_imbalance_score / fat_regulation_score：
#   buffer_score = 0.30 * antioxidant + 0.25 * prebiotic + 0.20 * fiber + 0.25 * fat_regulation
#   black_chin_score = 0.45 * fat + 0.25 * omega_imbalance + 0.15 * protein + 0.05 * carb - 0.30 * buffer
# 如果没有脂肪扩展字段，则自动退回旧公式：
#   buffer_score = 0.40 * antioxidant + 0.35 * prebiotic + 0.25 * fiber
#   black_chin_score = 0.40 * fat + 0.25 * protein + 0.10 * carb - 0.25 * buffer


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


def ensure_reference_pool_formula_id(engine):
    """Bring the reference pool snapshot up to formula-level identity when possible."""
    columns = get_table_columns(engine, "reference_sku_pool_snapshot")
    with engine.begin() as conn:
        if "formula_id" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE reference_sku_pool_snapshot "
                    "ADD COLUMN formula_id BIGINT UNSIGNED NULL AFTER sku_id"
                )
            )

        index_rows = conn.execute(
            text(
                """
                SELECT INDEX_NAME
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = 'reference_sku_pool_snapshot'
                  AND INDEX_NAME = 'idx_reference_formula_feature'
                """
            ),
            {"schema_name": DB_CONFIG["database"]},
        ).fetchall()
        if not index_rows:
            conn.execute(
                text(
                    "ALTER TABLE reference_sku_pool_snapshot "
                    "ADD KEY idx_reference_formula_feature (formula_id, feature_version)"
                )
            )

        conn.execute(
            text(
                """
                UPDATE reference_sku_pool_snapshot p
                JOIN (
                    SELECT sku_id, feature_version, MIN(formula_id) AS formula_id
                    FROM sku_feature_input
                    WHERE formula_id IS NOT NULL
                    GROUP BY sku_id, feature_version
                    HAVING COUNT(DISTINCT formula_id) = 1
                ) f
                  ON p.sku_id = f.sku_id
                 AND p.feature_version = f.feature_version
                SET p.formula_id = f.formula_id
                WHERE p.formula_id IS NULL
                """
            )
        )


def build_sku_feature_sql(engine):
    """
    构造 sku_feature_input 查询 SQL。

    优先使用脂肪评分结果表中的 fat_score / fat_regulation_score / omega_imbalance_score / fat_reason_tags。
    如果脂肪评分结果表不存在，或者缺少字段，则退回 sku_feature_input 原始 fat_score。
    """
    fat_cols = get_table_columns(engine, FAT_FEATURE_TABLE)
    can_join_fat_table = {
        "product_key",
        "fat_score",
        "fat_regulation_score",
        "omega_imbalance_score",
        "fat_reason_tags",
    }.issubset(fat_cols)

    if can_join_fat_table:
        return f"""
        SELECT
            s.formula_id,
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
            f.fat_regulation_score,
            f.omega_imbalance_score,
            f.fat_reason_tags
        FROM sku_feature_input s
        LEFT JOIN (
            SELECT f0.*
            FROM {FAT_FEATURE_TABLE} f0
            LEFT JOIN (
                SELECT product_name
                FROM {FAT_FEATURE_TABLE}
                WHERE COALESCE(TRIM(product_name), '') <> ''
                GROUP BY product_name
                HAVING COUNT(*) = 1
            ) unique_name
              ON f0.product_name = unique_name.product_name
            WHERE f0.product_key IS NOT NULL
               OR unique_name.product_name IS NOT NULL
        ) f
          ON s.formula_id = f.formula_id
          OR (s.formula_id IS NULL AND s.sku_id = f.product_key)
          OR (
              COALESCE(TRIM(s.sku_name), '') <> ''
              AND f.product_key <> s.sku_id
              AND f.product_key IS NOT NULL
              AND COALESCE(TRIM(s.sku_name), '') = COALESCE(TRIM(f.product_name), '')
          )
        WHERE s.feature_version = :feature_version
        """

    return """
        SELECT
            formula_id,
            sku_id,
            sku_name,
            brand_name,
            batch_id,
            data_type,
            feature_version,
            protein_score,
            carb_score,
            fiber_score,
            fat_score,
            prebiotic_score,
            antioxidant_score,
            NULL AS fat_regulation_score,
            NULL AS omega_imbalance_score,
            NULL AS fat_reason_tags
        FROM sku_feature_input
        WHERE feature_version = :feature_version
    """


def calculate_reference_percentile(value, reference_values):
    """
    计算 value 在 reference_values 中的历史分位数。

    例如：
    value 高于或等于 80% 的历史 SKU，则返回 0.80。

    注意：
    这不是概率，是相对位置。
    """
    reference_values = pd.to_numeric(reference_values, errors="coerce").dropna()

    if pd.isna(value) or len(reference_values) == 0:
        return np.nan

    return (reference_values <= value).mean()


def assign_risk_level(percentile):
    """
    根据分位数分风险等级。
    """
    if pd.isna(percentile):
        return "未知风险"

    if percentile >= 0.80:
        return "高配方风险"
    elif percentile >= 0.60:
        return "中高配方风险"
    elif percentile >= 0.40:
        return "中配方风险"
    elif percentile >= 0.20:
        return "中低配方风险"
    else:
        return "低配方风险"


def assign_batch_priority_level(batch_percentile):
    """
    本批新品内部优先级。
    """
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


def assign_brand_black_chin_level(row):
    """
    根据品牌黑下巴占比生成品牌反馈等级。
    """
    rate = row.get("brand_black_chin_rate")
    total_count = row.get("brand_total_comment_count")

    if pd.isna(rate):
        return "无品牌反馈数据"

    if pd.isna(total_count) or total_count < 30:
        return "低可信品牌样本"

    if rate >= 0.15:
        return "高品牌反馈"
    elif rate >= 0.10:
        return "中高品牌反馈"
    elif rate >= 0.05:
        return "中品牌反馈"
    else:
        return "低品牌反馈"


def assign_final_risk_type(row):
    """
    最终风险类型：
    - 有品牌反馈：品牌反馈 × SKU配方风险
    - 无品牌反馈：冷启动配方风险
    """
    brand_level = row.get("brand_black_chin_level")
    history_level = row.get("history_risk_level")

    high_brand = brand_level in ["高品牌反馈", "中高品牌反馈"]
    mid_or_low_brand = brand_level in ["中品牌反馈", "低品牌反馈"]
    low_brand = brand_level == "低品牌反馈"

    high_sku = history_level in ["高配方风险", "中高配方风险"]
    low_sku = history_level in ["中低配方风险", "低配方风险"]

    # 新品无品牌反馈，走冷启动逻辑
    if brand_level == "无品牌反馈数据":
        if history_level == "高配方风险":
            return "冷启动高风险预警型"
        elif history_level == "中高配方风险":
            return "冷启动中高风险型"
        elif history_level == "中配方风险":
            return "冷启动观察型"
        elif history_level in ["中低配方风险", "低配方风险"]:
            return "冷启动低风险型"
        else:
            return "冷启动未知型"

    if brand_level == "低可信品牌样本":
        if high_sku:
            return "低可信高配方风险观察型"
        else:
            return "低可信观察型"

    # 有品牌反馈时，走二维矩阵
    if high_brand and high_sku:
        return "高可信高风险型"

    if high_brand and low_sku:
        return "品牌驱动型"

    if mid_or_low_brand and high_sku:
        return "潜在预警型"

    if low_brand and low_sku:
        return "低风险型"

    return "中间观察型"


def merge_tag_list(tags, extra_tags_text):
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
            if tag and tag not in seen and tag != "暂无明显脂肪结构风险":
                result.append(tag)
                seen.add(tag)

    return result


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


def format_tag_list(tags, fallback=None):
    cleaned = merge_tag_list(tags, None)
    if not cleaned and fallback:
        cleaned = [fallback]
    return ",".join(cleaned)


def build_main_reason_tags(row):
    """
    黑下巴主解释标签：
    只保留更适合作为前台/报告主结论的标签。

    不直接合并 fat_reason_tags，避免把脂肪模型内部细项全部塞进主原因。
    """
    tags = []

    if value_ge(row, "fat_history_percentile", 0.70):
        tags.append("脂肪负担偏高")

    if value_ge(row, "omega_imbalance_history_percentile", 0.70):
        tags.append("Omega脂肪酸比例偏失衡")

    if value_ge(row, "protein_history_percentile", 0.70):
        tags.append("蛋白结构偏复杂")

    if value_le(row, "fat_regulation_history_percentile", 0.30):
        tags.append("脂肪调节支持不足")

    if value_le(row, "buffer_score", 0.30):
        tags.append("调节缓冲不足")

    return format_tag_list(tags, fallback="暂无明显黑下巴主风险标签")


def build_support_reason_tags(row):
    """
    辅助解释标签：
    可以辅助理解配方状态，但不直接作为黑下巴主因展示。
    """
    tags = []

    if value_ge(row, "carb_history_percentile", 0.70):
        tags.append("碳水结构偏高")

    if value_le(row, "antioxidant_history_percentile", 0.30):
        tags.append("抗氧化支持偏弱")

    if value_le(row, "prebiotic_history_percentile", 0.30):
        tags.append("益生元支持偏弱")

    if value_le(row, "fiber_history_percentile", 0.30):
        tags.append("纤维支持偏弱")

    return format_tag_list(tags, fallback="暂无明显辅助观察标签")


def build_fat_detail_tags(row):
    """
    脂肪细节标签：
    直接沿用脂肪脚本的 fat_reason_tags，用于解释脂肪负担或 Omega 失衡背后的细项。
    """
    tags = merge_tag_list([], row.get("fat_reason_tags"))
    return format_tag_list(tags, fallback="暂无明显脂肪结构风险")


def build_all_reason_tags(row):
    """
    全量追踪标签：
    合并主原因、辅助原因、脂肪细节，供后台排查和模型解释使用。
    """
    tags = []

    for field, fallback in [
        ("main_reason_tags", "暂无明显黑下巴主风险标签"),
        ("support_reason_tags", "暂无明显辅助观察标签"),
        ("fat_detail_tags", "暂无明显脂肪结构风险"),
    ]:
        text_value = row.get(field, "")
        if pd.isna(text_value):
            continue
        for tag in str(text_value).replace("，", ",").split(","):
            tag = tag.strip()
            if tag and tag != fallback:
                tags.append(tag)

    return format_tag_list(tags, fallback="暂无明显高风险标签")


def build_reason_tags(row):
    """
    兼容旧字段：
    reason_tags 继续保留，但只等于 main_reason_tags，避免前台展示过长、过杂。
    """
    return build_main_reason_tags(row)


def assign_main_risk_driver(row):
    """给出主要拉高项，方便解释高配方风险的第一原因。"""
    drivers = {
        "脂肪负担": row.get("fat_risk_contribution", 0),
        "Omega失衡": row.get("omega_imbalance_risk_contribution", 0),
        "蛋白结构": row.get("protein_risk_contribution", 0),
        "碳水结构": row.get("carb_risk_contribution", 0),
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
    if row.get("fat_regulation_history_percentile", np.nan) <= 0.30:
        weak_buffers.append("脂肪调节弱")
    if row.get("antioxidant_history_percentile", np.nan) <= 0.30:
        weak_buffers.append("抗氧化弱")
    if row.get("prebiotic_history_percentile", np.nan) <= 0.30:
        weak_buffers.append("益生元弱")
    if row.get("fiber_history_percentile", np.nan) <= 0.30:
        weak_buffers.append("纤维弱")

    if weak_buffers:
        return main_driver + "主导 + " + "/".join(weak_buffers)

    return main_driver + "主导"


def validate_required_columns(df, required_columns, table_name):
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        raise ValueError(f"{table_name} 缺少必要字段: {missing}")


# =========================================================
# 4. 读取数据
# =========================================================

def get_active_reference_pool_version(engine):
    sql = """
        SELECT reference_pool_version
        FROM reference_sku_pool_snapshot
        WHERE is_active_pool = 1
        GROUP BY reference_pool_version
        ORDER BY MAX(pool_created_at) DESC
        LIMIT 1
    """
    df = pd.read_sql(sql, engine)

    if df.empty:
        raise ValueError("没有找到 is_active_pool = 1 的基准池版本")

    return df.iloc[0]["reference_pool_version"]


def load_sku_feature_input(engine, feature_version):
    sql = build_sku_feature_sql(engine)
    return pd.read_sql(text(sql), engine, params={"feature_version": feature_version})


def dedupe_reference_pool_rows(df):
    if df.empty or "formula_id" not in df.columns:
        return df
    linked = df[df["formula_id"].notna()].drop_duplicates(
        subset=["formula_id"],
        keep="first",
    )
    legacy = df[df["formula_id"].isna()].drop_duplicates(
        subset=["sku_id", "feature_version"],
        keep="first",
    )
    return pd.concat([linked, legacy], ignore_index=True)


def load_reference_pool(engine, reference_pool_version, feature_version):
    fat_cols = get_table_columns(engine, FAT_FEATURE_TABLE)
    can_join_fat_table = {
        "product_key",
        "fat_score",
        "fat_regulation_score",
        "omega_imbalance_score",
        "fat_reason_tags",
    }.issubset(fat_cols)

    if can_join_fat_table:
        sql = f"""
            SELECT
                f.formula_id,
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
                ff.fat_regulation_score,
                ff.omega_imbalance_score,
                ff.fat_reason_tags
            FROM reference_sku_pool_snapshot p
            JOIN sku_feature_input f
              ON p.feature_version = f.feature_version
             AND (
                    (p.formula_id IS NOT NULL AND p.formula_id = f.formula_id)
                 OR (p.formula_id IS NULL AND p.sku_id = f.sku_id)
             )
            LEFT JOIN {FAT_FEATURE_TABLE} ff
              ON f.formula_id = ff.formula_id
              OR (f.formula_id IS NULL AND f.sku_id = ff.product_key)
            WHERE p.reference_pool_version = :reference_pool_version
              AND p.feature_version = :feature_version
        """
    else:
        sql = """
            SELECT
                f.formula_id,
                f.sku_id,
                f.sku_name,
                f.brand_name,
                f.batch_id,
                f.data_type,
                f.feature_version,
                f.protein_score,
                f.carb_score,
                f.fiber_score,
                f.fat_score,
                f.prebiotic_score,
                f.antioxidant_score,
                NULL AS fat_regulation_score,
                NULL AS omega_imbalance_score,
                NULL AS fat_reason_tags
            FROM reference_sku_pool_snapshot p
            JOIN sku_feature_input f
              ON p.feature_version = f.feature_version
             AND (
                    (p.formula_id IS NOT NULL AND p.formula_id = f.formula_id)
                 OR (p.formula_id IS NULL AND p.sku_id = f.sku_id)
             )
            WHERE p.reference_pool_version = :reference_pool_version
              AND p.feature_version = :feature_version
        """

    df = pd.read_sql(
        text(sql),
        engine,
        params={
            "reference_pool_version": reference_pool_version,
            "feature_version": feature_version,
        },
    )
    return dedupe_reference_pool_rows(df)


def load_brand_symptom_stats(engine, symptom_type="black_chin"):
    """
    读取品牌黑下巴症状表。
    当前 brand_symptom_stats 口径：
    secondary_symptom IN ('皮脂分泌过多', '黑下巴') AND direct='加重'，
    且品牌相关线索数 > 3。
    """
    sql = """
        SELECT
            brand AS brand_name,
            'black_chin' AS symptom_type,
            black_chin_aggravation_count AS symptom_count,
            total_clue_count AS total_comment_count,
            black_chin_aggravation_ratio AS symptom_rate,
            'black_chin_sebum_gt3_20260429' AS stat_version
        FROM brand_symptom_stats
    """
    return pd.read_sql(text(sql), engine)


# =========================================================
# 5. 核心计算逻辑
# =========================================================

def get_percentile_config(df):
    config = {
        "protein_score": "protein_history_percentile",
        "carb_score": "carb_history_percentile",
        "fiber_score": "fiber_history_percentile",
        "fat_score": "fat_history_percentile",
        "prebiotic_score": "prebiotic_history_percentile",
        "antioxidant_score": "antioxidant_history_percentile",
    }

    if "omega_imbalance_score" in df.columns:
        config["omega_imbalance_score"] = "omega_imbalance_history_percentile"

    if "fat_regulation_score" in df.columns:
        config["fat_regulation_score"] = "fat_regulation_history_percentile"

    return config


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


def calculate_buffer_score(df):
    """
    计算缓冲分。

    如果有 fat_regulation_history_percentile，则使用 M2 缓冲公式；
    否则退回 M1 缓冲公式。
    """
    has_fat_regulation = (
        "fat_regulation_history_percentile" in df.columns
        and df["fat_regulation_history_percentile"].notna().any()
    )

    if has_fat_regulation:
        return (
            0.30 * df["antioxidant_history_percentile"]
            + 0.25 * df["prebiotic_history_percentile"]
            + 0.20 * df["fiber_history_percentile"]
            + 0.25 * df["fat_regulation_history_percentile"]
        )

    return (
        0.40 * df["antioxidant_history_percentile"]
        + 0.35 * df["prebiotic_history_percentile"]
        + 0.25 * df["fiber_history_percentile"]
    )


def calculate_black_chin_explain_score(df):
    """
    计算黑下巴解释分。

    如果有 omega_imbalance_history_percentile，则使用 M2 公式；
    否则退回 M1 公式。
    """
    has_omega = (
        "omega_imbalance_history_percentile" in df.columns
        and df["omega_imbalance_history_percentile"].notna().any()
    )

    if has_omega:
        return (
            0.45 * df["fat_history_percentile"]
            + 0.25 * df["omega_imbalance_history_percentile"]
            + 0.15 * df["protein_history_percentile"]
            + 0.05 * df["carb_history_percentile"]
            - 0.30 * df["buffer_score"]
        )

    return (
        0.40 * df["fat_history_percentile"]
        + 0.25 * df["protein_history_percentile"]
        + 0.10 * df["carb_history_percentile"]
        - 0.25 * df["buffer_score"]
    )


def add_score_contributions(df):
    """拆解主要贡献项，方便定位高风险原因。"""
    result = df.copy()

    has_omega = (
        "omega_imbalance_history_percentile" in result.columns
        and result["omega_imbalance_history_percentile"].notna().any()
    )

    if has_omega:
        result["fat_risk_contribution"] = 0.45 * result["fat_history_percentile"]
        result["omega_imbalance_risk_contribution"] = 0.25 * result["omega_imbalance_history_percentile"]
        result["protein_risk_contribution"] = 0.15 * result["protein_history_percentile"]
        result["carb_risk_contribution"] = 0.05 * result["carb_history_percentile"]
        result["buffer_protection_contribution"] = -0.30 * result["buffer_score"]
    else:
        result["fat_risk_contribution"] = 0.40 * result["fat_history_percentile"]
        result["omega_imbalance_risk_contribution"] = np.nan
        result["protein_risk_contribution"] = 0.25 * result["protein_history_percentile"]
        result["carb_risk_contribution"] = 0.10 * result["carb_history_percentile"]
        result["buffer_protection_contribution"] = -0.25 * result["buffer_score"]

    return result


def calculate_scores(target_df, reference_df, current_pool_df=None):
    """
    target_df:
        要计算风险的 SKU，可以是历史 SKU，也可以是一批新品。

    reference_df:
        固定历史基准池，比如 V1 的45个SKU。

    current_pool_df:
        当前全量池，用来计算 current_pool_percentile。
        如果不传，默认用 reference_df + target_df。
    """

    df = target_df.copy()
    reference_df = reference_df.copy()

    score_cols = [
        "protein_score",
        "carb_score",
        "fiber_score",
        "fat_score",
        "prebiotic_score",
        "antioxidant_score",
        "omega_imbalance_score",
        "fat_regulation_score",
    ]

    for col in score_cols:
        if col not in df.columns:
            df[col] = np.nan
        if col not in reference_df.columns:
            reference_df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
        reference_df[col] = pd.to_numeric(reference_df[col], errors="coerce")

    # 1. 计算目标 SKU 各特征在历史基准池中的分位数
    df = add_percentile_columns(df, reference_df)

    # 2. 调节缓冲分：M2 纳入 fat_regulation_score；无该字段时退回 M1
    df["buffer_score"] = calculate_buffer_score(df)

    # 3. 黑下巴解释分：M2 纳入 omega_imbalance_score；无该字段时退回 M1
    df["black_chin_explain_score"] = calculate_black_chin_explain_score(df)

    # 4. 先给 reference_df 也计算出 explain_score，作为历史分位数参照
    ref_scored = add_percentile_columns(reference_df.copy(), reference_df.copy())
    ref_scored["buffer_score"] = calculate_buffer_score(ref_scored)
    ref_scored["black_chin_explain_score"] = calculate_black_chin_explain_score(ref_scored)

    # 5. 目标 SKU 的历史分位数
    df["history_percentile"] = df["black_chin_explain_score"].apply(
        lambda x: calculate_reference_percentile(
            x,
            ref_scored["black_chin_explain_score"]
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

    df["current_pool_percentile"] = df["black_chin_explain_score"].apply(
        lambda x: calculate_reference_percentile(
            x,
            current_pool_scored["black_chin_explain_score"]
        )
    )

    df["current_pool_risk_level"] = df["current_pool_percentile"].apply(assign_risk_level)

    # 7. 贡献项拆解
    df = add_score_contributions(df)
    df["main_risk_driver"] = df.apply(assign_main_risk_driver, axis=1)

    # 8. 本批内部排名
    if "batch_id" in df.columns:
        df["batch_rank"] = (
            df.groupby("batch_id")["black_chin_explain_score"]
            .rank(method="dense", ascending=False)
            .astype("Int64")
        )

        df["batch_percentile"] = (
            df.groupby("batch_id")["black_chin_explain_score"]
            .rank(pct=True, method="average")
        )

        df["batch_priority_level"] = df["batch_percentile"].apply(assign_batch_priority_level)
    else:
        df["batch_rank"] = np.nan
        df["batch_percentile"] = np.nan
        df["batch_priority_level"] = "无批次优先级"

    return df


def calculate_current_pool_explain_scores(current_pool_df, reference_df):
    """
    给当前全量池计算 explain_score。
    注意：
    各特征分位数依然基于 reference_df 计算，
    这样和历史分位数的评分口径保持一致。
    """
    df = current_pool_df.copy()
    reference_df = reference_df.copy()

    score_cols = [
        "protein_score",
        "carb_score",
        "fiber_score",
        "fat_score",
        "prebiotic_score",
        "antioxidant_score",
        "omega_imbalance_score",
        "fat_regulation_score",
    ]

    for col in score_cols:
        if col not in df.columns:
            df[col] = np.nan
        if col not in reference_df.columns:
            reference_df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
        reference_df[col] = pd.to_numeric(reference_df[col], errors="coerce")

    df = add_percentile_columns(df, reference_df)
    df["buffer_score"] = calculate_buffer_score(df)
    df["black_chin_explain_score"] = calculate_black_chin_explain_score(df)

    return df


def attach_brand_symptom(scored_df, brand_symptom_df):
    """
    把品牌黑下巴占比 join 到结果里。
    """
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

    # 如果同一品牌有多个 stat_version，这里默认取最新一条。
    # 如果你有明确的 active_stat_version，可以在 SQL 里筛选。
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

    df["brand_black_chin_level"] = df.apply(assign_brand_black_chin_level, axis=1)

    return df


def finalize_result(df, reference_pool_version, feature_version):
    """
    生成最终字段。
    """
    result = df.copy()

    result["reference_pool_version"] = reference_pool_version
    result["feature_version"] = feature_version
    result["score_model_version"] = SCORE_MODEL_VERSION

    result["final_risk_type"] = result.apply(assign_final_risk_type, axis=1)

    # 标签拆分：
    # reason_tags 保留为前台主标签，避免历史字段断裂；
    # main/support/fat_detail/all 用于更清晰地区分主因、辅助项、脂肪细节和后台全量追踪。
    result["main_reason_tags"] = result.apply(build_main_reason_tags, axis=1)
    result["support_reason_tags"] = result.apply(build_support_reason_tags, axis=1)
    result["fat_detail_tags"] = result.apply(build_fat_detail_tags, axis=1)
    result["all_reason_tags"] = result.apply(build_all_reason_tags, axis=1)
    result["reason_tags"] = result["main_reason_tags"]

    result["calculated_at"] = datetime.now()

    return result


def build_reference_monitor_batch_id(batch_id, reference_pool_version):
    if batch_id:
        return f"{batch_id}__reference_monitor"
    return f"{reference_pool_version}__reference_monitor"


def calculate_reference_monitor_results(
    reference_df,
    reference_pool_version,
    feature_version,
    current_pool_df,
    brand_symptom_df,
    monitor_batch_id,
):
    """
    目标批次跑完后，同步重算基准池 SKU 在当前全量池中的位置。
    history_* 仍基于 reference pool 自身；current_pool_* 会反映新增 SKU 后的变化。
    """
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
# 6. 写入结果表
# =========================================================

def delete_existing_results(
    engine,
    batch_id,
    reference_pool_version,
    feature_version,
    score_model_version,
):
    """
    防止同一批次重复插入。

    如果你想保留每次重复计算的历史记录，可以不要调用这个函数。
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


def write_results(engine, result_df):
    """
    写入 sku_risk_score_result。

    注意：
    如果你的 sku_risk_score_result 表没有某些字段，
    可以从 insert_columns 里删掉对应字段。
    """

    insert_columns = [
        "formula_id",
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

    # 只保留结果表需要的字段
    output_df = result_df[insert_columns].copy()

    # MySQL 不喜欢 pandas 的 NA
    output_df = output_df.replace({pd.NA: None, np.nan: None})

    ensure_result_table_schema(engine)

    output_df.to_sql(
        "sku_risk_score_result",
        con=engine,
        if_exists="append",
        index=False,
        chunksize=1000,
    )


def ensure_result_table_schema(engine):
    """
    兼容当前 sku_risk_score_result 表，补齐脚本会写入的品牌反馈字段。
    """
    expected_columns = {
        "formula_id": "BIGINT UNSIGNED NULL",
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


# =========================================================
# 7. 主流程
# =========================================================

def run(
    batch_id=None,
    data_type=None,
    feature_version=DEFAULT_FEATURE_VERSION,
    reference_pool_version=None,
    output_batch_id=None,
    output_reference_pool_version=None,
    recalculate_all=False,
    delete_before_insert=True,
    monitor_reference_pool=True,
):
    engine = get_engine()
    ensure_reference_pool_formula_id(engine)

    if reference_pool_version is None:
        reference_pool_version = get_active_reference_pool_version(engine)

    print(f"使用基准池版本: {reference_pool_version}")
    print(f"使用特征版本: {feature_version}")
    print(f"使用评分模型版本: {SCORE_MODEL_VERSION}")

    sku_df = load_sku_feature_input(engine, feature_version=feature_version)
    reference_df = load_reference_pool(
        engine,
        reference_pool_version=reference_pool_version,
        feature_version=feature_version,
    )
    brand_symptom_df = load_brand_symptom_stats(engine, symptom_type="black_chin")

    validate_required_columns(
        sku_df,
        [
            "sku_id",
            "sku_name",
            "brand_name",
            "batch_id",
            "data_type",
            "feature_version",
            "protein_score",
            "carb_score",
            "fiber_score",
            "fat_score",
            "prebiotic_score",
            "antioxidant_score",
        ],
        "sku_feature_input",
    )

    validate_required_columns(
        reference_df,
        [
            "sku_id",
            "protein_score",
            "carb_score",
            "fiber_score",
            "fat_score",
            "prebiotic_score",
            "antioxidant_score",
        ],
        "reference_sku_pool_snapshot + sku_feature_input",
    )

    if reference_df.empty:
        raise ValueError(f"基准池 {reference_pool_version} 没有 SKU，请检查 reference_sku_pool_snapshot")

    # 选择要计算的目标 SKU
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
    print(f"基准池 SKU 数量: {len(reference_df)}")

    # 当前全量池：这里使用 sku_feature_input 中所有当前 feature_version 的 SKU
    current_pool_df = sku_df.copy()

    scored_df = calculate_scores(
        target_df=target_df,
        reference_df=reference_df,
        current_pool_df=current_pool_df,
    )

    scored_df = attach_brand_symptom(scored_df, brand_symptom_df)

    result_reference_pool_version = output_reference_pool_version or reference_pool_version
    result_batch_id = output_batch_id or batch_id

    if result_batch_id is not None:
        scored_df = scored_df.copy()
        scored_df["batch_id"] = result_batch_id

    result_df = finalize_result(
        scored_df,
        reference_pool_version=result_reference_pool_version,
        feature_version=feature_version,
    )

    # 如果是按 batch 计算，建议先删除同批次同版本旧结果，避免重复。
    if delete_before_insert and batch_id is not None:
        delete_existing_results(
            engine=engine,
            batch_id=result_batch_id,
            reference_pool_version=result_reference_pool_version,
            feature_version=feature_version,
            score_model_version=SCORE_MODEL_VERSION,
        )

    write_results(engine, result_df)

    print("写入 sku_risk_score_result 完成。")
    print("\n风险类型分布：")
    print(result_df["final_risk_type"].value_counts(dropna=False))

    print("\n历史风险等级分布：")
    print(result_df["history_risk_level"].value_counts(dropna=False))

    if monitor_reference_pool and batch_id is not None:
        monitor_batch_id = build_reference_monitor_batch_id(
            batch_id=result_batch_id,
            reference_pool_version=result_reference_pool_version,
        )
        monitor_result_df = calculate_reference_monitor_results(
            reference_df=reference_df,
            reference_pool_version=result_reference_pool_version,
            feature_version=feature_version,
            current_pool_df=current_pool_df,
            brand_symptom_df=brand_symptom_df,
            monitor_batch_id=monitor_batch_id,
        )

        if delete_before_insert:
            delete_existing_results(
                engine=engine,
                batch_id=monitor_batch_id,
                reference_pool_version=result_reference_pool_version,
                feature_version=feature_version,
                score_model_version=SCORE_MODEL_VERSION,
            )

        write_results(engine, monitor_result_df)

        print(f"\n基准池监控结果已写入 batch_id={monitor_batch_id}。")
        print("基准池 current_pool_risk_level 分布：")
        print(monitor_result_df["current_pool_risk_level"].value_counts(dropna=False))

        print("\n基准池 final_risk_type 分布：")
        print(monitor_result_df["final_risk_type"].value_counts(dropna=False))


# =========================================================
# 8. 命令行入口
# =========================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="计算 SKU 黑下巴风险结果并写入 sku_risk_score_result")

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
        help="特征版本，默认 F1。",
    )

    parser.add_argument(
        "--reference_pool_version",
        type=str,
        default=None,
        help="指定基准池版本，例如 V1。不传则自动使用 is_active_pool = 1 的版本。",
    )

    parser.add_argument(
        "--output_batch_id",
        type=str,
        default=None,
        help="写入结果表时使用的短 batch_id；不传则沿用源数据 batch_id。",
    )

    parser.add_argument(
        "--output_reference_pool_version",
        type=str,
        default=None,
        help="写入结果表时使用的短 reference_pool_version；实际计算仍使用 --reference_pool_version。",
    )

    parser.add_argument(
        "--recalculate_all",
        action="store_true",
        help="是否重算所有 SKU。",
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
        output_batch_id=args.output_batch_id,
        output_reference_pool_version=args.output_reference_pool_version,
        recalculate_all=args.recalculate_all,
        delete_before_insert=not args.no_delete_before_insert,
        monitor_reference_pool=not args.no_reference_monitor,
    )
