# -*- coding: utf-8 -*-
"""
宠析 - 单产品配方画像分析页 + 通义千问 AI 综合解读

功能：
1. 输入产品名称
2. 从本地 MySQL 数据库读取：
   - 六维/九维画像 score
   - 黑下巴风险排序与标签
   - 软便风险排序与标签
3. 生成页面1：
   - 产品摘要
   - 配方画像与底层 score 证据
   - 风险标签解释
   - 适合/需要谨慎的猫咪
   - 通义千问 AI 综合解读按钮（页面最后）
4. 通义千问只负责“解释”，不负责重新打分和重新判定风险。

运行：
python3 feature_score_pipeline_project/pipeline.py product-display
或：
streamlit run feature_score_pipeline_project/apps/product_display.py
"""

import json
import os
import re
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import create_engine, text
from openai import OpenAI

from app_config import get_feature_mysql_config


# =========================================================
# 1. 数据库配置
# =========================================================

_FEATURE_DB_CONFIG = get_feature_mysql_config()
DB_CONFIG = {
    "host": _FEATURE_DB_CONFIG.get("host", "127.0.0.1"),
    "port": int(_FEATURE_DB_CONFIG.get("port", 3306)),
    "user": _FEATURE_DB_CONFIG.get("user", "root"),
    "password": _FEATURE_DB_CONFIG.get("password", ""),
    "database": _FEATURE_DB_CONFIG.get("database", "protein_feature_platform"),
    "charset": _FEATURE_DB_CONFIG.get("charset", "utf8mb4"),
}


def get_engine():
    url = (
        f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
        f"?charset={DB_CONFIG['charset']}"
    )
    return create_engine(url)


# =========================================================
# 2. 表名与字段配置：你的表字段不同，主要改这里
# =========================================================

TABLE_CONFIG = {
    # 六维/九维画像 score 表，来自流水线宽表
    "score_table": "catfood_protein_fat_fiber_score_wide",

    # 黑下巴/软便共用当前项目风险结果表，通过 score_model_version 区分
    "risk_table": "sku_risk_score_result",
    "black_chin_score_model_like": "BLACK_CHIN%",
    "soft_stool_score_model_like": "SOFT_STOOL%",

    # score 表产品名称字段
    "product_name_col": "product_name",

    # 风险表产品名称字段
    "risk_product_name_col": "sku_name",

    # 品牌字段
    "brand_name_col": "brand_name",

    # score 字段
    "score_cols": [
        "protein_quality_score",
        "protein_score",
        "carb_score",
        "fiber_score",
        "fat_score",
        "prebiotic_score",
        "antioxidant_score",
        "protein_structure_score",
        "starch_burden_score",
        "p_total_score",
        "fat_regulation_score",
        "p_buffer",
        "q_feed",
        "q_scfa",
    ],

    # 风险等级字段
    "risk_level_col": "current_pool_risk_level",

    # 排名字段
    "rank_col": "batch_rank",

    # 分位数字段。没有的话可以设为 None
    "percentile_col": "current_pool_percentile",

    # 标签字段。脚本会自动合并这些标签
    "tag_cols": [
        "main_reason_tags",
        "support_reason_tags",
        "fat_detail_tags",
        "all_reason_tags",
    ],

    # 可选：batch_id 字段。没有的话可以设为 None
    "batch_id_col": "batch_id",
}


# =========================================================
# 3. 通义千问配置
# =========================================================

QWEN_CONFIG = {
    # 推荐先用 qwen-plus，成本和效果比较均衡。
    # 你也可以换成 qwen-max、qwen-turbo 等你账号可用的模型。
    "model": "qwen-plus",

    # 阿里云百炼 OpenAI 兼容模式 base_url
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",

    # 控制文案稳定性。越低越稳定，越高越发散。
    "temperature": 0.35,

    # 输出长度上限
    "max_tokens": 1200,
}


def get_qwen_client() -> OpenAI:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未检测到环境变量 DASHSCOPE_API_KEY。请先配置阿里云百炼 API Key。"
        )

    return OpenAI(
        api_key=api_key,
        base_url=QWEN_CONFIG["base_url"],
    )


# =========================================================
# 4. score 展示名与解释
# =========================================================

SCORE_DISPLAY_MAP = {
    "protein_score": {
        "name": "蛋白消化压力",
        "type": "pressure",
        "explain": "蛋白来源越复杂、越重，部分猫越容易出现消化压力或换粮不适。",
    },
    "protein_quality_score": {
        "name": "蛋白质量",
        "type": "protective",
        "explain": "代表动物蛋白占优、来源清晰、消化友好、植物蛋白干扰低等正向质量特征。",
    },
    "protein_structure_score": {
        "name": "蛋白结构负载",
        "type": "pressure",
        "explain": "蛋白结构越复杂、越重，越可能带来消化压力。",
    },
    "carb_score": {
        "name": "碳水/淀粉负担",
        "type": "pressure",
        "explain": "淀粉、豆类、薯类负担越重，肠胃敏感猫越可能不稳定。",
    },
    "starch_burden_score": {
        "name": "淀粉负担",
        "type": "pressure",
        "explain": "淀粉、豆类、薯类负担越重，肠胃敏感猫越可能不稳定。",
    },
    "fiber_score": {
        "name": "纤维支持度",
        "type": "mixed",
        "explain": "纤维可以帮助便便成形和肠道缓冲，但过度发酵时也可能带来软便压力。",
    },
    "fat_score": {
        "name": "脂肪负担",
        "type": "pressure",
        "explain": "油脂压力越高，越可能关联黑下巴、油脂旺盛或脂肪消化压力。",
    },
    "prebiotic_score": {
        "name": "益生元支持",
        "type": "mixed",
        "explain": "益生元可以喂养有益菌，但如果供菌底物过多，也可能带来发酵压力。",
    },
    "p_total_score": {
        "name": "纤维总支持",
        "type": "mixed",
        "explain": "综合反映纤维结构、成形和缓冲支持。",
    },
    "antioxidant_score": {
        "name": "抗氧化保护",
        "type": "protective",
        "explain": "抗氧化支持越强，越有助于抗炎、皮肤稳定和整体保护。",
    },
    "fat_regulation_score": {
        "name": "脂肪调节支持",
        "type": "protective",
        "explain": "代表配方里帮助脂肪代谢、皮肤稳定和氧化压力缓冲的支持。",
    },
    "p_buffer": {
        "name": "肠道缓冲力",
        "type": "protective",
        "explain": "肠道遇到刺激时，这个分数代表配方有没有足够的缓冲垫。",
    },
    "q_feed": {
        "name": "供菌底物",
        "type": "mixed",
        "explain": "代表给肠道菌群提供了多少食物。适量是支持，过高可能变成发酵压力。",
    },
    "q_scfa": {
        "name": "菌群代谢支持",
        "type": "protective",
        "explain": "代表是否有利于形成更稳定、更友好的肠道菌群代谢环境。",
    },
}

