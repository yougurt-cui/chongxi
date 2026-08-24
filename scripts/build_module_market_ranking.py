# -*- coding: utf-8 -*-
"""
各模块市场相对排名（百分位）

从 catfood_protein_fat_fiber_score_wide 读取各维度评分，
按 10 个营养模块独立计算市场百分位排名 (0–100)，写入新表。

输出表: catfood_module_market_ranking
数据源: catfood_protein_fat_fiber_score_wide

用法:
  python scripts/build_module_market_ranking.py
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import Numeric, create_engine, text
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config


# ============================================================
# 数据库配置
# ============================================================

SOURCE_TABLE = "catfood_protein_fat_fiber_score_wide"
OUTPUT_TABLE = "catfood_module_market_ranking"


# ============================================================
# 10 个模块配置
#   fields:  该模块依赖的 score 字段列表（来自 wide 表）
#   polarities:  每个字段的方向
#     "positive" → 分数越高市场排名越靠前（质量 / 支持类）
#     "negative" → 分数越高市场排名越靠后（负担 / 压力类）
#   composite: 多字段时是否先取均值再排百分位
#   note: 补充说明（代理 / 缺失原因）
# ============================================================

MODULE_CONFIGS = [
    {
        "name": "animal_protein",
        "label": "动物蛋白",
        "fields": ["protein_quality_score"],
        "polarities": ["positive"],
        "composite": False,
        "note": "",
    },
    {
        "name": "plant_protein",
        "label": "植物蛋白",
        "fields": [],
        "polarities": [],
        "composite": False,
        "note": "protein_structure_score 是蛋白结构负载综合分(7维)，不适合单独代表植物蛋白，暂置空",
    },
    {
        "name": "starch_carb",
        "label": "淀粉碳水",
        "fields": ["starch_burden_score"],
        "polarities": ["negative"],
        "composite": False,
        "note": "",
    },
    {
        "name": "fat",
        "label": "脂肪",
        "fields": ["fat_score"],
        "polarities": ["negative"],
        "composite": False,
        "note": "",
    },
    {
        "name": "fiber",
        "label": "纤维",
        "fields": ["p_form_score", "p_bulk_score"],
        "polarities": ["positive", "positive"],
        "composite": True,
        "note": "成形分 + 骨架分均值",
    },
    {
        "name": "prebiotic",
        "label": "益生元",
        "fields": ["q_feed", "q_scfa"],
        "polarities": ["positive", "positive"],
        "composite": True,
        "note": "供菌底物 + 菌群代谢支持均值",
    },
    {
        "name": "digestion_support",
        "label": "消化支持",
        "fields": ["p_buffer"],
        "polarities": ["positive"],
        "composite": False,
        "note": "肠道缓冲能力",
    },
    {
        "name": "palatability",
        "label": "适口性",
        "fields": ["fat_oily_score"],
        "polarities": ["positive"],
        "composite": False,
        "note": "wide 表无直接适口性分，油脂评分作为近似代理",
    },
    {
        "name": "mineral_micronutrient",
        "label": "矿物微量营养",
        "fields": ["fat_regulation_score"],
        "polarities": ["positive"],
        "composite": False,
        "note": "脂肪调节分中含 25% 微量营养支持 + 35% 抗氧化 + 30% Omega-3，作为矿物微量营养近似代理",
    },
    {
        "name": "functional_nutrition",
        "label": "功能性营养",
        "fields": ["omega_imbalance_score"],
        "polarities": ["negative"],
        "composite": False,
        "note": "Omega 脂肪酸结构失衡风险，越低说明脂肪酸越平衡",
    },
]


# ============================================================
# 工具函数
# ============================================================

def get_engine():
    cfg = get_feature_mysql_config()
    return create_engine(URL.create(
        "mysql+pymysql",
        username=str(cfg["user"]),
        password=str(cfg.get("password") or ""),
        host=str(cfg["host"]),
        port=int(cfg["port"]),
        database=str(cfg["database"]),
        query={"charset": str(cfg.get("charset") or "utf8mb4")},
    ))


def quote_identifier(name: str) -> str:
    return "`{}`".format(str(name).replace("`", "``"))


def get_available_columns(engine, table_name: str) -> set:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = :tbl
                """
            ),
            {"tbl": table_name},
        ).fetchall()
    return {r[0] for r in rows}


