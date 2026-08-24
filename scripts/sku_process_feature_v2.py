# -*- coding: utf-8 -*-
"""
功能：
1. 从本地 MySQL 读取 SKU 配料表
2. 识别原料关键词
3. 生成 SKU 级工艺结构模块分
4. 生成 main_process_tags
5. 生成 process_structure_summary
6. 生成 candidate_process_watch_tags
7. 生成 candidate_quality_result_tags
8. 生成 candidate_feedback_risk_tags
9. 写入 sku_process_feature_profile

安装依赖：
pip install pandas pymysql sqlalchemy
"""

import argparse
import os
import re
import json
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


# =========================
# 1. 数据库配置
# =========================

DB_CONFIG = {
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "database": os.getenv("MYSQL_DATABASE", "protein_feature_platform"),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
}

DEFAULT_SOURCE_TABLE = os.getenv("SKU_PROCESS_SOURCE_TABLE", "catfood_sku_label_wide")
DEFAULT_OUTPUT_TABLE = os.getenv("SKU_PROCESS_OUTPUT_TABLE", "sku_process_feature_profile")


def make_engine(database: str | None = None):
    return create_engine(
        URL.create(
            "mysql+pymysql",
            username=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            database=database or DB_CONFIG["database"],
            query={"charset": DB_CONFIG["charset"]},
        ),
        pool_pre_ping=True,
    )


def quote_identifier(name: str) -> str:
    return "`{}`".format(str(name).replace("`", "``"))