SCORE_SCALE_MAX = {
    "protein_quality_score": 1.0,
    "protein_structure_score": 1.0,
    "protein_score": 1.0,
    "fat_score": 1.0,
    "fat_regulation_score": 1.0,
    "antioxidant_score": 1.0,
    "starch_burden_score": 5.0,
    "carb_score": 5.0,
    "p_total_score": 5.0,
    "fiber_score": 5.0,
    "p_buffer": 5.0,
    "q_feed": 5.0,
    "q_scfa": 5.0,
}


# =========================================================
# 5. 标签解释词典：规则兜底用
# =========================================================

TAG_EXPLAIN_MAP = {
    # 黑下巴相关
    "脂肪负担偏高": "油脂压力偏大，可能更容易刺激皮脂分泌，对容易黑下巴的猫不够友好。",
    "动物脂肪负担偏高": "动物脂肪占比较重，部分猫可能更容易出现油下巴或皮脂压力。",
    "Omega脂肪酸比例偏失衡": "脂肪结构不够平衡，帮助皮肤稳定的脂肪支持可能不足。",
    "Omega-6压力偏高": "促炎倾向的脂肪压力偏高，可能加重皮肤炎症相关风险。",
    "Omega-3支持不足": "帮助皮肤舒缓和稳定的脂肪支持不足。",
    "整体皮脂调节缓冲不足": "配方里帮助皮肤维持稳定状态的保护因子偏弱。",
    "脂肪调节支持不足": "脂肪压力存在，但调节脂肪代谢和皮脂稳定的支持不够。",
    "抗氧化支持偏弱": "抗炎、抗氧化保护不足，面对脂肪或炎症压力时缓冲力较弱。",
    "益生元支持偏弱": "肠道菌群支持不足，可能间接影响皮肤和炎症状态。",

    # 软便相关
    "碳水结构压力偏高": "淀粉、豆类或薯类负担偏重，肠胃敏感猫可能更容易便软。",
    "蛋白消化压力偏高": "蛋白结构偏复杂，部分猫可能更难适应，换粮时需要更谨慎。",
    "脂肪消化负担偏高": "油脂偏重，肠胃较弱的猫可能更容易出现软便或消化压力。",
    "供菌底物相对过量": "可发酵底物偏多，可能出现喂菌过头，导致产气、肠鸣或便软。",
    "便便成形支持不足": "缺少帮助便便稳定成形的纤维结构。",
    "菌群代谢支持不足": "虽然可能有供菌成分，但真正帮助肠道稳定代谢的支持不够。",
    "肠道缓冲支持不足": "肠道受到脂肪、蛋白、淀粉等刺激时，缺少足够缓冲保护。",
}


# =========================================================
# 6. 基础工具函数
# =========================================================

def normalize_score(x: Any, field: Optional[str] = None) -> Optional[float]:
    """
    将 score 标准化到 0-100。
    如果原始值是 0-1，则乘以 100。
    如果已经是 0-100，则直接返回。
    """
    if x is None or pd.isna(x):
        return None

    try:
        val = float(x)
    except Exception:
        return None

    scale_max = SCORE_SCALE_MAX.get(field or "")
    if scale_max:
        return round(max(0.0, min(100.0, val / scale_max * 100)), 2)

    if 0 <= val <= 1:
        return round(val * 100, 2)

    return round(val, 2)


def score_level(score: Optional[float], score_type: str) -> str:
    """
    把分数转成用户能懂的等级。
    """
    if score is None or pd.isna(score):
        return "暂无数据"

    if score_type == "pressure":
        if score >= 75:
            return "偏高"
        elif score >= 55:
            return "中等偏高"
        elif score >= 35:
            return "中等"
        else:
            return "较低"

    if score_type == "protective":
        if score >= 75:
            return "较强"
        elif score >= 55:
            return "中等偏强"
        elif score >= 35:
            return "中等"
        else:
            return "偏弱"

    if score_type == "mixed":
        if score >= 75:
            return "偏高，需结合搭配判断"
        elif score >= 55:
            return "中等偏高"
        elif score >= 35:
            return "中等"
        else:
            return "偏低"

    return "暂无数据"


def parse_tags(value: Any) -> List[str]:
    """
    支持解析：
    1. JSON 数组：["脂肪负担偏高", "Omega-3支持不足"]
    2. 逗号分隔：脂肪负担偏高,Omega-3支持不足
    3. 中文顿号分隔：脂肪负担偏高、Omega-3支持不足
    4. 单个标签
    """
    if value is None or pd.isna(value):
        return []

    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]

    s = str(value).strip()
    if not s:
        return []

    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
        if isinstance(parsed, dict):
            return [str(x).strip() for x in parsed.values() if str(x).strip()]
    except Exception:
        pass

    parts = re.split(r"[,\，、;；|/]+", s)
    return [p.strip() for p in parts if p.strip()]


def merge_tag_cols(row: Optional[pd.Series], tag_cols: List[str]) -> List[str]:
    if row is None or row.empty:
        return []

    tags = []
    for col in tag_cols:
        if col in row.index:
            tags.extend(parse_tags(row[col]))

    seen = set()
    result = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            result.append(tag)

    return result


def explain_tags(tags: List[str]) -> List[Tuple[str, str]]:
    result = []
    for tag in tags:
        explanation = TAG_EXPLAIN_MAP.get(
            tag,
            "该标签表示该产品在这一维度上存在相对突出的配方特征，需要结合原料和猫咪体质进一步判断。"
        )
        result.append((tag, explanation))
    return result


def safe_get(row: Optional[pd.Series], col: Optional[str], default: Any = None) -> Any:
    if row is None or col is None:
        return default
    if col not in row.index:
        return default
    val = row[col]
    if pd.isna(val):
        return default
    return val


def make_json_safe(obj: Any) -> Any:
    """
    将 pandas/numpy 类型转成可 JSON 序列化类型。
    """
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(make_json_safe(v) for v in obj)
    if pd.isna(obj) if not isinstance(obj, (dict, list, tuple, str)) else False:
        return None
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return obj


