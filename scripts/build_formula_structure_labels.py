#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_formula_structure_labels.py
基于 protein_source_aggregate / catfood_fiber_feature_json / catfood_fat_material_features
生成配方粒度的结构标签表 (catfood_formula_structure_labels)

标签维度:
  配方一级 → 配方二级 → 工艺一级 → 工艺二级
"""

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config

OUTPUT_TABLE = "catfood_formula_structure_labels"

# ============================================================
# 标签枚举定义 (配方二级 → 工艺映射)
# ============================================================
LABEL_MAP = {
    # 配方一级: 蛋白结构
    "蛋白结构": {
        "高鲜肉结构":      ("原料预处理",     "高含水原料预处理"),
        "肉粉支撑结构":    ("混合与均匀控制",  "干粉混合均匀控制"),
        "鲜肉+肉粉复合结构": ("膨化与成型控制", "鲜肉/干粉协同成型"),
        "多肉源结构":      ("混合与均匀控制",  "多原料混合一致性"),
    },
    # 配方一级: 碳水/淀粉结构
    "碳水/淀粉结构": {
        "谷物膨化结构":  ("调质熟化控制", "谷物淀粉糊化控制"),
        "薯类膨化结构":  ("调质熟化控制", "薯类淀粉熟化控制"),
        "豆类淀粉结构":  ("调质熟化控制", "豆类熟化控制"),
        "低淀粉结构":   ("膨化与成型控制", "低淀粉成型控制"),
    },
    # 配方一级: 脂肪结构
    "脂肪结构": {
        "高动物脂肪结构":  ("后喷涂与适口控制", "油脂后喷涂控制"),
        "高不饱和脂肪结构": ("稳定与品质控制",  "氧化保护控制"),
        "多油脂复合结构":  ("后喷涂与适口控制", "多油脂喷涂与均匀性控制"),
    },
    # 配方一级: 纤维结构
    "纤维结构": {
        "不溶性纤维结构": ("膨化与成型控制", "高纤维成型控制"),
        "高吸水纤维结构": ("干燥与水分控制", "水分竞争控制"),
        "胶质纤维结构":  ("原料预处理",    "胶体水化与分散控制"),
    },
    # 配方一级: 肠道功能结构
    "肠道功能结构": {
        "益生元发酵结构":    ("混合与均匀控制", "微量功能物均匀添加"),
        "益生元+纤维复合结构": ("混合与均匀控制", "多功能物协同混合"),
    },
    # 配方一级: 适口性结构
    "适口性结构": {
        "水解蛋白增味结构": ("后喷涂与适口控制", "水解物喷涂/混合控制"),
        "酵母风味结构":   ("混合与均匀控制",  "风味物均匀添加"),
        "表面诱食结构":   ("后喷涂与适口控制", "表面喷涂控制"),
    },
    # 配方一级: 矿物/微量营养结构
    "矿物/微量营养结构": {
        "常量矿物结构":  ("混合与均匀控制", "矿物粉体分散控制"),
        "微量矿物结构":  ("混合与均匀控制", "微量预混均匀性控制"),
        "多矿物复合结构": ("混合与均匀控制", "预混料一致性控制"),
    },
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


# ============================================================
# 加载三张源表
# ============================================================

def load_protein(engine) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT formula_id, product_key, brand_name, product_name, "
        "primary_meat_source_type, secondary_meat_source_type, meat_source_complexity, "
        "animal_sources, protein_source_details, "
        "plant_protein_interference, hydrolyzed_protein_role "
        "FROM protein_source_aggregate",
        engine,
    )


def load_fiber(engine) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT formula_id, raw_ingredient_text, ingredient_feature_json, "
        "starch_ingredients_json "
        "FROM catfood_fiber_feature_json",
        engine,
    )


def load_fat(engine) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT formula_id, fat_sources, fat_source_types, "
        "antioxidant_sources, micronutrient_sources, micronutrient_types, "
        "omega6_sources, omega3_sources "
        "FROM catfood_fat_material_features",
        engine,
    )


# ============================================================
# 配方二级标签分类函数
# ============================================================

def classify_protein_structure(row: pd.Series) -> list:
    """蛋白结构 → 配方二级"""
    tags = []
    primary = str(row.get("primary_meat_source_type") or "")
    secondary = str(row.get("secondary_meat_source_type") or "")
    complexity = str(row.get("meat_source_complexity") or "")
    sources = str(row.get("animal_sources") or "")

    # 高鲜肉结构: 主要蛋白为鲜肉/冻肉，且次要蛋白不含大量肉粉
    is_fresh_primary = primary in ("鲜肉为主", "鲜肉/冻肉为主", "冻肉为主")
    is_meal_primary = primary == "肉粉为主"
    has_meal_secondary = "肉粉" in secondary

    if is_fresh_primary and has_meal_secondary:
        tags.append("鲜肉+肉粉复合结构")
    elif is_fresh_primary:
        tags.append("高鲜肉结构")
    elif is_meal_primary:
        tags.append("肉粉支撑结构")

    # 多肉源结构: 复杂度为跨类多源/双源，或动物源≥3种
    source_count = len([s for s in sources.replace("、", ",").split(",") if s.strip()])
    if complexity in ("跨类多源",) or source_count >= 3:
        tags.append("多肉源结构")
    elif complexity == "跨类双源" and source_count >= 2:
        tags.append("多肉源结构")

    return tags


def classify_starch_structure(fiber_row: pd.Series) -> list:
    """碳水/淀粉结构 → 配方二级"""
    tags = []
    starch_json = fiber_row.get("starch_ingredients_json")

    if not starch_json or pd.isna(starch_json):
        tags.append("低淀粉结构")
        return tags

    try:
        starches = json.loads(starch_json) if isinstance(starch_json, str) else starch_json
    except Exception:
        tags.append("低淀粉结构")
        return tags

    if not starches:
        tags.append("低淀粉结构")
        return tags

    # 按权重排序（取 weighted_score 最高的主淀粉源）
    categories = [item.get("category", "") for item in starches]
    weights = {item.get("category", ""): item.get("weighted_score", 0) for item in starches}

    # 主淀粉类别 = weighted_score 最高的
    if weights:
        main_cat = max(weights, key=weights.get)
    else:
        main_cat = categories[0] if categories else ""

    if "谷物淀粉来源" in main_cat:
        tags.append("谷物膨化结构")
    elif "薯类淀粉来源" in main_cat:
        tags.append("薯类膨化结构")
    elif "豆类碳水来源" in main_cat:
        tags.append("豆类淀粉结构")
    elif any("淀粉" in c or "碳水" in c for c in categories):
        # 有高淀粉粉类或精制淀粉，但不是谷/薯/豆
        if any("薯类" in c for c in categories):
            tags.append("薯类膨化结构")
        elif any("谷物" in c for c in categories):
            tags.append("谷物膨化结构")
        elif any("豆类" in c for c in categories):
            tags.append("豆类淀粉结构")
        else:
            tags.append("低淀粉结构")
    else:
        tags.append("低淀粉结构")

    return tags


def classify_fat_structure(fat_row: pd.Series) -> list:
    """脂肪结构 → 配方二级"""
    tags = []
    fat_types = str(fat_row.get("fat_source_types") or "")
    omega3 = str(fat_row.get("omega3_sources") or "")
    fat_sources = str(fat_row.get("fat_sources") or "")

    # 多油脂复合结构: 动物+植物脂肪，或多种动物油脂（牛油+鸡油+鱼油等）
    fat_items = [s.strip() for s in fat_sources.replace("、", ",").split(",") if s.strip()]
    has_plant_fat = "植物脂肪" in fat_types

    # 高不饱和脂肪结构: 有 omega3 来源（鱼油、三文鱼、磷虾等）
    high_pufa_keywords = ["鱼油", "三文鱼", "鳕鱼", "磷虾", "亚麻籽", "藻油", "鲱鱼", "沙丁鱼", "白鱼", "比目鱼", "鲭鱼"]
    has_high_pufa = any(kw in omega3 for kw in high_pufa_keywords)

    # 多油脂复合: 动物+植物，或≥3种不同油脂
    is_multi_oil = has_plant_fat or len(fat_items) >= 3

    if is_multi_oil and has_high_pufa:
        tags.append("多油脂复合结构")
        tags.append("高不饱和脂肪结构")
    elif is_multi_oil:
        tags.append("多油脂复合结构")
    elif has_high_pufa:
        tags.append("高不饱和脂肪结构")
    elif fat_types:
        tags.append("高动物脂肪结构")

    return tags


def classify_fiber_structure(fiber_row: pd.Series) -> list:
    """纤维结构 → 配方二级"""
    tags = []
    feature_json = fiber_row.get("ingredient_feature_json")

    if not feature_json or pd.isna(feature_json):
        return tags

    try:
        data = json.loads(feature_json) if isinstance(feature_json, str) else feature_json
    except Exception:
        return tags

    details = data.get("ingredient_tag_detail", {})
    if not details:
        return tags

    # 统计各纤维功能出现次数
    fiber_funcs = Counter()
    for ing, info in details.items():
        for func in (info.get("fiber_functions") or []):
            fiber_funcs[func] += 1

    # 高吸水纤维结构: 车前子等吸水性强（纤维功能含"吸水成形"）
    has_high_absorb = fiber_funcs.get("吸水成形", 0) >= 2
    # 胶质纤维结构: 瓜尔胶/果胶/魔芋等（ingredient 名含关键字）
    gel_keywords = ["瓜尔", "果胶", "魔芋", "黄原胶", "卡拉胶", "结冷胶", "胶"]
    has_gel = any(
        any(kw in ing for kw in gel_keywords)
        for ing in details.keys()
    )
    # 不溶性纤维结构: 纤维素、苹果纤维等（纤维骨架 / 缓冲刺激）
    has_insoluble = (
        fiber_funcs.get("增加粪便骨架", 0) >= 1
        or fiber_funcs.get("缓冲刺激", 0) >= 2
    )

    if has_high_absorb:
        tags.append("高吸水纤维结构")
    if has_gel:
        tags.append("胶质纤维结构")
    if has_insoluble and not has_high_absorb:
        tags.append("不溶性纤维结构")

    return tags


def classify_gut_structure(fiber_row: pd.Series) -> list:
    """肠道功能结构 → 配方二级"""
    tags = []
    feature_json = fiber_row.get("ingredient_feature_json")

    if not feature_json or pd.isna(feature_json):
        return tags

    try:
        data = json.loads(feature_json) if isinstance(feature_json, str) else feature_json
    except Exception:
        return tags

    details = data.get("ingredient_tag_detail", {})

    has_prebiotic = False
    has_fiber = False

    for ing, info in details.items():
        cat = info.get("ingredient_category", "")
        pre_funcs = info.get("prebiotic_functions") or []
        fiber_funcs = info.get("fiber_functions") or []

        if "益生元" in cat or pre_funcs:
            has_prebiotic = True
        if "膳食纤维" in cat or fiber_funcs:
            has_fiber = True

    if has_prebiotic and has_fiber:
        tags.append("益生元+纤维复合结构")
    elif has_prebiotic:
        tags.append("益生元发酵结构")

    return tags


def classify_palatability_structure(protein_row: pd.Series, fat_row: pd.Series) -> list:
    """适口性结构 → 配方二级"""
    tags = []
    hydro_role = str(protein_row.get("hydrolyzed_protein_role") or "")
    fat_sources = str(fat_row.get("fat_sources") or "")
    ingredient_text = str(protein_row.get("protein_source_details") or "")

    # 水解蛋白增味结构
    if hydro_role and hydro_role != "nan":
        tags.append("水解蛋白增味结构")

    # 酵母风味结构: 原料含酵母相关词
    yeast_keywords = ["酵母", "啤酒酵母", "酵母提取物", "酵母细胞壁"]
    if any(kw in ingredient_text or kw in fat_sources for kw in yeast_keywords):
        tags.append("酵母风味结构")

    # 表面诱食结构: 鸡油/牛油等动物油脂明确喷涂（脂肪来源≥2种动物油脂）
    fat_items = [s.strip() for s in fat_sources.replace("、", ",").split(",") if s.strip()]
    oily_fats = [f for f in fat_items if any(kw in f for kw in ["鸡油", "牛油", "鸭油", "羊油", "猪油"])]
    if len(oily_fats) >= 1 and len(fat_items) >= 2:
        tags.append("表面诱食结构")

    return tags


def classify_mineral_structure(fat_row: pd.Series) -> list:
    """矿物/微量营养结构 → 配方二级"""
    tags = []
    micro_types = str(fat_row.get("micronutrient_types") or "")
    micro_sources = str(fat_row.get("micronutrient_sources") or "")
    anti_sources = str(fat_row.get("antioxidant_sources") or "")

    if not micro_types and micro_types == "nan":
        return tags
    if not micro_sources and micro_sources == "nan":
        return tags

    # 判断矿物复杂度
    micro_type_list = [t.strip() for t in micro_types.replace("、", ",").split(",") if t.strip()]
    has_trace = any("微量" in t or "矿物质" in t or "矿物" in t for t in micro_type_list)
    has_organ = any("内脏" in t or "组织" in t for t in micro_type_list)
    has_fortified = any("强化" in t for t in micro_type_list)

    # 多矿物复合结构: ≥3种类型
    if len(micro_type_list) >= 3:
        tags.append("多矿物复合结构")
    elif has_trace or has_fortified:
        # 微量矿物结构
        tags.append("微量矿物结构")
    elif has_organ:
        # 常量矿物结构: 主要来自动物内脏
        tags.append("常量矿物结构")
    elif micro_sources:
        tags.append("常量矿物结构")

    return tags


# ============================================================
# 主流程
# ============================================================

def main():
    engine = get_engine()

    # 加载
    protein_df = load_protein(engine)
    fiber_df = load_fiber(engine)
    fat_df = load_fat(engine)

    print(f"加载: 蛋白 {len(protein_df)} 行, 纤维 {len(fiber_df)} 行, 脂肪 {len(fat_df)} 行")

    # 以 protein_source_aggregate 为主表，LEFT JOIN 其余两张
    fiber_indexed = fiber_df.set_index("formula_id")
    fat_indexed = fat_df.set_index("formula_id")

    results = []
    for _, prow in protein_df.iterrows():
        fid = prow["formula_id"]
        fiber_row = fiber_indexed.loc[fid] if fid in fiber_indexed.index else pd.Series(dtype=object)
        fat_row = fat_indexed.loc[fid] if fid in fat_indexed.index else pd.Series(dtype=object)

        # 如果有重复 formula_id（多行），取第一行
        if isinstance(fiber_row, pd.DataFrame):
            fiber_row = fiber_row.iloc[0]
        if isinstance(fat_row, pd.DataFrame):
            fat_row = fat_row.iloc[0]

        # 分类各维度
        protein_tags = classify_protein_structure(prow)
        starch_tags = classify_starch_structure(fiber_row)
        fat_tags = classify_fat_structure(fat_row)
        fiber_tags = classify_fiber_structure(fiber_row)
        gut_tags = classify_gut_structure(fiber_row)
        palatability_tags = classify_palatability_structure(prow, fat_row)
        mineral_tags = classify_mineral_structure(fat_row)

        # 组装所有标签
        all_labels = []
        for level1, tags in [
            ("蛋白结构", protein_tags),
            ("碳水/淀粉结构", starch_tags),
            ("脂肪结构", fat_tags),
            ("纤维结构", fiber_tags),
            ("肠道功能结构", gut_tags),
            ("适口性结构", palatability_tags),
            ("矿物/微量营养结构", mineral_tags),
        ]:
            for level2 in tags:
                process_info = LABEL_MAP.get(level1, {}).get(level2, ("", ""))
                all_labels.append({
                    "level1": level1,
                    "level2": level2,
                    "process_level1": process_info[0],
                    "process_level2": process_info[1],
                })

        results.append({
            "formula_id": fid,
            "product_key": prow.get("product_key"),
            "brand": prow.get("brand_name"),
            "product_name": prow.get("product_name"),
            "labels": all_labels,
        })

    # 统计
    total_labels = sum(len(r["labels"]) for r in results)
    print(f"生成 {len(results)} 个配方, 共 {total_labels} 条标签")

    # 各维度分布
    l1_counter = Counter()
    l2_counter = Counter()
    for r in results:
        for lb in r["labels"]:
            l1_counter[lb["level1"]] += 1
            l2_counter[f"{lb['level1']} → {lb['level2']}"] += 1

    print("\n配方一级分布:")
    for k, v in l1_counter.most_common():
        print(f"  {k:15s} {v}")

    print("\n配方二级分布:")
    for k, v in l2_counter.most_common():
        print(f"  {k:35s} {v}")

    # 写入数据库
    write_output(engine, results)

    # 打印样例
    print("\n样例（前 10 个配方）:")
    for r in results[:10]:
        name = str(r["product_key"])[:30]
        label_str = " | ".join(
            f"{lb['level1']}:{lb['level2']}" for lb in r["labels"]
        )
        print(f"  {name:30s} {label_str}")


def write_output(engine, results: list):
    """写入 catfood_formula_structure_labels 表"""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {quote_identifier(OUTPUT_TABLE)} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        formula_id BIGINT NOT NULL,
        product_key VARCHAR(255) NULL,
        brand VARCHAR(100) NULL,
        product_name VARCHAR(255) NULL,
        formula_level1 VARCHAR(50) NOT NULL COMMENT '配方一级标签',
        formula_level2 VARCHAR(50) NOT NULL COMMENT '配方二级标签',
        process_level1 VARCHAR(50) NOT NULL COMMENT '工艺一级标签',
        process_level2 VARCHAR(50) NOT NULL COMMENT '工艺二级标签',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_formula_label (formula_id, formula_level1, formula_level2),
        KEY idx_formula_id (formula_id),
        KEY idx_product_key (product_key(100)),
        KEY idx_level1 (formula_level1)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """

    with engine.begin() as conn:
        conn.execute(text(ddl))
        conn.execute(text(f"DELETE FROM {quote_identifier(OUTPUT_TABLE)}"))

    # 展开标签为行
    rows = []
    for r in results:
        for lb in r["labels"]:
            rows.append({
                "formula_id": r["formula_id"],
                "product_key": r["product_key"],
                "brand": r["brand"],
                "product_name": r["product_name"],
                "formula_level1": lb["level1"],
                "formula_level2": lb["level2"],
                "process_level1": lb["process_level1"],
                "process_level2": lb["process_level2"],
            })

    if not rows:
        print("无数据写入")
        return

    df = pd.DataFrame(rows)
    cols = ["formula_id", "product_key", "brand", "product_name",
            "formula_level1", "formula_level2", "process_level1", "process_level2"]

    insert_cols = ", ".join(quote_identifier(c) for c in cols)
    param_cols = ", ".join(f":{c}" for c in cols)
    update_exprs = ", ".join(
        f"{quote_identifier(c)} = VALUES({quote_identifier(c)})"
        for c in cols if c != "formula_id"
    )

    insert_sql = text(f"""
        INSERT INTO {quote_identifier(OUTPUT_TABLE)} ({insert_cols})
        VALUES ({param_cols})
        ON DUPLICATE KEY UPDATE {update_exprs}
    """)

    records = df[cols].astype(object).where(pd.notnull(df[cols]), None).to_dict(orient="records")

    with engine.begin() as conn:
        conn.execute(insert_sql, records)

    print(f"\n写入 {len(records)} 行 → {OUTPUT_TABLE}")


def quote_identifier(name: str) -> str:
    return f"`{name}`"


if __name__ == "__main__":
    main()
