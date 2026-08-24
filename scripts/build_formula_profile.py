#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_formula_profile.py
合并 catfood_formula_structure_labels + catfood_module_market_ranking
生成配方粒度画像表 catfood_formula_profile

字段:
  formula_id, product_key, brand, product_name
  structure_labels      JSON  结构标签数组 [{level1, level2, process1, process2}]
  market_rankings       JSON  各模块百分位排名 {module: pct}
  advantage_tags        JSON  市场优势标签数组 (>= P75)
  weakness_tags         JSON  市场弱势标签数组 (<= P25)
  profile_summary       TEXT  一句话画像摘要
"""

import json
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config

OUTPUT_TABLE = "catfood_formula_profile"

MODULE_LABELS = {
    "animal_protein_pct": "动物蛋白",
    "starch_carb_pct": "淀粉碳水",
    "fat_pct": "脂肪",
    "fiber_pct": "纤维",
    "prebiotic_pct": "益生元",
    "digestion_support_pct": "消化支持",
    "palatability_pct": "适口性",
    "mineral_micronutrient_pct": "矿物微量营养",
    "functional_nutrition_pct": "功能性营养",
}


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


def build_profile(engine) -> pd.DataFrame:
    """合并两张源表，生成画像 DataFrame"""

    # 1. 加载排名表
    ranking = pd.read_sql(
        "SELECT formula_id, source_id, product_key, brand, product_name, "
        + ", ".join(MODULE_LABELS.keys())
        + ", advantage_tags "
        + "FROM catfood_module_market_ranking",
        engine,
    )

    # 2. 加载结构标签表并聚合
    labels_df = pd.read_sql(
        "SELECT formula_id, formula_level1, formula_level2, "
        "process_level1, process_level2 "
        "FROM catfood_formula_structure_labels",
        engine,
    )

    label_groups = {}
    for fid, group in labels_df.groupby("formula_id"):
        label_groups[fid] = [
            {
                "level1": r["formula_level1"],
                "level2": r["formula_level2"],
                "process1": r["process_level1"],
                "process2": r["process_level2"],
            }
            for _, r in group.iterrows()
        ]

    # 3. 计算 P25 阈值（弱势线）
    p25_thresholds = {}
    p75_thresholds = {}
    for col in MODULE_LABELS:
        vals = pd.to_numeric(ranking[col], errors="coerce").dropna()
        if not vals.empty:
            p25_thresholds[col] = float(vals.quantile(0.25))
            p75_thresholds[col] = float(vals.quantile(0.75))

    # 4. 逐行生成画像
    rows = []
    for _, row in ranking.iterrows():
        fid = row["formula_id"]
        structure = label_groups.get(fid, [])

        # market_rankings: {模块名: 百分位值}
        rankings = {}
        for col, label in MODULE_LABELS.items():
            val = row.get(col)
            if val is not None and not pd.isna(val):
                rankings[label] = round(float(val), 1)

        # advantage_tags 直接从排名表读取
        adv_raw = row.get("advantage_tags")
        advantages = json.loads(adv_raw) if adv_raw and not pd.isna(adv_raw) else []

        # weakness_tags: <= P25 的模块
        weaknesses = []
        for col, label in MODULE_LABELS.items():
            val = row.get(col)
            if val is not None and not pd.isna(val) and col in p25_thresholds:
                if float(val) <= p25_thresholds[col]:
                    weaknesses.append(label)

        # profile_summary: 一句话摘要
        summary = _build_summary(
            row.get("brand", ""),
            row.get("product_name", ""),
            structure,
            rankings,
            advantages,
            weaknesses,
        )

        rows.append({
            "formula_id": fid,
            "source_id": row.get("source_id"),
            "product_key": row.get("product_key"),
            "brand": row.get("brand"),
            "product_name": row.get("product_name"),
            "structure_labels": json.dumps(structure, ensure_ascii=False) if structure else None,
            "market_rankings": json.dumps(rankings, ensure_ascii=False) if rankings else None,
            "advantage_tags": json.dumps(advantages, ensure_ascii=False) if advantages else None,
            "weakness_tags": json.dumps(weaknesses, ensure_ascii=False) if weaknesses else None,
            "profile_summary": summary,
        })

    return pd.DataFrame(rows)


def _build_summary(brand, product_name, structure, rankings, advantages, weaknesses):
    """生成一句话画像摘要"""
    parts = []

    # 1. 结构特征
    if structure:
        # 取配方一级的核心标签（去重后拼接）
        level1_set = []
        for lb in structure:
            if lb["level1"] not in level1_set:
                level1_set.append(lb["level1"])
        level2_short = [lb["level2"] for lb in structure[:4]]  # 取前4个关键二级
        if level2_short:
            parts.append("结构: " + "+".join(level2_short))

    # 2. 市场优势
    if advantages:
        parts.append(f"优势: {'/'.join(advantages)}")

    # 3. 市场弱势
    if weaknesses:
        parts.append(f"短板: {'/'.join(weaknesses)}")

    # 4. 综合评分（所有模块百分位均值）
    if rankings:
        avg = sum(rankings.values()) / len(rankings)
        parts.append(f"综合分位: {avg:.0f}")

    return " | ".join(parts) if parts else ""


def write_output(engine, df: pd.DataFrame):
    """写入画像表"""
    ddl = (
        f"CREATE TABLE IF NOT EXISTS `{OUTPUT_TABLE}` (\n"
        "        id BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
        "        formula_id BIGINT NOT NULL,\n"
        "        source_id BIGINT NULL,\n"
        "        product_key VARCHAR(255) NULL,\n"
        "        brand VARCHAR(100) NULL,\n"
        "        product_name VARCHAR(255) NULL,\n"
        "        structure_labels JSON NULL COMMENT '结构标签数组',\n"
        "        market_rankings JSON NULL COMMENT '各模块百分位排名',\n"
        "        advantage_tags JSON NULL COMMENT '市场优势模块>=P75',\n"
        "        weakness_tags JSON NULL COMMENT '市场弱势模块<=P25',\n"
        "        profile_summary TEXT NULL COMMENT '一句话画像摘要',\n"
        "        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,\n"
        "        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,\n"
        "        UNIQUE KEY uq_formula_id (formula_id),\n"
        "        KEY idx_product_key (product_key(100)),\n"
        "        KEY idx_brand (brand)\n"
        "    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )

    with engine.begin() as conn:
        conn.execute(text(ddl))

    cols = [
        "formula_id", "source_id", "product_key", "brand", "product_name",
        "structure_labels", "market_rankings", "advantage_tags",
        "weakness_tags", "profile_summary",
    ]

    insert_cols = ", ".join(f"`{c}`" for c in cols)
    param_cols = ", ".join(f":{c}" for c in cols)
    update_exprs = ", ".join(
        f"`{c}` = VALUES(`{c}`)" for c in cols if c != "formula_id"
    )

    insert_sql = text(f"""
        INSERT INTO `{OUTPUT_TABLE}` ({insert_cols})
        VALUES ({param_cols})
        ON DUPLICATE KEY UPDATE {update_exprs}
    """)

    records = df[cols].where(pd.notnull(df[cols]), None).to_dict(orient="records")

    if not records:
        print("无数据写入")
        return

    with engine.begin() as conn:
        conn.execute(insert_sql, records)

    print(f"写入 {len(records)} 行 → {OUTPUT_TABLE}")


def main():
    engine = get_engine()
    df = build_profile(engine)
    write_output(engine, df)

    # 统计
    print(f"\n共 {len(df)} 个配方画像")

    has_structure = df["structure_labels"].notna().sum()
    has_advantage = df["advantage_tags"].notna().sum()
    has_weakness = df["weakness_tags"].notna().sum()
    print(f"  有结构标签: {has_structure}")
    print(f"  有市场优势: {has_advantage}")
    print(f"  有市场短板: {has_weakness}")

    # 打印样例
    print("\n画像样例:")
    print("=" * 100)
    for _, row in df.head(8).iterrows():
        name = str(row["product_key"])[:30]
        summary = str(row["profile_summary"])[:95]
        print(f"\n{name:30s} {row.get('brand','')}")
        print(f"  {summary}")


if __name__ == "__main__":
    main()