def calc_input_hash(context: dict) -> str:
    raw = json.dumps(make_json_safe(context), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()



def _to_float_or_none(value: Any) -> Optional[float]:
    """将数据库里的 rank/percentile 等值安全转成 float。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return None


def build_risk_relative_position(row: Optional[pd.Series], pool_summary: dict) -> str:
    """
    将风险排序转成用户可读的相对位置。

    约定：batch_rank 越小，风险越靠前；第 1 名代表当前池子中风险最高。
    因此不要把 rank/percentile 直接说成“前 xx%”，否则容易让用户误解。
    """
    if row is None:
        return "相对位置：暂无风险排序数据"

    rank = _to_float_or_none(safe_get(row, TABLE_CONFIG["rank_col"], None))
    total = _to_float_or_none((pool_summary or {}).get("total_products"))

    if rank is not None and total is not None and total > 0:
        ratio = rank / total
        rank_text = f"排名第{int(rank)}/{int(total)}，数字越靠前代表风险越高"

        if ratio <= 0.25:
            return f"相对位置：风险靠前（{rank_text}）"
        if ratio <= 0.45:
            return f"相对位置：高于不少产品（{rank_text}）"
        if ratio <= 0.60:
            return f"相对位置：中间区间（{rank_text}）"
        return f"相对位置：低于多数产品（{rank_text}）"

    percentile = _to_float_or_none(safe_get(row, TABLE_CONFIG["percentile_col"], None))
    if percentile is not None:
        # 这里不解释成“前xx%”，只作为内部排序参考。
        return f"相对位置：参考分位 {percentile:.3f}，请结合风险等级理解"

    return "相对位置：暂无可解释的排序位置"


# =========================================================
# 7. 数据查询
# =========================================================

def quote_identifier(name: str) -> str:
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        raise ValueError(f"非法字段或表名：{name}")
    return f"`{name}`"


def get_table_columns(engine, table_name: str) -> set:
    sql = text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :schema_name
          AND TABLE_NAME = :table_name
    """)
    df = pd.read_sql(
        sql,
        engine,
        params={"schema_name": DB_CONFIG["database"], "table_name": table_name},
    )
    return set(df["COLUMN_NAME"].tolist())


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def resolve_brand_col(columns: set, preferred_col: str) -> Optional[str]:
    if preferred_col in columns:
        return preferred_col
    if "brand" in columns:
        return "brand"
    return None


def build_product_search_sql(product_col: str, brand_col: Optional[str] = None) -> Tuple[str, str]:
    product_sql = quote_identifier(product_col)
    if not brand_col:
        return f"{product_sql} LIKE :product_name", f"{product_sql} = :product_query"

    brand_sql = quote_identifier(brand_col)
    brand_product_sql = f"CONCAT(COALESCE({brand_sql}, ''), COALESCE({product_sql}, ''))"
    brand_space_product_sql = f"CONCAT(COALESCE({brand_sql}, ''), ' ', COALESCE({product_sql}, ''))"
    where_sql = (
        f"({product_sql} LIKE :product_name "
        f"OR {brand_product_sql} LIKE :product_name_compact "
        f"OR {brand_space_product_sql} LIKE :product_name)"
    )
    exact_sql = (
        f"({brand_product_sql} = :product_name_compact_exact "
        f"OR {brand_space_product_sql} = :product_query "
        f"OR {product_sql} = :product_query)"
    )
    return where_sql, exact_sql


def product_identity_mask(df: pd.DataFrame, product_col: str, query: str, brand_col: Optional[str] = None) -> pd.Series:
    product_text = df[product_col].astype(str)
    raw_query = str(query or "").strip()
    mask = product_text.str.contains(raw_query, na=False, regex=False)
    if brand_col and brand_col in df.columns:
        brand_text = df[brand_col].fillna("").astype(str)
        compact_query = compact_text(raw_query)
        mask = (
            mask
            | (brand_text + product_text).str.contains(compact_query, na=False, regex=False)
            | (brand_text + " " + product_text).str.contains(raw_query, na=False, regex=False)
        )
    return mask


def query_one_product(
    engine,
    table_name: str,
    product_name: str,
    *,
    product_col: str,
    brand_col: Optional[str] = None,
    extra_where: str = "",
    params: Optional[dict] = None,
) -> Optional[pd.Series]:
    """
    根据产品名模糊查询。
    如果命中多条，默认取第一条。
    """
    columns = get_table_columns(engine, table_name)
    if product_col not in columns:
        return None
    resolved_brand_col = resolve_brand_col(columns, brand_col or TABLE_CONFIG["brand_name_col"])

    order_parts = []
    search_sql, exact_match_sql = build_product_search_sql(product_col, resolved_brand_col)
    order_parts.append(f"CASE WHEN {exact_match_sql} THEN 0 ELSE 1 END")
    if "calculated_at" in columns:
        order_parts.append("calculated_at DESC")
    elif "created_at" in columns:
        order_parts.append("created_at DESC")
    order_sql = "ORDER BY " + ", ".join(order_parts)

    where_sql = f"WHERE {search_sql}"
    if extra_where:
        where_sql += f" AND {extra_where}"
    product_query = str(product_name or "").strip()
    query_params = {
        "product_query": product_query,
        "product_name": f"%{product_query}%",
        "product_name_compact": f"%{compact_text(product_query)}%",
        "product_name_compact_exact": compact_text(product_query),
    }
    if params:
        query_params.update(params)

    sql = text(f"""
        SELECT *
        FROM {quote_identifier(table_name)}
        {where_sql}
        {order_sql}
        LIMIT 10
    """)

    df = pd.read_sql(sql, engine, params=query_params)

    if df.empty:
        return None

    return df.iloc[0]


def get_pool_summary(engine, product_name: str) -> dict:
    """
    计算当前产品在全池子中的相对位置。
    """
    score_table = TABLE_CONFIG["score_table"]
    product_col = TABLE_CONFIG["product_name_col"]
    brand_col = TABLE_CONFIG["brand_name_col"]
    score_cols = TABLE_CONFIG["score_cols"]
    table_cols = get_table_columns(engine, score_table)
    if product_col not in table_cols:
        return {}
    resolved_brand_col = resolve_brand_col(table_cols, brand_col)
    selected_score_cols = [col for col in score_cols if col in table_cols]

    existing_cols = [product_col] + ([resolved_brand_col] if resolved_brand_col else []) + selected_score_cols
    sql = text(f"""
        SELECT {", ".join(quote_identifier(col) for col in existing_cols)}
        FROM {quote_identifier(score_table)}
    """)

    try:
        df = pd.read_sql(sql, engine)
    except Exception:
        return {}

    if df.empty:
        return {}

    for col in selected_score_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda value, field=col: normalize_score(value, field))

    product_df = df[product_identity_mask(df, product_col, product_name, resolved_brand_col)]

    if product_df.empty:
        return {
            "total_products": len(df),
            "message": "未找到当前产品的池子位置",
        }

    product_row = product_df.iloc[0]

    score_summary = {}

    for col in selected_score_cols:
        if col not in df.columns:
            continue

        valid = df[col].dropna()
        if valid.empty:
            continue

        value = product_row[col]
        if value is None or pd.isna(value):
            continue

        avg = valid.mean()
        median = valid.median()
        percentile = (valid <= value).mean()

        score_summary[col] = {
            "display_name": SCORE_DISPLAY_MAP.get(col, {}).get("name", col),
            "product_value": round(float(value), 2),
            "pool_avg": round(float(avg), 2),
            "pool_median": round(float(median), 2),
            "percentile": round(float(percentile), 3),
        }

    return {
        "total_products": len(df),
        "score_summary": score_summary,
    }