def table_exists(engine, table_name: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = :tbl
                    """
                ),
                {"tbl": table_name},
            ).scalar()
        )


# ============================================================
# 数据加载
# ============================================================

def load_source_data(engine, available_columns: set) -> pd.DataFrame:
    """从 wide 表读取有效数据，只保留有 formula_id 的行。"""
    score_fields = set()
    for cfg in MODULE_CONFIGS:
        score_fields.update(cfg["fields"])

    existing = [f for f in score_fields if f in available_columns]
    if not existing:
        raise ValueError(f"{SOURCE_TABLE} 中找不到任何已配置的评分字段")

    cols = ["formula_id", "source_id", "product_key", "brand", "product_name"] + existing
    col_sql = ", ".join(quote_identifier(c) for c in cols)
    sql = f"""
        SELECT {col_sql}
        FROM {quote_identifier(SOURCE_TABLE)}
        WHERE formula_id IS NOT NULL
          AND product_key IS NOT NULL
          AND TRIM(product_key) <> ''
    """
    df = pd.read_sql(sql, engine)
    df = df.drop_duplicates(subset=["formula_id"], keep="last")
    return df


# ============================================================
# 百分位计算
# ============================================================

def compute_percentile_ranks(df: pd.DataFrame) -> pd.DataFrame:
    """
    对每个模块独立计算市场百分位排名 (0–100)。

    - 单字段模块: 直接 rank(pct=True) * 100
    - 多字段模块: 各字段独立百分位 → 按极性调整 → 均值
    - 极性 negative: percentile = 100 - raw_percentile
    """
    result = df[["formula_id", "source_id", "product_key", "brand", "product_name"]].copy()

    for cfg in MODULE_CONFIGS:
        module_name = cfg["name"]
        fields = cfg["fields"]
        polarities = cfg["polarities"]
        pct_col = f"{module_name}_pct"

        if not fields:
            result[pct_col] = None
            continue

        available = [(f, p) for f, p in zip(fields, polarities) if f in df.columns]
        if not available:
            result[pct_col] = None
            continue

        if len(available) == 1:
            field, polarity = available[0]
            values = pd.to_numeric(df[field], errors="coerce")
            raw_pct = values.rank(pct=True, na_option="keep") * 100
            if polarity == "negative":
                raw_pct = 100 - raw_pct
            result[pct_col] = raw_pct.round(2)
        else:
            sub_pcts = []
            for field, polarity in available:
                values = pd.to_numeric(df[field], errors="coerce")
                raw_pct = values.rank(pct=True, na_option="keep") * 100
                if polarity == "negative":
                    raw_pct = 100 - raw_pct
                sub_pcts.append(raw_pct)

            combined = pd.concat(sub_pcts, axis=1)
            result[pct_col] = combined.mean(axis=1).round(2)

    return result


def compute_advantage_tags(output_df: pd.DataFrame) -> pd.DataFrame:
    """
    按 P75 阈值判断每个配方的市场优势模块。
    >= P75 的模块记为优势，写入 advantage_tags JSON 列。
    """
    import json
    pct_columns = [f"{cfg['name']}_pct" for cfg in MODULE_CONFIGS if cfg['fields']]
    label_map = {f"{cfg['name']}_pct": cfg['label'] for cfg in MODULE_CONFIGS if cfg['fields']}

    # 计算各模块 P75 阈值
    thresholds = {}
    for col in pct_columns:
        vals = pd.to_numeric(output_df[col], errors='coerce').dropna()
        if not vals.empty:
            thresholds[col] = float(vals.quantile(0.75))

    tags_list = []
    for _, row in output_df.iterrows():
        tags = []
        for col in pct_columns:
            val = row.get(col)
            if val is not None and not pd.isna(val) and col in thresholds:
                if float(val) >= thresholds[col]:
                    tags.append(label_map[col])
        tags_list.append(json.dumps(tags, ensure_ascii=False) if tags else None)

    output_df["advantage_tags"] = tags_list
    return output_df, thresholds


# ============================================================
# 写入输出表
# ============================================================

def write_output(engine, output_df: pd.DataFrame):
    pct_columns = [f"{cfg['name']}_pct" for cfg in MODULE_CONFIGS]

    pct_col_defs = ",\n        ".join(
        f"{quote_identifier(c)} DECIMAL(5,2) NULL" for c in pct_columns
    )
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {quote_identifier(OUTPUT_TABLE)} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        formula_id BIGINT NOT NULL,
        source_id BIGINT NULL,
        product_key VARCHAR(255) NULL,
        brand VARCHAR(100) NULL,
        product_name VARCHAR(255) NULL,
        {pct_col_defs},
        advantage_tags JSON NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_formula_id (formula_id),
        KEY idx_product_key (product_key(100))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """

    with engine.begin() as conn:
        conn.execute(text(ddl))
        # 如果表已存在但缺少 advantage_tags 列，自动补加
        existing_cols = {
            row[0] for row in conn.execute(
                text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                     "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tbl"),
                {"tbl": OUTPUT_TABLE},
            ).fetchall()
        }
        if "advantage_tags" not in existing_cols:
            conn.execute(text(
                f"ALTER TABLE {quote_identifier(OUTPUT_TABLE)} ADD COLUMN advantage_tags JSON NULL"
            ))

    all_cols = ["formula_id", "source_id", "product_key", "brand", "product_name"] + pct_columns + ["advantage_tags"]
    insert_cols = ", ".join(quote_identifier(c) for c in all_cols)
    param_cols = ", ".join(f":{c}" for c in all_cols)
    update_exprs = ", ".join(
        f"{quote_identifier(c)} = VALUES({quote_identifier(c)})"
        for c in all_cols
        if c != "formula_id"
    )

    insert_sql = text(f"""
        INSERT INTO {quote_identifier(OUTPUT_TABLE)} ({insert_cols})
        VALUES ({param_cols})
        ON DUPLICATE KEY UPDATE {update_exprs}
    """)

    records = (
        output_df[all_cols]
        .astype(object)
        .where(pd.notnull(output_df[all_cols]), None)
        .to_dict(orient="records")
    )

    if not records:
        print("没有可写入的数据")
        return

    with engine.begin() as conn:
        conn.execute(insert_sql, records)

    print(f"写入/更新 {len(records)} 行 → {OUTPUT_TABLE}")