def table_columns(engine, table_name: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = :table_name
                """
            ),
            {"table_name": table_name},
        ).fetchall()
    return {row[0] for row in rows}


def select_expr(columns: set[str], field: str, alias: str) -> str:
    if field in columns:
        return f"{quote_identifier(field)} AS {quote_identifier(alias)}"
    return f"NULL AS {quote_identifier(alias)}"


def build_source_sql(engine, source_table: str) -> str:
    columns = table_columns(engine, source_table)
    if not columns:
        raise ValueError(f"源表不存在或没有列：{DB_CONFIG['database']}.{source_table}")

    if "sku_id" in columns:
        sku_expr = select_expr(columns, "sku_id", "sku_id")
    elif "product_key" in columns:
        sku_expr = select_expr(columns, "product_key", "sku_id")
    elif "source_id" in columns:
        sku_expr = select_expr(columns, "source_id", "sku_id")
    else:
        raise ValueError(f"{source_table} 缺少 sku_id/product_key/source_id，无法生成 sku_id")

    if "ingredient_text" in columns:
        ingredient_expr = select_expr(columns, "ingredient_text", "ingredient_text")
    elif "ingredient_composition" in columns:
        ingredient_expr = select_expr(columns, "ingredient_composition", "ingredient_text")
    elif "raw_ingredient_text" in columns:
        ingredient_expr = select_expr(columns, "raw_ingredient_text", "ingredient_text")
    else:
        raise ValueError(
            f"{source_table} 缺少 ingredient_text/ingredient_composition/raw_ingredient_text，无法读取配料"
        )

    if "sku_name" in columns:
        sku_name_expr = select_expr(columns, "sku_name", "sku_name")
    elif "product_name" in columns:
        sku_name_expr = select_expr(columns, "product_name", "sku_name")
    else:
        sku_name_expr = "NULL AS `sku_name`"

    if "brand_name" in columns:
        brand_expr = select_expr(columns, "brand_name", "brand_name")
    elif "brand" in columns:
        brand_expr = select_expr(columns, "brand", "brand_name")
    else:
        brand_expr = "NULL AS `brand_name`"

    return f"""
        SELECT
            {sku_expr},
            {sku_name_expr},
            {brand_expr},
            {ingredient_expr}
        FROM {quote_identifier(source_table)}
        WHERE {ingredient_expr.split(' AS ')[0]} IS NOT NULL
          AND {ingredient_expr.split(' AS ')[0]} <> ''
    """


def build_formula_source_sql(engine) -> str:
    """构建配方粒度数据源 SQL：从 catfood_standard_formula + product/brand JOIN 获取结构化配料。"""
    label_db = os.getenv("LABEL_SOURCE_DATABASE", "csv_labeling")
    return f"""
        SELECT
            f.formula_id AS sku_id,
            CONCAT(b.standard_brand_name, '/', p.standard_product_name) AS sku_name,
            b.standard_brand_name AS brand_name,
            f.normalized_ingredients_json AS ingredient_text
        FROM {label_db}.catfood_standard_formula f
        JOIN {label_db}.catfood_standard_product p ON f.product_id = p.product_id
        JOIN {label_db}.catfood_standard_brand b ON p.brand_id = b.brand_id
        WHERE f.normalized_ingredients_json IS NOT NULL
          AND f.normalized_ingredients_json != '[]'
          AND f.normalized_ingredients_json != 'null'
    """


# =========================
# 2. 工艺结构规则 (v2: 分类表 + 关键词兜底)
# =========================

# 模块默认 base_score
MODULE_DEFAULT_BASE_SCORE = {
    "moisture_drying_score": 2.0,
    "animal_protein_powder_score": 1.8,
    "plant_protein_score": 1.8,
    "starch_extrusion_score": 1.5,
    "oil_spray_score": 1.8,
    "oxidation_sensitivity_score": 2.0,
    "palatability_process_score": 1.7,
    "fermentation_substrate_score": 1.6,
    "fiber_structure_score": 1.5,
    "water_binding_gel_score": 2.0,
    "mineral_powder_score": 1.0,
}

# 模块 → 中文 tag + 三类候选标签
MODULE_TAG_MAP = {
    "moisture_drying_score": {
        "tag": "高含水动物基质",
        "process_watch_tags": ["原料含水控制", "混合水分控制", "干燥负荷控制"],
        "quality_result_tags": ["含水率波动", "颗粒稳定性波动", "批次质构波动"],
        "feedback_risk_tags": ["适口性波动", "呕吐反馈观察"]
    },
    "animal_protein_powder_score": {
        "tag": "动物蛋白干粉基质",
        "process_watch_tags": ["粉体混合均匀性", "粉体流动性", "批次气味控制"],
        "quality_result_tags": ["粉体结构稳定性", "气味波动", "颗粒密实度波动"],
        "feedback_risk_tags": ["适口性波动", "呕吐反馈观察"]
    },
    "plant_protein_score": {
        "tag": "植物蛋白补强",
        "process_watch_tags": ["粉体混合均匀性", "植物蛋白添加比例", "粉体结构控制"],
        "quality_result_tags": ["粉体结构复杂度升高", "颗粒密实度波动", "适口性波动"],
        "feedback_risk_tags": ["软便反馈观察", "呕吐反馈观察", "采食意愿波动"]
    },
    "starch_extrusion_score": {
        "tag": "淀粉膨化支撑",
        "process_watch_tags": ["淀粉糊化控制", "膨化稳定性", "挤压参数控制"],
        "quality_result_tags": ["颗粒硬度波动", "膨化度波动", "碎粉率波动"],
        "feedback_risk_tags": ["软便反馈观察", "便便成形波动"]
    },
    "oil_spray_score": {
        "tag": "后喷涂油脂结构",
        "process_watch_tags": ["喷涂量控制", "喷涂均匀性", "冷却后表油控制"],
        "quality_result_tags": ["表面油脂感", "油脂迁移", "气味波动"],
        "feedback_risk_tags": ["黑下巴外部诱因观察", "软便反馈观察", "采食意愿波动"]
    },
    "oxidation_sensitivity_score": {
        "tag": "氧化敏感脂肪",
        "process_watch_tags": ["抗氧化体系控制", "储存氧暴露控制", "喷涂后稳定性控制"],
        "quality_result_tags": ["氧化酸败风险", "气味波动", "油脂稳定性波动"],
        "feedback_risk_tags": ["黑下巴外部诱因观察", "采食意愿波动"]
    },
    "palatability_process_score": {
        "tag": "风味适口增强结构",
        "process_watch_tags": ["风味添加均匀性", "热加工稳定性", "批次风味控制"],
        "quality_result_tags": ["气味波动", "适口性波动", "风味强度波动"],
        "feedback_risk_tags": ["采食意愿波动", "呕吐反馈观察"]
    },
    "fermentation_substrate_score": {
        "tag": "益生元发酵底物",
        "process_watch_tags": ["益生元添加比例", "吸湿结块控制", "混合均匀性"],
        "quality_result_tags": ["吸湿结块风险", "发酵底物比例偏高", "粉体稳定性波动"],
        "feedback_risk_tags": ["软便反馈观察", "便便成形波动"]
    },
    "fiber_structure_score": {
        "tag": "纤维结构支持",
        "process_watch_tags": ["纤维添加比例", "粉体结构控制", "颗粒成型控制"],
        "quality_result_tags": ["颗粒硬度波动", "便便成形波动", "碎粉率波动"],
        "feedback_risk_tags": ["软便反馈观察", "便便成形波动"]
    },
    "water_binding_gel_score": {
        "tag": "高吸水胶质结构",
        "process_watch_tags": ["加水量控制", "混合黏度控制", "挤压压力控制"],
        "quality_result_tags": ["吸水膨胀压力", "颗粒硬度波动", "质构波动"],
        "feedback_risk_tags": ["便便成形波动", "软便反馈观察"]
    },
    "mineral_powder_score": {
        "tag": "矿物粉体结构",
        "process_watch_tags": ["矿物粉体分散性", "粉体流动性", "灰分水平控制"],
        "quality_result_tags": ["颗粒硬度影响", "粉体流动性波动", "灰分水平波动"],
        "feedback_risk_tags": ["适口性波动"]
    },
}

# 旧关键词规则（仅保留 module 映射，第三层兜底用）
PROCESS_RULES = [
    {"keywords": ["冻鸡肉", "鲜鸡肉", "冻鸭肉", "鲜鸭肉", "冻牛肉", "鲜牛肉", "鲜肉", "冻肉"], "module": "moisture_drying_score"},
    {"keywords": ["鸡肉粉", "鸭肉粉", "鱼粉", "牛肉粉", "肉粉"], "module": "animal_protein_powder_score"},
    {"keywords": ["玉米蛋白粉", "小麦蛋白粉", "豌豆蛋白", "马铃薯蛋白", "大豆粕", "豆粕"], "module": "plant_protein_score"},
    {"keywords": ["小麦", "大米", "玉米", "糙米", "燕麦", "木薯", "马铃薯", "红薯", "豌豆粉"], "module": "starch_extrusion_score"},
    {"keywords": ["鸡油", "鸭油", "牛油", "动物脂肪", "禽脂"], "module": "oil_spray_score"},
    {"keywords": ["鱼油", "亚麻籽油", "藻油", "三文鱼油"], "module": "oxidation_sensitivity_score"},
    {"keywords": ["鸡肝水解粉", "水解鸡肝", "水解蛋白", "酵母抽提物", "酵母提取物", "诱食剂", "肝粉"], "module": "palatability_process_score"},
    {"keywords": ["菊糖", "低聚果糖", "低聚半乳糖", "甘露寡糖", "果寡糖"], "module": "fermentation_substrate_score"},
    {"keywords": ["甜菜粕", "车前子", "苜蓿", "纤维素", "南瓜", "苹果纤维"], "module": "fiber_structure_score"},
    {"keywords": ["车前子", "瓜尔胶", "黄原胶", "魔芋粉", "果胶"], "module": "water_binding_gel_score"},
    {"keywords": ["矿物质", "碳酸钙", "磷酸氢钙", "氯化钠", "硫酸锌", "硫酸铜"], "module": "mineral_powder_score"},
]

MODULE_COLUMNS = [
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

# 分类表列名 → MODULE_COLUMNS 映射
_TABLE_COL_TO_MODULE = {
    "moisture_drying": "moisture_drying_score",
    "animal_protein_powder": "animal_protein_powder_score",
    "plant_protein": "plant_protein_score",
    "starch_extrusion": "starch_extrusion_score",
    "oil_spray": "oil_spray_score",
    "oxidation_sensitivity": "oxidation_sensitivity_score",
    "palatability_process": "palatability_process_score",
    "fermentation_substrate": "fermentation_substrate_score",
    "fiber_structure": "fiber_structure_score",
    "water_binding_gel": "water_binding_gel_score",
    "mineral_powder": "mineral_powder_score",
}

# 分类表内存缓存
_INGREDIENT_MODULE_CACHE: dict = {}
_CACHE_LOADED = False


def load_ingredient_module_cache(engine=None):
    """从 catfood_ingredient_process_module 加载分类表到内存。"""
    global _INGREDIENT_MODULE_CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return

    if engine is None:
        engine = make_engine()

    table_cols = [
        "ingredient_name", "moisture_drying", "animal_protein_powder",
        "plant_protein", "starch_extrusion", "oil_spray",
        "oxidation_sensitivity", "palatability_process",
        "fermentation_substrate", "fiber_structure",
        "water_binding_gel", "mineral_powder",
    ]
    col_sql = ", ".join(table_cols)

    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {col_sql} FROM catfood_ingredient_process_module")
        ).fetchall()

    for row in rows:
        name = row[0]
        modules = {}
        for i, table_col in enumerate(table_cols[1:], start=1):
            module_col = _TABLE_COL_TO_MODULE.get(table_col)
            if module_col and row[i]:
                modules[module_col] = True
        _INGREDIENT_MODULE_CACHE[name] = modules

    _CACHE_LOADED = True
    print(f"[分类表] 已加载 {len(_INGREDIENT_MODULE_CACHE)} 个原料分类到内存")


# =========================
# 3. 配料解析函数
# =========================

def normalize_text(text: str) -> str:
    if pd.isna(text):
        return ""

    text = str(text)
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("，", "、").replace(",", "、")
    text = text.replace("；", "、").replace(";", "、")
    text = text.replace("：", ":")
    return text


def split_ingredients(ingredient_text: str):
    """
    将配料文本拆成结构化原料列表。
    """
    text = normalize_text(ingredient_text)
    parts = [p.strip() for p in text.split("、") if p.strip()]

    results = []

    for idx, part in enumerate(parts, start=1):
        percent = None

        m = re.search(r"([\d.]+)\s*%", part)
        if m:
            percent = float(m.group(1))

        name = re.sub(r"\(.*?\)", "", part).strip()

        results.append({
            "name": name,
            "rank": idx,
            "percent": percent
        })

    return results


def rank_weight(rank: int) -> float:
    """
    配料顺位权重。
    """
    if rank <= 3:
        return 1.0
    elif rank <= 6:
        return 0.7
    elif rank <= 10:
        return 0.4
    else:
        return 0.2


def percent_weight(percent):
    """
    百分比权重。
    有明确百分比时，用百分比修正贡献强度。
    """
    if percent is None:
        return 1.0
    if percent >= 10:
        return 1.2
    elif percent >= 5:
        return 1.0
    elif percent >= 1:
        return 0.5
    else:
        return 0.2


# =========================
# 4. 规则匹配函数 (v2: 分类表 + 关键词兜底)
# =========================

def _build_rule_dict(mod: str) -> dict:
    """根据模块列名构建完整 rule dict。"""
    info = MODULE_TAG_MAP[mod]
    return {
        "module": mod,
        "tag": info["tag"],
        "base_score": MODULE_DEFAULT_BASE_SCORE[mod],
        "process_watch_tags": info["process_watch_tags"],
        "quality_result_tags": info["quality_result_tags"],
        "feedback_risk_tags": info["feedback_risk_tags"],
    }


def match_process_rules(ingredient_name: str) -> list:
    """两层匹配：分类表精确匹配 → 旧关键词 substring 兜底。
    返回 list of rule dict，每个包含 module/tag/base_score/process_watch_tags 等。
    """
    if not _CACHE_LOADED:
        load_ingredient_module_cache()

    # --- 第一层：分类表精确匹配 ---
    if ingredient_name in _INGREDIENT_MODULE_CACHE:
        modules = _INGREDIENT_MODULE_CACHE[ingredient_name]
        return [
            _build_rule_dict(mod)
            for mod in MODULE_COLUMNS
            if modules.get(mod)
        ]

    # --- 第二层：旧关键词 substring 兜底 ---
    matched = []
    for rule in PROCESS_RULES:
        for kw in rule["keywords"]:
            if kw in ingredient_name:
                matched.append(_build_rule_dict(rule["module"]))
                break

    return matched


# =========================
# 5. 标签聚合函数
# =========================

def aggregate_tags_by_contribution(detail_rows, tag_field: str, top_n: int = 8):
    """
    对 process_watch_tags / quality_result_tags / feedback_risk_tags 按贡献分聚合排序。
    """

    rows = []

    for item in detail_rows:
        contribution = item.get("contribution", 0)
        tags = item.get(tag_field, [])

        for tag in tags:
            rows.append({
                "tag": tag,
                "contribution": contribution
            })

    if not rows:
        return []

    df = pd.DataFrame(rows)

    tags = (
        df.groupby("tag")["contribution"]
        .sum()
        .sort_values(ascending=False)
        .head(top_n)
        .index
        .tolist()
    )

    return tags


# =========================
# 6. 生成结构摘要
# =========================

def build_process_structure_summary(scores: dict) -> str:
    """
    根据模块分组合，生成业务可读的工艺结构画像。
    """

    parts = []

    has_moisture_meat = scores.get("moisture_drying_score", 0) > 0
    has_animal_powder = scores.get("animal_protein_powder_score", 0) > 0
    has_plant_protein = scores.get("plant_protein_score", 0) > 0
    has_starch = scores.get("starch_extrusion_score", 0) > 0
    has_oil = scores.get("oil_spray_score", 0) > 0
    has_oxidation = scores.get("oxidation_sensitivity_score", 0) > 0
    has_palatability = scores.get("palatability_process_score", 0) > 0
    has_fermentation = scores.get("fermentation_substrate_score", 0) > 0
    has_fiber = scores.get("fiber_structure_score", 0) > 0
    has_gel = scores.get("water_binding_gel_score", 0) > 0

    if has_moisture_meat and has_animal_powder:
        parts.append("冻/鲜肉+肉粉复合蛋白")
    elif has_moisture_meat:
        parts.append("高含水肉源结构")
    elif has_animal_powder:
        parts.append("动物蛋白干粉结构")

    if has_plant_protein:
        parts.append("植物蛋白补强")

    if has_starch:
        parts.append("谷物/淀粉膨化支撑")

    if has_oil and has_oxidation:
        parts.append("油脂后喷涂+氧化敏感脂肪")
    elif has_oil:
        parts.append("油脂后喷涂")
    elif has_oxidation:
        parts.append("氧化敏感脂肪")

    if has_palatability:
        parts.append("风味适口增强")

    if has_fermentation and has_fiber:
        parts.append("发酵底物+纤维辅助")
    elif has_fermentation:
        parts.append("益生元发酵底物辅助")
    elif has_fiber:
        parts.append("纤维结构辅助")

    if has_gel:
        parts.append("高吸水胶质干预")

    if not parts:
        return "暂未识别出明显工艺结构"

    return " / ".join(parts)


# =========================
# 7. 构建 SKU 工艺画像
# =========================

def build_sku_process_profile(row, formula_mode: bool = False):
    ingredient_text = row["ingredient_text"]

    if formula_mode:
        # 配方模式：ingredient_text 是 JSON 数组
        try:
            items = json.loads(ingredient_text) if isinstance(ingredient_text, str) else ingredient_text
            ingredients = [
                {"name": str(item).strip(), "rank": idx, "percent": None}
                for idx, item in enumerate(items, start=1)
                if str(item).strip()
            ]
        except (json.JSONDecodeError, TypeError):
            ingredients = []
    else:
        # SKU 模式：ingredient_text 是原始配料文本
        ingredients = split_ingredients(ingredient_text)

    scores = {col: 0.0 for col in MODULE_COLUMNS}
    tag_details = []

    for ing in ingredients:
        name = ing["name"]
        rank = ing["rank"]
        percent = ing["percent"]

        r_weight = rank_weight(rank)
        p_weight = percent_weight(percent)

        rules = match_process_rules(name)

        for rule in rules:
            module = rule["module"]
            base_score = rule["base_score"]

            contribution = base_score * r_weight * p_weight
            scores[module] += contribution

            tag_details.append({
                "ingredient_name": name,
                "rank": rank,
                "percent": percent,
                "matched_tag": rule["tag"],
                "module": module,
                "base_score": base_score,
                "rank_weight": r_weight,
                "percent_weight": p_weight,
                "contribution": round(contribution, 4),

                # 新增三类标签明细
                "process_watch_tags": rule.get("process_watch_tags", []),
                "quality_result_tags": rule.get("quality_result_tags", []),
                "feedback_risk_tags": rule.get("feedback_risk_tags", [])
            })

    # =========================
    # main_process_tags
    # 主体工艺属性标签：默认只看 rank <= 10，避免后排微量成分过度影响主体结构
    # =========================

    if tag_details:
        tag_df = pd.DataFrame(tag_details)

        main_tag_df = tag_df[tag_df["rank"] <= 10].copy()

        if main_tag_df.empty:
            main_tag_df = tag_df.copy()

        main_process_tags = (
            main_tag_df.groupby("matched_tag")["contribution"]
            .sum()
            .sort_values(ascending=False)
            .head(5)
            .index
            .tolist()
        )
    else:
        main_process_tags = []

    # =========================
    # 三类候选标签
    # =========================

    candidate_process_watch_tags = aggregate_tags_by_contribution(
        tag_details,
        tag_field="process_watch_tags",
        top_n=8
    )

    candidate_quality_result_tags = aggregate_tags_by_contribution(
        tag_details,
        tag_field="quality_result_tags",
        top_n=8
    )

    candidate_feedback_risk_tags = aggregate_tags_by_contribution(
        tag_details,
        tag_field="feedback_risk_tags",
        top_n=8
    )

    # =========================
    # process_structure_summary
    # =========================

    process_structure_summary = build_process_structure_summary(scores)

    # =========================
    # 主导模块
    # =========================

    sorted_modules = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    main_process_modules = [
        {
            "module": module,
            "score": round(score, 4)
        }
        for module, score in sorted_modules
        if score > 0
    ][:3]

    result = {
        "sku_id": row["sku_id"],
        "sku_name": row.get("sku_name", None),
        "brand_name": row.get("brand_name", None),
        "ingredient_text": ingredient_text,

        # 模块分，给后续相似度/聚类/排序用
        **{k: round(v, 4) for k, v in scores.items()},

        # 核心中文字段
        "main_process_tags": json.dumps(main_process_tags, ensure_ascii=False),
        "process_structure_summary": process_structure_summary,

        # 新增三类候选标签
        "candidate_process_watch_tags": json.dumps(candidate_process_watch_tags, ensure_ascii=False),
        "candidate_quality_result_tags": json.dumps(candidate_quality_result_tags, ensure_ascii=False),
        "candidate_feedback_risk_tags": json.dumps(candidate_feedback_risk_tags, ensure_ascii=False),

        # 辅助追溯字段
        "main_process_modules": json.dumps(main_process_modules, ensure_ascii=False),
        "process_tag_details": json.dumps(tag_details, ensure_ascii=False)
    }

    return result


# =========================
# 8. 主程序
# =========================

def parse_args():
    parser = argparse.ArgumentParser(description="生成 SKU/配方 工艺结构画像并写入 MySQL")
    parser.add_argument("--source-table", default=DEFAULT_SOURCE_TABLE, help="源表名，默认 catfood_sku_label_wide")
    parser.add_argument("--output-table", default=DEFAULT_OUTPUT_TABLE, help="输出表名，默认 sku_process_feature_profile")
    parser.add_argument("--if-exists", choices=["replace", "append", "fail"], default="replace")
    parser.add_argument("--limit", type=int, default=0, help="调试用限制行数，0 表示不限制")
    parser.add_argument("--formula-mode", action="store_true",
                        help="配方模式：从 catfood_standard_formula.normalized_ingredients_json 读取")
    return parser.parse_args()


def main():
    args = parse_args()
    engine = make_engine()

    # 加载原料分类表到内存
    load_ingredient_module_cache(engine)

    if args.formula_mode:
        sql = build_formula_source_sql(engine)
        mode_label = "配方(formula)"
    else:
        sql = build_source_sql(engine, args.source_table)
        mode_label = f"SKU({args.source_table})"

    if args.limit and args.limit > 0:
        sql = f"{sql}\nLIMIT {int(args.limit)}"

    print(f"模式: {mode_label}；写入目标表: {DB_CONFIG['database']}.{args.output_table}")

    raw_df = pd.read_sql(sql, engine)

    if raw_df.empty:
        print("没有读取到配料数据")
        return

    profile_rows = []
    for _, row in raw_df.iterrows():
        profile_rows.append(build_sku_process_profile(row, formula_mode=args.formula_mode))

    profile_df = pd.DataFrame(profile_rows)

    profile_df.to_sql(
        args.output_table,
        engine,
        if_exists=args.if_exists,
        index=False
    )

    print(f"已生成 {DB_CONFIG['database']}.{args.output_table}")
    print(f"共处理 {mode_label} 数量：{len(profile_df)}")

    print("\n工艺结构画像示例：")
    print(
        profile_df[
            [
                "sku_id",
                "sku_name",
                "main_process_tags",
                "process_structure_summary",
                "candidate_process_watch_tags",
                "candidate_quality_result_tags",
                "candidate_feedback_risk_tags"
            ]
        ].head(10)
    )


if __name__ == "__main__":
    main()