def get_product_data(product_name: str) -> Dict[str, Any]:
    engine = get_engine()

    score_row = query_one_product(
        engine,
        TABLE_CONFIG["score_table"],
        product_name,
        product_col=TABLE_CONFIG["product_name_col"],
        brand_col=TABLE_CONFIG["brand_name_col"],
    )

    black_chin_row = query_one_product(
        engine,
        TABLE_CONFIG["risk_table"],
        product_name,
        product_col=TABLE_CONFIG["risk_product_name_col"],
        brand_col=TABLE_CONFIG["brand_name_col"],
        extra_where="score_model_version LIKE :score_model_like",
        params={"score_model_like": TABLE_CONFIG["black_chin_score_model_like"]},
    )

    soft_stool_row = query_one_product(
        engine,
        TABLE_CONFIG["risk_table"],
        product_name,
        product_col=TABLE_CONFIG["risk_product_name_col"],
        brand_col=TABLE_CONFIG["brand_name_col"],
        extra_where="score_model_version LIKE :score_model_like",
        params={"score_model_like": TABLE_CONFIG["soft_stool_score_model_like"]},
    )

    pool_summary = get_pool_summary(engine, product_name)

    return {
        "score": score_row,
        "black_chin": black_chin_row,
        "soft_stool": soft_stool_row,
        "pool_summary": pool_summary,
    }


# =========================================================
# 8. 分析逻辑
# =========================================================

def build_score_profile(score_row: Optional[pd.Series]) -> pd.DataFrame:
    rows = []

    if score_row is None:
        return pd.DataFrame()

    for col in TABLE_CONFIG["score_cols"]:
        if col not in score_row.index:
            continue

        meta = SCORE_DISPLAY_MAP.get(col, {})
        raw_score = score_row[col]
        score = normalize_score(raw_score, col)
        score_type = meta.get("type", "mixed")

        rows.append({
            "field": col,
            "display_name": meta.get("name", col),
            "score": score,
            "level": score_level(score, score_type),
            "type": score_type,
            "explain": meta.get("explain", ""),
        })

    return pd.DataFrame(rows)


def _first_score_from_profile(score_df: pd.DataFrame, *fields: str) -> Optional[float]:
    """从 score_df 中按优先级取第一个可用分数。"""
    for field in fields:
        sub = score_df[score_df["field"] == field]
        if not sub.empty:
            score = sub.iloc[0]["score"]
            if score is not None and not pd.isna(score):
                return score
    return None


def _score_evidence_item(score_df: pd.DataFrame, field: str, meaning: Optional[str] = None) -> dict:
    """生成某个底层 score 的证据项。"""
    meta = SCORE_DISPLAY_MAP.get(field, {})
    sub = score_df[score_df["field"] == field]

    if sub.empty:
        return {
            "field": field,
            "name": meta.get("name", field),
            "score": None,
            "level": "暂无数据",
            "type": meta.get("type", "mixed"),
            "meaning": meaning or meta.get("explain", "暂无解释。"),
        }

    row = sub.iloc[0]
    return {
        "field": field,
        "name": row.get("display_name", meta.get("name", field)),
        "score": row.get("score"),
        "level": row.get("level"),
        "type": row.get("type", meta.get("type", "mixed")),
        "meaning": meaning or row.get("explain", meta.get("explain", "暂无解释。")),
    }


def _weighted_avg_valid(weighted_values: List[Tuple[Optional[float], float]]) -> Optional[float]:
    """对可用分数做加权平均，自动忽略缺失值并重归一化权重。"""
    valid = [
        (float(value), float(weight))
        for value, weight in weighted_values
        if value is not None and not pd.isna(value) and weight > 0
    ]
    if not valid:
        return None

    total_weight = sum(weight for _, weight in valid)
    return round(sum(value * weight for value, weight in valid) / total_weight, 2)


def build_merged_profile(score_df: pd.DataFrame) -> pd.DataFrame:
    """
    将底层 score 融合成前台 6 个画像维度。

    页面只把 6 个画像维度作为一级信息展示；
    每个画像维度下面保留 underlying_scores，作为可展开的底层证据。
    """
    protein_pressure = _first_score_from_profile(score_df, "protein_score", "protein_structure_score")
    protein_quality = _first_score_from_profile(score_df, "protein_quality_score")

    carb_pressure = _first_score_from_profile(score_df, "carb_score", "starch_burden_score")
    starch_burden = _first_score_from_profile(score_df, "starch_burden_score")

    fat_pressure = _first_score_from_profile(score_df, "fat_score")
    fat_regulation = _first_score_from_profile(score_df, "fat_regulation_score")

    fiber_score = _first_score_from_profile(score_df, "fiber_score", "p_total_score")
    p_total = _first_score_from_profile(score_df, "p_total_score")
    p_buffer = _first_score_from_profile(score_df, "p_buffer")

    prebiotic = _first_score_from_profile(score_df, "prebiotic_score")
    q_feed = _first_score_from_profile(score_df, "q_feed")
    q_scfa = _first_score_from_profile(score_df, "q_scfa")

    antioxidant = _first_score_from_profile(score_df, "antioxidant_score", "fat_regulation_score")

    # 纤维缓冲更重视 p_buffer，因为它更贴近“肠道遇到刺激时是否稳”。
    fiber_buffer = _weighted_avg_valid([
        (fiber_score, 0.35),
        (p_total, 0.20),
        (p_buffer, 0.45),
    ])

    # 菌群支持不简单等同于 q_feed 高。
    # q_scfa 更接近“稳定代谢支持”，prebiotic 是整体益生元结构，q_feed 只给较低权重。
    microbiome_support = _weighted_avg_valid([
        (prebiotic, 0.35),
        (q_scfa, 0.45),
        (q_feed, 0.20),
    ])

    rows = [
        {
            "dimension": "蛋白压力",
            "score": protein_pressure,
            "type": "pressure",
            "summary": "看蛋白来源和蛋白结构是否偏复杂，以及是否可能增加消化或换粮适应压力。",
            "underlying_scores": [
                _score_evidence_item(score_df, "protein_score", "代表整体蛋白消化压力。"),
                _score_evidence_item(score_df, "protein_structure_score", "代表蛋白结构复杂度和结构负载。"),
                _score_evidence_item(score_df, "protein_quality_score", "这是正向质量支持，不等同于蛋白压力低。"),
            ],
        },
        {
            "dimension": "碳水负担",
            "score": carb_pressure,
            "type": "pressure",
            "summary": "看淀粉、豆类、薯类等碳水结构是否偏重，以及是否可能增加软便或发酵压力。",
            "underlying_scores": [
                _score_evidence_item(score_df, "carb_score", "代表整体碳水/淀粉结构压力。"),
                _score_evidence_item(score_df, "starch_burden_score", "代表淀粉、豆薯类等原料带来的淀粉负担。"),
            ],
        },
        {
            "dimension": "脂肪负担",
            "score": fat_pressure,
            "type": "pressure",
            "summary": "看油脂压力是否偏高，以及脂肪调节支持是否能跟上。",
            "underlying_scores": [
                _score_evidence_item(score_df, "fat_score", "代表脂肪压力，分数越高越需要关注黑下巴、油脂旺盛或脂肪消化压力。"),
                _score_evidence_item(score_df, "fat_regulation_score", "这是脂肪调节和皮肤稳定支持，属于保护证据。"),
            ],
        },
        {
            "dimension": "纤维缓冲",
            "score": fiber_buffer,
            "type": "protective",
            "summary": "看纤维是否真正能帮助便便成形、增加肠道缓冲，而不是只看纤维总量。",
            "underlying_scores": [
                _score_evidence_item(score_df, "fiber_score", "代表整体纤维支持。"),
                _score_evidence_item(score_df, "p_total_score", "代表便便成形、粪便骨架和缓冲支持的综合能力。"),
                _score_evidence_item(score_df, "p_buffer", "代表肠道遇到刺激时的缓冲垫能力。"),
            ],
        },
        {
            "dimension": "菌群支持",
            "score": microbiome_support,
            "type": "mixed",
            "summary": "看益生元供菌、供菌底物和菌群代谢支持是否平衡；不是供菌越多就一定越好。",
            "underlying_scores": [
                _score_evidence_item(score_df, "prebiotic_score", "代表益生元和供菌结构的整体支持。"),
                _score_evidence_item(score_df, "q_feed", "代表供菌底物多少，偏高时要结合发酵稳定性判断。"),
                _score_evidence_item(score_df, "q_scfa", "代表菌群代谢支持，越高通常越有利于肠道稳定。"),
            ],
        },
        {
            "dimension": "抗氧化保护",
            "score": antioxidant,
            "type": "protective",
            "summary": "看抗炎、抗氧化和皮肤稳定相关的保护支持是否足够。",
            "underlying_scores": [
                _score_evidence_item(score_df, "antioxidant_score", "代表抗氧化保护能力。"),
                _score_evidence_item(score_df, "fat_regulation_score", "脂肪调节支持也可作为皮肤稳定和氧化压力缓冲的辅助证据。"),
            ],
        },
    ]

    return pd.DataFrame(rows)