# ============================================================
# 主流程
# ============================================================

def main():
    engine = get_engine()

    if not table_exists(engine, SOURCE_TABLE):
        print(f"错误: 源表 {DB_NAME}.{SOURCE_TABLE} 不存在")
        sys.exit(1)

    available_columns = get_available_columns(engine, SOURCE_TABLE)

    # ---- 加载数据 ----
    df = load_source_data(engine, available_columns)
    print(f"加载 {len(df)} 个配方（已按 formula_id 去重）")

    if df.empty:
        print("错误: 源表无有效数据")
        sys.exit(1)

    # ---- 模块覆盖情况 ----
    for cfg in MODULE_CONFIGS:
        existing = [f for f in cfg["fields"] if f in available_columns]
        missing = [f for f in cfg["fields"] if f not in available_columns]
        status = f"{len(existing)} 个字段" if existing else "无字段"
        print(f"  [{cfg['label']:6s}] {status}" + (f"  (缺失: {missing})" if missing else ""))

    # ---- 百分位排名 ----
    output_df = compute_percentile_ranks(df)

    # ---- 优势标签 ----
    output_df, thresholds = compute_advantage_tags(output_df)
    print("\nP75 阈值:")
    for col, thr in thresholds.items():
        label = next((cfg['label'] for cfg in MODULE_CONFIGS if f"{cfg['name']}_pct" == col), col)
        print(f"  {label:8s} >= {thr:.1f}")

    # ---- 写入 ----
    write_output(engine, output_df)

    # ---- 打印摘要 ----
    pct_columns = [f"{cfg['name']}_pct" for cfg in MODULE_CONFIGS]
    print("\n前 15 行:")
    display_cols = ["product_key", "brand"] + pct_columns
    print(output_df[display_cols].head(15).to_string(index=False, float_format="%.1f"))

    print("\n各模块分布统计:")
    stats_rows = []
    for cfg, col in zip(MODULE_CONFIGS, pct_columns):
        vals = output_df[col].dropna()
        if vals.empty:
            stats_rows.append({
                "模块": cfg["label"], "count": 0,
                "mean": "—", "p25": "—", "p50": "—", "p75": "—",
            })
            continue
        stats_rows.append({
            "模块": cfg["label"],
            "count": len(vals),
            "mean": f"{vals.mean():.1f}",
            "p25": f"{vals.quantile(0.25):.1f}",
            "p50": f"{vals.quantile(0.50):.1f}",
            "p75": f"{vals.quantile(0.75):.1f}",
        })
    stats_df = pd.DataFrame(stats_rows)
    print(stats_df.to_string(index=False))

    print("\n构建完成 ✓")


if __name__ == "__main__":
    main()