def build_merged_profile_baseline(pool_summary: dict) -> Optional[pd.DataFrame]:
    """根据池子中位数构建 6 个画像维度的基线，用于雷达图对照。"""
    score_summary = (pool_summary or {}).get("score_summary") or {}
    if not score_summary:
        return None

    def get_median(*fields: str) -> Optional[float]:
        for field in fields:
            item = score_summary.get(field)
            if item and item.get("pool_median") is not None:
                return item.get("pool_median")
        return None

    protein_pressure = get_median("protein_score", "protein_structure_score")
    carb_pressure = get_median("carb_score", "starch_burden_score")
    fat_pressure = get_median("fat_score")

    fiber_score = get_median("fiber_score", "p_total_score")
    p_total = get_median("p_total_score")
    p_buffer = get_median("p_buffer")
    fiber_buffer = _weighted_avg_valid([
        (fiber_score, 0.35),
        (p_total, 0.20),
        (p_buffer, 0.45),
    ])

    prebiotic = get_median("prebiotic_score")
    q_feed = get_median("q_feed")
    q_scfa = get_median("q_scfa")
    microbiome_support = _weighted_avg_valid([
        (prebiotic, 0.35),
        (q_scfa, 0.45),
        (q_feed, 0.20),
    ])

    antioxidant = get_median("antioxidant_score", "fat_regulation_score")

    return pd.DataFrame([
        {"dimension": "蛋白压力", "score": protein_pressure},
        {"dimension": "碳水负担", "score": carb_pressure},
        {"dimension": "脂肪负担", "score": fat_pressure},
        {"dimension": "纤维缓冲", "score": fiber_buffer},
        {"dimension": "菌群支持", "score": microbiome_support},
        {"dimension": "抗氧化保护", "score": antioxidant},
    ])


# 兼容旧函数名，避免 pipeline.py 里仍引用旧名称时报错。
def build_six_dimension_profile(score_df: pd.DataFrame) -> pd.DataFrame:
    return build_merged_profile(score_df)


def build_six_dimension_baseline(pool_summary: dict) -> Optional[pd.DataFrame]:
    return build_merged_profile_baseline(pool_summary)


def generate_rule_summary(score_df: pd.DataFrame) -> str:
    """
    规则版一句话摘要，作为页面顶部快速结论。
    AI 解读会更自然。
    """
    def get_score(*fields: str) -> Optional[float]:
        for field in fields:
            sub = score_df[score_df["field"] == field]
            if not sub.empty:
                return sub.iloc[0]["score"]
        return None

    fat = get_score("fat_score")
    carb = get_score("carb_score", "starch_burden_score")
    protein = get_score("protein_score", "protein_structure_score")
    protein_quality = get_score("protein_quality_score")
    antioxidant = get_score("antioxidant_score", "fat_regulation_score")
    p_buffer = get_score("p_buffer")
    q_feed = get_score("q_feed")
    q_scfa = get_score("q_scfa")

    points = []

    if fat is not None and fat >= 70:
        points.append("脂肪压力偏高")
    elif fat is not None and fat <= 35:
        points.append("脂肪压力较低")

    if carb is not None and carb >= 70:
        points.append("碳水/淀粉负担偏高")

    if protein is not None and protein >= 70:
        points.append("蛋白结构偏复杂")
    if protein_quality is not None and protein_quality >= 75:
        points.append("蛋白质量较好")

    if p_buffer is not None and p_buffer <= 35:
        points.append("肠道缓冲偏弱")
    elif p_buffer is not None and p_buffer >= 70:
        points.append("肠道缓冲较好")

    if antioxidant is not None and antioxidant <= 35:
        points.append("抗氧化保护偏弱")
    elif antioxidant is not None and antioxidant >= 70:
        points.append("抗氧化保护较强")

    if q_feed is not None and q_feed >= 75 and (q_scfa is None or q_scfa < 55):
        points.append("供菌底物偏多但代谢支持一般")

    if not points:
        return "这款粮整体没有特别突出的单一压力点，建议结合猫咪体质和历史换粮反应判断。"

    return "这款粮的主要特点是：" + "、".join(points[:4]) + "。"


def generate_suitable_advice(
    score_df: pd.DataFrame,
    black_chin_tags: List[str],
    soft_stool_tags: List[str],
) -> Tuple[List[str], List[str]]:
    def get_score(*fields: str) -> Optional[float]:
        for field in fields:
            sub = score_df[score_df["field"] == field]
            if not sub.empty:
                return sub.iloc[0]["score"]
        return None

    fat = get_score("fat_score")
    carb = get_score("carb_score", "starch_burden_score")
    protein = get_score("protein_score", "protein_structure_score")
    protein_quality = get_score("protein_quality_score")
    antioxidant = get_score("antioxidant_score", "fat_regulation_score")
    p_buffer = get_score("p_buffer")
    q_feed = get_score("q_feed")
    q_scfa = get_score("q_scfa")

    suitable = []
    unsuitable = []

    if fat is not None and fat <= 45:
        suitable.append("皮脂分泌比较稳定、担心油脂压力的猫")
    if p_buffer is not None and p_buffer >= 60:
        suitable.append("肠胃需要稳定过渡、容易轻微软便的猫")
    if antioxidant is not None and antioxidant >= 60:
        suitable.append("需要一定皮肤和抗氧化保护支持的猫")
    if protein is not None and protein <= 45:
        suitable.append("对复杂蛋白不太耐受、适合简单蛋白结构的猫")
    if protein_quality is not None and protein_quality >= 70:
        suitable.append("减重期需要优质蛋白支持保肌肉的猫")

    if fat is not None and fat >= 70:
        unsuitable.append("黑下巴反复、皮脂分泌旺盛的猫")
    if carb is not None and carb >= 70:
        unsuitable.append("肠胃敏感、对淀粉或豆薯类原料反应明显的猫")
    if protein is not None and protein >= 70:
        unsuitable.append("换粮敏感、疑似蛋白不耐受的猫")
    if protein_quality is not None and protein_quality <= 45:
        unsuitable.append("需要更高质量动物蛋白支持的猫")
    if p_buffer is not None and p_buffer <= 35:
        unsuitable.append("软便频繁、肠道稳定性较弱的猫")
    if q_feed is not None and q_feed >= 75 and (q_scfa is None or q_scfa < 55):
        unsuitable.append("容易胀气、肠鸣、发酵型软便的猫")

    if "脂肪负担偏高" in black_chin_tags or "动物脂肪负担偏高" in black_chin_tags:
        unsuitable.append("对高脂粮反应明显、容易油下巴的猫")
    if "肠道缓冲支持不足" in soft_stool_tags:
        unsuitable.append("肠道刺激后容易拉稀或便软的猫")

    if not suitable:
        suitable.append("肠胃和皮肤状态较稳定、没有明显特殊敏感史的猫")

    if not unsuitable:
        unsuitable.append("没有明显禁忌，但仍建议换粮期观察便便、下巴和食欲变化")

    suitable = list(dict.fromkeys(suitable))
    unsuitable = list(dict.fromkeys(unsuitable))

    return suitable, unsuitable


# =========================================================
# 9. 通义千问 AI 解读
# =========================================================

def build_llm_context(
    product_display_name: str,
    brand_name: str,
    merged_df: pd.DataFrame,
    black_chin_row: Optional[pd.Series],
    soft_stool_row: Optional[pd.Series],
    black_chin_tags: List[str],
    soft_stool_tags: List[str],
    pool_summary: dict,
    suitable: List[str],
    unsuitable: List[str],
) -> dict:
    """
    构建传给通义千问的结构化上下文。
    这里使用 merged_profile：一级是用户画像维度，二级是底层 score 证据，避免 score 和六维画像重复表达。
    """
    merged_profile = []
    for _, row in merged_df.iterrows():
        score = row["score"]
        merged_profile.append({
            "dimension": row["dimension"],
            "score": None if score is None or pd.isna(score) else score,
            "level": score_level(score, row["type"]),
            "type": row["type"],
            "summary": row["summary"],
            "underlying_scores": row["underlying_scores"],
        })

    context = {
        "product_name": product_display_name,
        "brand_name": brand_name,
        "merged_profile": merged_profile,
        "black_chin_risk": {
            "risk_level": safe_get(black_chin_row, TABLE_CONFIG["risk_level_col"], "暂无"),
            "rank": safe_get(black_chin_row, TABLE_CONFIG["rank_col"], "暂无"),
            "percentile": safe_get(black_chin_row, TABLE_CONFIG["percentile_col"], "暂无"),
            "relative_position": build_risk_relative_position(black_chin_row, pool_summary),
            "rank_note": "排序数字越小，代表风险越靠前；不要把该信息表述为‘前xx%’。",
            "tags": black_chin_tags,
        },
        "soft_stool_risk": {
            "risk_level": safe_get(soft_stool_row, TABLE_CONFIG["risk_level_col"], "暂无"),
            "rank": safe_get(soft_stool_row, TABLE_CONFIG["rank_col"], "暂无"),
            "percentile": safe_get(soft_stool_row, TABLE_CONFIG["percentile_col"], "暂无"),
            "relative_position": build_risk_relative_position(soft_stool_row, pool_summary),
            "rank_note": "排序数字越小，代表风险越靠前；不要把该信息表述为‘前xx%’。",
            "tags": soft_stool_tags,
        },
        "rule_based_advice": {
            "suitable": suitable,
            "need_caution": unsuitable,
        },
        "pool_summary": pool_summary or {},
        "important_instruction": [
            "所有风险、排序、标签均来自规则模型或数据库，大模型不得重新打分。",
            "大模型只能做解释、总结和用户语言翻译。",
            "不得编造未提供的原料、品牌背景、兽医诊断或临床结论。",
            "merged_profile 中的 dimension 是给用户看的画像维度，underlying_scores 是该维度的底层证据。",
            "如果风险标签里出现“碳水结构压力偏高”或“碳水结构偏高”，不得总结为“碳水极低”或“低碳水”。",
            "如果风险标签里出现“脂肪调节支持不足”，不得总结为“脂肪调节支持强”。",
            "protein_quality_score 高只能说明蛋白质量支持好，不能推导为整体消化负担轻；还必须同时看蛋白压力、碳水和脂肪标签。",
            "风险位置必须优先使用 relative_position 字段，例如‘相对位置：低于多数产品’，不得写成‘前xx%’。",
            "‘适合与需要谨慎’必须优先基于 rule_based_advice 改写；不得新增没有出现在规则建议、风险标签或 merged_profile 解释中的猫咪人群或症状。",
        ],
    }

    return make_json_safe(context)


def generate_qwen_interpretation(context: dict) -> str:
    client = get_qwen_client()

    system_prompt = """
你是“宠析”的宠物食品配方分析助手，任务是把猫粮配方模型结果翻译成普通养猫用户能听懂的解释。

你必须遵守：
1. 只能基于用户提供的结构化数据解释，不要编造成分、品牌背景、疾病诊断或临床结论。
2. 不要重新计算分数，不要改变风险等级、排序、标签。
3. 不要使用绝对化表达，例如“必然导致”“一定会软便”“一定会黑下巴”。
4. 可以使用“可能”“更需要观察”“对某类猫不够友好”“相对更稳/更需要谨慎”等克制表达。
5. 面向 C 端养猫用户，语言自然、清楚、有温度，但不要营销化。
6. 输出要适合放在产品页面中展示，不要写成论文。
7. 需要结合“当前产品在池子中的相对位置”做综合总结；如果池子数据缺失，就说明“当前池子对比信息不足”。
8. 不要输出 Markdown 表格。
9. 风险标签优先级高于自由总结。若标签提示“碳水结构压力偏高/碳水结构偏高”，必须明确说碳水是需要观察的压力点，不能写成“碳水极低”。
10. 若标签提示“脂肪调节支持不足”，必须明确说脂肪调节/皮肤稳定支持偏弱，不能写成“调节支持强”。
11. protein_quality_score 只能解释为“蛋白质量支持较好”；除非蛋白压力、碳水、脂肪都低，否则不要概括成“消化负担轻”。
12. 风险相对位置必须使用结构化数据中的 relative_position 原文或同义表达，例如“相对位置：低于多数产品”。不得写“前xx%”“后xx%”，避免用户误解。
13. “适合与需要谨慎”部分必须优先基于 rule_based_advice.suitable 和 rule_based_advice.need_caution 改写。可以解释原因，但不得新增没有出现在规则建议、风险标签或 merged_profile 解释中的猫咪人群/症状。
14. 禁止把“菌群代谢支持不足”自由扩写成“曾有菌群紊乱史”，除非输入数据明确出现“菌群紊乱史”。禁止把皮肤相关风险自由扩写成“皮肤偏干痒/慢性皮肤问题”，除非输入数据明确出现这些词。

输出结构固定为：

### 一句话结论
用 1-2 句话总结这款粮的整体画像。

### 配方画像解读
解释蛋白、碳水、脂肪、纤维缓冲、菌群支持、抗氧化保护中最突出的 2-4 个点。

### 黑下巴风险解读
结合黑下巴风险等级、排序、标签，解释主要风险来源。

### 软便风险解读
结合软便风险等级、排序、标签，解释主要风险来源。

### 放在当前产品池中看
结合 pool_summary 和 relative_position 说明它相对全池子的突出点，例如哪些分数高于多数产品、哪些保护项偏弱；风险位置用“相对位置：低于多数产品/中间区间/风险靠前”等表达。

### 适合与需要谨慎
基于 rule_based_advice 改写说明更适合什么猫、哪些猫需要谨慎观察；不要新增无依据症状。
"""

    user_prompt = f"""
下面是某款猫粮的结构化模型结果。请基于这些数据生成页面展示用的自然语言解释。

结构化数据：
{json.dumps(context, ensure_ascii=False, indent=2)}
"""

    completion = client.chat.completions.create(
        model=QWEN_CONFIG["model"],
        messages=[
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_prompt.strip()},
        ],
        temperature=QWEN_CONFIG["temperature"],
        max_tokens=QWEN_CONFIG["max_tokens"],
    )

    return completion.choices[0].message.content


# =========================================================
# 10. 可视化函数
# =========================================================

def radar_chart(six_df: pd.DataFrame, baseline_df: Optional[pd.DataFrame] = None):
    df = six_df.dropna(subset=["score"]).copy()

    if df.empty:
        st.info("暂无足够 score 数据生成雷达图。")
        return

    categories = df["dimension"].tolist()
    values = df["score"].tolist()

    categories_closed = categories + [categories[0]]
    values_closed = values + [values[0]]

    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=values_closed,
        theta=categories_closed,
        fill="toself",
        name="产品画像",
    ))

    if baseline_df is not None and not baseline_df.empty:
        baseline_map = dict(zip(baseline_df["dimension"], baseline_df["score"]))
        baseline_values = [
            baseline_map.get(category)
            for category in categories
        ]
        if any(value is not None and not pd.isna(value) for value in baseline_values):
            baseline_values = [
                None if value is None or pd.isna(value) else value
                for value in baseline_values
            ]
            fig.add_trace(go.Scatterpolar(
                r=baseline_values + [baseline_values[0]],
                theta=categories_closed,
                mode="lines",
                line=dict(color="#667085", width=2, dash="dash"),
                name="池子中位数",
            ))

    fig.update_layout(
        polar=dict(
            radialaxis=dict(
                visible=True,
                range=[0, 100],
            )
        ),
        showlegend=True,
        height=420,
        margin=dict(l=40, r=40, t=40, b=40),
    )

    st.plotly_chart(fig, use_container_width=True)


def score_bar_chart(score_df: pd.DataFrame):
    df = score_df.dropna(subset=["score"]).copy()

    if df.empty:
        st.info("暂无 score 明细数据。")
        return

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["score"],
        y=df["display_name"],
        orientation="h",
        text=df["level"],
        textposition="auto",
    ))

    fig.update_layout(
        xaxis=dict(range=[0, 100], title="分数"),
        yaxis=dict(title=""),
        height=420,
        margin=dict(l=20, r=20, t=30, b=30),
    )

    st.plotly_chart(fig, use_container_width=True)


def render_merged_profile(merged_df: pd.DataFrame, baseline_df: Optional[pd.DataFrame] = None):
    """渲染“配方画像 + 底层 score 证据”融合模块。"""
    left, right = st.columns([1.1, 1])

    with left:
        radar_chart(merged_df, baseline_df)

    with right:
        st.markdown("### 画像总览")
        for _, row in merged_df.iterrows():
            score = row["score"]
            if score is None or pd.isna(score):
                st.markdown(f"**{row['dimension']}：暂无数据**  \n{row['summary']}")
                continue

            level = score_level(score, row["type"])
            st.markdown(
                f"**{row['dimension']}：{level}（{score:.1f}）**  \n"
                f"{row['summary']}"
            )

    st.markdown("### 底层 score 证据")
    st.caption("一级展示给用户看的画像维度；展开后查看该维度背后的模型字段与解释。")

    for _, row in merged_df.iterrows():
        score = row["score"]
        if score is None or pd.isna(score):
            title = f"{row['dimension']}｜暂无数据"
        else:
            title = f"{row['dimension']}｜{score_level(score, row['type'])}（{score:.1f}）"

        with st.expander(title):
            st.markdown(row["summary"])
            evidence_rows = []
            for item in row["underlying_scores"]:
                evidence_rows.append({
                    "底层字段": item.get("field"),
                    "展示名": item.get("name"),
                    "分数": item.get("score"),
                    "等级": item.get("level"),
                    "含义": item.get("meaning"),
                })
            st.dataframe(pd.DataFrame(evidence_rows), use_container_width=True)


def render_pool_summary(pool_summary: dict):
    if not pool_summary:
        st.info("暂无产品池统计数据。")
        return

    total_products = pool_summary.get("total_products")
    if total_products:
        st.caption(f"当前参考池产品数：{total_products}")

    score_summary = pool_summary.get("score_summary", {})
    if not score_summary:
        st.info("暂无 score 池子对比信息。")
        return

    rows = []
    for field, item in score_summary.items():
        rows.append({
            "指标": item.get("display_name", field),
            "当前产品": item.get("product_value"),
            "池子均值": item.get("pool_avg"),
            "池子中位数": item.get("pool_median"),
            "分位数": item.get("percentile"),
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True)


# =========================================================
# 11. Streamlit 页面
# =========================================================

def render_page():
    st.set_page_config(
        page_title="宠析 - 单产品配方画像分析",
        layout="wide",
    )

    st.title("宠析｜单产品配方画像分析")

    with st.sidebar:
        st.markdown("## 配置检查")
        st.write(f"通义千问模型：`{QWEN_CONFIG['model']}`")

        if os.getenv("DASHSCOPE_API_KEY"):
            st.success("已检测到 DASHSCOPE_API_KEY")
        else:
            st.warning("未检测到 DASHSCOPE_API_KEY")

        st.markdown("## 查询说明")
        st.caption("输入品牌产品名称后，系统会优先按“品牌 + 产品名称”匹配数据库中的产品。")

    product_name = st.text_input(
        "请输入品牌产品名称",
        placeholder="例如：某某品牌 鸡肉全价猫粮",
    )

    if not product_name:
        st.info("请输入品牌产品名称后开始分析。")
        return

    try:
        data = get_product_data(product_name)
    except Exception as e:
        st.error(f"数据库查询失败：{e}")
        return

    score_row = data.get("score")
    black_chin_row = data.get("black_chin")
    soft_stool_row = data.get("soft_stool")
    pool_summary = data.get("pool_summary", {})

    if score_row is None and black_chin_row is None and soft_stool_row is None:
        st.warning("没有查询到该产品。请检查品牌产品名称，或确认数据库表中是否存在该产品。")
        return

    product_display_name = (
        safe_get(score_row, TABLE_CONFIG["product_name_col"])
        or safe_get(black_chin_row, TABLE_CONFIG["risk_product_name_col"])
        or safe_get(soft_stool_row, TABLE_CONFIG["risk_product_name_col"])
        or product_name
    )

    brand_name = (
        safe_get(score_row, TABLE_CONFIG["brand_name_col"])
        or safe_get(score_row, "brand")
        or safe_get(black_chin_row, TABLE_CONFIG["brand_name_col"])
        or safe_get(soft_stool_row, TABLE_CONFIG["brand_name_col"])
        or ""
    )

    score_df = build_score_profile(score_row)
    merged_df = build_merged_profile(score_df)
    merged_baseline_df = build_merged_profile_baseline(pool_summary)

    black_chin_tags = merge_tag_cols(black_chin_row, TABLE_CONFIG["tag_cols"])
    soft_stool_tags = merge_tag_cols(soft_stool_row, TABLE_CONFIG["tag_cols"])

    rule_summary = generate_rule_summary(score_df)
    suitable, unsuitable = generate_suitable_advice(
        score_df,
        black_chin_tags,
        soft_stool_tags,
    )

    black_risk_level = safe_get(black_chin_row, TABLE_CONFIG["risk_level_col"], "暂无")
    black_rank = safe_get(black_chin_row, TABLE_CONFIG["rank_col"], "暂无")
    black_percentile = safe_get(black_chin_row, TABLE_CONFIG["percentile_col"], "暂无")
    black_relative_position = build_risk_relative_position(black_chin_row, pool_summary)

    stool_risk_level = safe_get(soft_stool_row, TABLE_CONFIG["risk_level_col"], "暂无")
    stool_rank = safe_get(soft_stool_row, TABLE_CONFIG["rank_col"], "暂无")
    stool_percentile = safe_get(soft_stool_row, TABLE_CONFIG["percentile_col"], "暂无")
    stool_relative_position = build_risk_relative_position(soft_stool_row, pool_summary)

    llm_context = build_llm_context(
        product_display_name=str(product_display_name),
        brand_name=str(brand_name),
        merged_df=merged_df,
        black_chin_row=black_chin_row,
        soft_stool_row=soft_stool_row,
        black_chin_tags=black_chin_tags,
        soft_stool_tags=soft_stool_tags,
        pool_summary=pool_summary,
        suitable=suitable,
        unsuitable=unsuitable,
    )
    input_hash = calc_input_hash(llm_context)

    # =====================================================
    # 1. 产品摘要
    # =====================================================

    st.markdown("## 1. 产品摘要")

    if brand_name:
        st.caption(f"品牌：{brand_name}")

    st.subheader(str(product_display_name))
    st.markdown(f"**规则摘要：** {rule_summary}")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric("黑下巴风险", str(black_risk_level), f"排名：{black_rank}")

    with col2:
        st.metric("软便风险", str(stool_risk_level), f"排名：{stool_rank}")

    with col3:
        st.metric(
            "相对位置",
            "低位更友好",
            "见下方说明",
        )
        st.caption(f"黑下巴：{black_relative_position}")
        st.caption(f"软便：{stool_relative_position}")

    # =====================================================
    # 2. 配方画像与底层证据
    # =====================================================

    st.markdown("---")
    st.markdown("## 2. 配方画像与底层 score 证据")
    render_merged_profile(merged_df, merged_baseline_df)

    # =====================================================
    # 3. 风险原因解释
    # =====================================================

    st.markdown("---")
    st.markdown("## 3. 风险原因解释")

    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("### 黑下巴相关标签")
        if black_chin_tags:
            for tag, explanation in explain_tags(black_chin_tags):
                st.markdown(f"**{tag}**  \n{explanation}")
        else:
            st.info("暂无黑下巴标签数据。")

    with col_b:
        st.markdown("### 软便相关标签")
        if soft_stool_tags:
            for tag, explanation in explain_tags(soft_stool_tags):
                st.markdown(f"**{tag}**  \n{explanation}")
        else:
            st.info("暂无软便标签数据。")

    # =====================================================
    # 4. 适合 / 需要谨慎
    # =====================================================

    st.markdown("---")
    st.markdown("## 4. 适合 / 需要谨慎")

    col_s, col_u = st.columns(2)

    with col_s:
        st.markdown("### 相对适合")
        for item in suitable:
            st.markdown(f"- {item}")

    with col_u:
        st.markdown("### 需要谨慎")
        for item in unsuitable:
            st.markdown(f"- {item}")

    # =====================================================
    # 5. 通义千问 AI 综合解读
    # =====================================================

    st.markdown("---")
    st.markdown("## 5. 通义千问 AI 综合解读")
    st.caption(f"当前分析输入指纹：`{input_hash[:12]}`。同一产品数据不变时，AI 解读依据一致。")

    if st.button("生成通义千问 AI 综合解读", type="primary"):
        with st.spinner("正在调用通义千问生成解释..."):
            try:
                ai_text = generate_qwen_interpretation(llm_context)
                st.session_state[f"ai_text_{input_hash}"] = ai_text
            except Exception as e:
                st.error(f"通义千问解读生成失败：{e}")

    cached_text = st.session_state.get(f"ai_text_{input_hash}")
    if cached_text:
        st.markdown(cached_text)
    else:
        st.info("点击按钮后，通义千问会基于当前产品的配方画像、底层 score 证据、风险排序、标签和池子统计生成自然语言总结。")



if __name__ == "__main__":
    render_page()
