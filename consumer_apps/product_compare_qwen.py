# -*- coding: utf-8 -*-
"""
宠析 - 双产品配方画像对比页 + 通义千问 AI 对比总结

核心设计：
1. 不默认 A 是旧粮、B 是新粮；只做两个候选产品的中性对比。
2. 页面顺序：
   - 配方画像差异
   - 病症概率评估
   - 通义千问 AI 对比总结
3. 不同需求下的关注点不单独展示，由通义千问在对比总结里进行条件化解释
3. 风险结论不在页面顶部硬给，由通义千问基于标签、风险等级、画像差异做条件化推理。
4. 数据仍然使用两类表：
   - score 宽表：catfood_protein_fat_fiber_score_wide
   - 风险结果表：sku_risk_score_result，通过 score_model_version 区分 BLACK_CHIN / SOFT_STOOL

运行：
python3 feature_score_pipeline_project/pipeline.py product-compare
或：
streamlit run feature_score_pipeline_project/apps/product_compare_qwen.py
"""

import json
import os
import re
import hashlib
import html
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
# 2. 表名与字段配置
# =========================================================

TABLE_CONFIG = {
    # 配方画像 score 宽表
    "score_table": "catfood_protein_fat_fiber_score_wide",

    # 黑下巴/软便共用风险结果表
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
        "fat_score",
        "protein_structure_score",
        "starch_burden_score",
        "p_total_score",
        "fat_regulation_score",
        "p_buffer",
        "q_feed",
        "q_scfa",
    ],

    # 风险结果字段
    "risk_level_col": "current_pool_risk_level",
    "rank_col": "batch_rank",
    "percentile_col": "current_pool_percentile",

    # 标签字段
    "tag_cols": [
        "main_reason_tags",
        "support_reason_tags",
        "fat_detail_tags",
        "all_reason_tags",
    ],

    "batch_id_col": "batch_id",
}


# =========================================================
# 3. 通义千问配置
# =========================================================

QWEN_CONFIG = {
    "model": os.getenv("QWEN_MODEL", "qwen-plus"),
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "temperature": 0.35,
    "max_tokens": 1500,
}


def get_qwen_client() -> OpenAI:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("未检测到环境变量 DASHSCOPE_API_KEY。请先配置阿里云百炼 API Key。")

    return OpenAI(
        api_key=api_key,
        base_url=QWEN_CONFIG["base_url"],
    )


# =========================================================
# 4. score 展示名与解释
# =========================================================

SCORE_DISPLAY_MAP = {
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
    "starch_burden_score": {
        "name": "淀粉负担",
        "type": "pressure",
        "explain": "淀粉、豆类、薯类负担越重，肠胃敏感猫越可能不稳定。",
    },
    "p_total_score": {
        "name": "纤维总支持",
        "type": "mixed",
        "explain": "综合反映纤维结构、成形和缓冲支持。",
    },
    "p_buffer": {
        "name": "肠道缓冲力",
        "type": "protective",
        "explain": "肠道遇到刺激时，这个分数代表配方有没有足够的缓冲垫。",
    },
    "fat_score": {
        "name": "脂肪负担",
        "type": "pressure",
        "explain": "油脂压力越高，越可能关联黑下巴、油脂旺盛或脂肪消化压力。",
    },
    "fat_regulation_score": {
        "name": "脂肪调节支持",
        "type": "protective",
        "explain": "代表配方里帮助脂肪代谢、皮肤稳定和氧化压力缓冲的支持。",
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
    "fat_score": 1.0,
    "fat_regulation_score": 1.0,
    "starch_burden_score": 5.0,
    "p_total_score": 5.0,
    "p_buffer": 5.0,
    "q_feed": 5.0,
    "q_scfa": 5.0,
}


TAG_EXPLAIN_MAP = {
    # 黑下巴相关
    "皮脂代谢压力偏高": "脂肪和碳水代谢压力较集中，可能推高皮脂分泌相关风险。",
    "脂肪负担偏高": "油脂压力偏大，可能更容易刺激皮脂分泌，对容易黑下巴的猫不够友好。",
    "动物脂肪负担偏高": "动物脂肪占比较重，部分猫可能更容易出现油下巴或皮脂压力。",
    "碳水代谢压力偏高": "碳水结构偏高，可能间接影响代谢和皮脂稳定，需要结合皮肤状态观察。",
    "脂肪酸结构失衡": "脂肪酸结构不够平衡，帮助皮肤稳定的脂肪支持可能不足。",
    "Omega脂肪酸比例偏失衡": "脂肪结构不够平衡，帮助皮肤稳定的脂肪支持可能不足。",
    "Omega-6压力偏高": "促炎倾向的脂肪压力偏高，可能加重皮肤炎症相关风险。",
    "Omega-3支持不足": "帮助皮肤舒缓和稳定的脂肪支持不足。",
    "皮脂调节缓冲不足": "配方里帮助皮肤维持稳定状态的保护因子偏弱。",
    "整体皮脂调节缓冲不足": "配方里帮助皮肤维持稳定状态的保护因子偏弱。",
    "脂肪调节支持不足": "脂肪压力存在，但调节脂肪代谢和皮脂稳定的支持不够。",
    "抗氧化支持偏弱": "抗炎、抗氧化保护不足，面对脂肪或炎症压力时缓冲力较弱。",
    "益生元支持偏弱": "肠道菌群支持不足，可能间接影响皮肤和炎症状态。",
    "碳水结构偏高": "碳水结构偏高，可能间接影响代谢和皮脂稳定，需要结合皮肤状态观察。",

    # 软便相关
    "消化负担链": "淀粉、蛋白和脂肪带来的整体消化压力偏集中。",
    "淀粉/豆薯类碳水负担偏高": "淀粉、豆类或薯类负担偏重，肠胃敏感猫可能更容易便软。",
    "碳水结构压力偏高": "淀粉、豆类或薯类负担偏重，肠胃敏感猫可能更容易便软。",
    "碳水消化压力偏高": "淀粉、豆类或薯类负担偏重，肠胃敏感猫可能更容易便软。",
    "蛋白消化压力偏高": "蛋白结构偏复杂，部分猫可能更难适应，换粮时需要更谨慎。",
    "脂肪消化负担偏高": "油脂偏重，肠胃较弱的猫可能更容易出现软便或消化压力。",
    "成形支持链": "帮助便便稳定成形的纤维和结构支持不足。",
    "供菌底物相对过量": "可发酵底物偏多，可能出现喂菌过头，导致产气、肠鸣或便软。",
    "便便成形支持不足": "缺少帮助便便稳定成形的纤维结构。",
    "吸水成形能力偏弱": "吸水凝胶或锁水成形能力偏弱，便便更难稳定成形。",
    "粪便骨架支持偏弱": "不溶性纤维或粪便骨架支撑偏弱，便便结构稳定性不足。",
    "肠道缓冲链": "面对脂肪、蛋白、淀粉等刺激时，肠道缓冲保护不足。",
    "菌群代谢支持不足": "虽然可能有供菌成分，但真正帮助肠道稳定代谢的支持不够。",
    "菌群代谢链": "菌群代谢、短链脂肪酸和底物结构支持不足。",
    "SCFA支持偏弱": "短链脂肪酸相关支持偏弱，肠道稳定性可能不足。",
    "供菌底物结构不均衡": "可发酵底物结构不够均衡，可能出现喂菌过头或支持不足。",
    "肠道缓冲支持不足": "肠道受到脂肪、蛋白、淀粉等刺激时，缺少足够缓冲保护。",
    "肠道刺激缓冲不足": "肠道受到脂肪、蛋白、淀粉等刺激时，缺少足够缓冲保护。",
    "刺激缓冲能力偏弱": "稀释刺激物、降低肠道刺激感的缓冲能力偏弱。",
}


BLACK_CHIN_TAG_ALIASES = {
    "动物脂肪负担偏高": "脂肪负担偏高",
    "碳水结构偏高": "碳水代谢压力偏高",
    "Omega脂肪酸比例偏失衡": "脂肪酸结构失衡",
    "整体皮脂调节缓冲不足": "皮脂调节缓冲不足",
}


BLACK_CHIN_TAG_TREE = [
    {
        "label": "皮脂代谢压力偏高",
        "children": ["脂肪负担偏高", "碳水代谢压力偏高"],
    },
    {
        "label": "脂肪酸结构失衡",
        "children": ["Omega-6压力偏高", "Omega-3支持不足"],
    },
    {
        "label": "皮脂调节缓冲不足",
        "children": ["益生元支持偏弱", "脂肪调节支持不足"],
    },
]


SOFT_STOOL_TAG_ALIASES = {
    "碳水结构压力偏高": "淀粉/豆薯类碳水负担偏高",
    "碳水消化压力偏高": "淀粉/豆薯类碳水负担偏高",
    "肠道缓冲支持不足": "肠道刺激缓冲不足",
    "供菌底物相对过量": "供菌底物结构不均衡",
}


SOFT_STOOL_TAG_TREE = [
    {
        "label": "消化负担链",
        "children": ["淀粉/豆薯类碳水负担偏高", "蛋白消化压力偏高", "脂肪消化负担偏高"],
    },
    {
        "label": "成形支持链",
        "children": ["便便成形支持不足", "吸水成形能力偏弱", "粪便骨架支持偏弱"],
    },
    {
        "label": "肠道缓冲链",
        "children": ["肠道刺激缓冲不足", "刺激缓冲能力偏弱"],
    },
    {
        "label": "菌群代谢链",
        "children": ["菌群代谢支持不足", "SCFA支持偏弱", "供菌底物结构不均衡"],
    },
]


# =========================================================
# 5. 基础工具函数
# =========================================================

def normalize_score(x: Any, field: Optional[str] = None) -> Optional[float]:
    """将不同量纲的 score 统一到 0-100。"""
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

    return round(max(0.0, min(100.0, val)), 2)


def score_level(score: Optional[float], score_type: str) -> str:
    if score is None or pd.isna(score):
        return "暂无数据"

    if score_type == "pressure":
        if score >= 75:
            return "偏高"
        if score >= 55:
            return "中等偏高"
        if score >= 35:
            return "中等"
        return "较低"

    if score_type == "protective":
        if score >= 75:
            return "较强"
        if score >= 55:
            return "中等偏强"
        if score >= 35:
            return "中等"
        return "偏弱"

    if score_type == "mixed":
        if score >= 75:
            return "偏高，需结合搭配判断"
        if score >= 55:
            return "中等偏高"
        if score >= 35:
            return "中等"
        return "偏低"

    return "暂无数据"


def format_score_value(score: Any, digits: int = 1) -> str:
    if score is None or pd.isna(score):
        return "暂无"
    try:
        return f"{float(score):.{digits}f}"
    except Exception:
        return str(score)


def format_probability(value: Any) -> str:
    if value is None or pd.isna(value):
        return "暂无"
    try:
        prob = float(value)
    except Exception:
        return str(value)
    if prob <= 1:
        prob *= 100
    return f"{prob:.1f}%"


def parse_tags(value: Any) -> List[str]:
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
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(make_json_safe(v) for v in obj)
    if not isinstance(obj, (dict, list, tuple, str)):
        try:
            if pd.isna(obj):
                return None
        except Exception:
            pass
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return obj


def calc_input_hash(context: dict) -> str:
    raw = json.dumps(make_json_safe(context), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def quote_identifier(name: str) -> str:
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        raise ValueError(f"非法字段或表名：{name}")
    return f"`{name}`"


# =========================================================
# 6. 数据查询
# =========================================================

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


@st.cache_data(ttl=300)
def load_product_options() -> List[str]:
    engine = get_engine()
    table_name = TABLE_CONFIG["score_table"]
    columns = get_table_columns(engine, table_name)
    product_col = TABLE_CONFIG["product_name_col"]
    brand_col = resolve_brand_col(columns, TABLE_CONFIG["brand_name_col"])
    if product_col not in columns:
        return []

    selected_cols = [product_col]
    if brand_col:
        selected_cols.insert(0, brand_col)

    sql = text(f"""
        SELECT DISTINCT {", ".join(quote_identifier(col) for col in selected_cols)}
        FROM {quote_identifier(table_name)}
        WHERE {quote_identifier(product_col)} IS NOT NULL
          AND TRIM({quote_identifier(product_col)}) <> ''
        ORDER BY {", ".join(quote_identifier(col) for col in selected_cols)}
    """)
    df = pd.read_sql(sql, engine)

    options = []
    seen = set()
    for _, row in df.iterrows():
        product_name = str(row.get(product_col) or "").strip()
        brand_name = str(row.get(brand_col) or "").strip() if brand_col else ""
        label = f"{brand_name} {product_name}".strip()
        if label and label not in seen:
            seen.add(label)
            options.append(label)
    return options


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
    score_table = TABLE_CONFIG["score_table"]
    product_col = TABLE_CONFIG["product_name_col"]
    brand_col = TABLE_CONFIG["brand_name_col"]
    score_cols = TABLE_CONFIG["score_cols"]

    try:
        table_cols = get_table_columns(engine, score_table)
    except Exception:
        return {}

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
        df[col] = df[col].apply(lambda value, field=col: normalize_score(value, field))

    product_df = df[product_identity_mask(df, product_col, product_name, resolved_brand_col)]
    if product_df.empty:
        return {"total_products": len(df), "message": "未找到当前产品的池子位置"}

    product_row = product_df.iloc[0]
    score_summary = {}

    for col in selected_score_cols:
        valid = df[col].dropna()
        if valid.empty:
            continue

        value = product_row[col]
        if value is None or pd.isna(value):
            continue

        score_summary[col] = {
            "display_name": SCORE_DISPLAY_MAP.get(col, {}).get("name", col),
            "product_value": round(float(value), 2),
            "pool_avg": round(float(valid.mean()), 2),
            "pool_median": round(float(valid.median()), 2),
            "percentile": round(float((valid <= value).mean()), 3),
        }

    return {
        "total_products": len(df),
        "score_summary": score_summary,
    }


def get_ingredient_composition(engine, source_id: Any) -> str:
    if source_id is None or pd.isna(source_id):
        return ""
    sql = text("""
        SELECT ingredient_composition
        FROM csv_labeling.catfood_ingredient_ocr_parsed
        WHERE source_id = :source_id
        ORDER BY id DESC
        LIMIT 1
    """)
    try:
        df = pd.read_sql(sql, engine, params={"source_id": source_id})
    except Exception:
        return ""
    if df.empty:
        return ""
    return str(df.iloc[0].get("ingredient_composition") or "").strip()


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
    source_id = safe_get(score_row, "source_id") or safe_get(black_chin_row, "source_id") or safe_get(soft_stool_row, "source_id")
    ingredient_composition = get_ingredient_composition(engine, source_id)

    return {
        "score": score_row,
        "black_chin": black_chin_row,
        "soft_stool": soft_stool_row,
        "pool_summary": pool_summary,
        "ingredient_composition": ingredient_composition,
    }


# =========================================================
# 7. 配方画像构建：7 维
# =========================================================

def build_score_profile(score_row: Optional[pd.Series]) -> pd.DataFrame:
    rows = []
    if score_row is None:
        return pd.DataFrame()

    for col in TABLE_CONFIG["score_cols"]:
        if col not in score_row.index:
            continue

        meta = SCORE_DISPLAY_MAP.get(col, {})
        score_type = meta.get("type", "mixed")
        score = normalize_score(score_row[col], col)

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
    for field in fields:
        sub = score_df[score_df["field"] == field]
        if not sub.empty:
            score = sub.iloc[0]["score"]
            if score is not None and not pd.isna(score):
                return score
    return None


def _score_evidence_item(score_df: pd.DataFrame, field: str, meaning: Optional[str] = None) -> dict:
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
    valid = [
        (float(value), float(weight))
        for value, weight in weighted_values
        if value is not None and not pd.isna(value) and weight > 0
    ]
    if not valid:
        return None

    total_weight = sum(weight for _, weight in valid)
    return round(sum(value * weight for value, weight in valid) / total_weight, 2)


def build_compare_profile(score_df: pd.DataFrame) -> pd.DataFrame:
    """
    构建 7 维对比画像。
    与单品页不同：这里把“蛋白质量”和“蛋白压力”拆开，避免质量高被误解为压力低。
    """
    protein_quality = _first_score_from_profile(score_df, "protein_quality_score")
    protein_pressure = _first_score_from_profile(score_df, "protein_structure_score")
    carb_pressure = _first_score_from_profile(score_df, "starch_burden_score")
    fat_pressure = _first_score_from_profile(score_df, "fat_score")

    p_total = _first_score_from_profile(score_df, "p_total_score")
    p_buffer = _first_score_from_profile(score_df, "p_buffer")
    fiber_buffer = _weighted_avg_valid([
        (p_total, 0.40),
        (p_buffer, 0.60),
    ])

    q_feed = _first_score_from_profile(score_df, "q_feed")
    q_scfa = _first_score_from_profile(score_df, "q_scfa")
    microbiome_support = _weighted_avg_valid([
        (q_scfa, 0.60),
        (q_feed, 0.40),
    ])

    fat_regulation = _first_score_from_profile(score_df, "fat_regulation_score")
    skin_protection = fat_regulation

    rows = [
        {
            "dimension": "蛋白质量",
            "score": protein_quality,
            "type": "protective",
            "summary": "看蛋白来源质量、动物蛋白优势和蛋白正向支持；它不等同于蛋白压力低。",
            "underlying_scores": [
                _score_evidence_item(score_df, "protein_quality_score", "代表蛋白质量支持，适合关注保肌肉、动物蛋白质量的场景。"),
            ],
        },
        {
            "dimension": "蛋白压力",
            "score": protein_pressure,
            "type": "pressure",
            "summary": "看蛋白来源和蛋白结构是否偏复杂，以及是否可能增加消化或适应压力。",
            "underlying_scores": [
                _score_evidence_item(score_df, "protein_structure_score", "代表蛋白结构复杂度和结构负载。"),
            ],
        },
        {
            "dimension": "碳水负担",
            "score": carb_pressure,
            "type": "pressure",
            "summary": "看淀粉、豆类、薯类等碳水结构是否偏重，以及是否可能增加软便或发酵压力。",
            "underlying_scores": [
                _score_evidence_item(score_df, "starch_burden_score", "代表淀粉、豆薯类等原料带来的淀粉负担。"),
            ],
        },
        {
            "dimension": "脂肪负担",
            "score": fat_pressure,
            "type": "pressure",
            "summary": "看油脂压力是否偏高，以及是否可能关联黑下巴、皮脂或脂肪消化压力。",
            "underlying_scores": [
                _score_evidence_item(score_df, "fat_score", "代表脂肪压力，越高越需要关注黑下巴、油脂旺盛或脂肪消化压力。"),
            ],
        },
        {
            "dimension": "纤维缓冲",
            "score": fiber_buffer,
            "type": "protective",
            "summary": "看纤维是否真正能帮助便便成形、增加肠道缓冲，而不是只看纤维总量。",
            "underlying_scores": [
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
                _score_evidence_item(score_df, "q_feed", "代表供菌底物多少，偏高时要结合发酵稳定性判断。"),
                _score_evidence_item(score_df, "q_scfa", "代表菌群代谢支持，越高通常越有利于肠道稳定。"),
            ],
        },
        {
            "dimension": "皮肤保护",
            "score": skin_protection,
            "type": "protective",
            "summary": "看脂肪调节和皮肤稳定相关支持是否足够。",
            "underlying_scores": [
                _score_evidence_item(score_df, "fat_regulation_score", "代表脂肪调节、皮肤稳定和氧化压力缓冲支持。"),
            ],
        },
    ]

    return pd.DataFrame(rows)


def build_baseline_profile(pool_summary: dict) -> Optional[pd.DataFrame]:
    score_summary = (pool_summary or {}).get("score_summary") or {}
    if not score_summary:
        return None

    def get_median(*fields: str) -> Optional[float]:
        for field in fields:
            item = score_summary.get(field)
            if item and item.get("pool_median") is not None:
                return item.get("pool_median")
        return None

    protein_quality = get_median("protein_quality_score")
    protein_pressure = get_median("protein_structure_score")
    carb_pressure = get_median("starch_burden_score")
    fat_pressure = get_median("fat_score")
    p_total = get_median("p_total_score")
    p_buffer = get_median("p_buffer")
    fiber_buffer = _weighted_avg_valid([(p_total, 0.40), (p_buffer, 0.60)])
    q_feed = get_median("q_feed")
    q_scfa = get_median("q_scfa")
    microbiome_support = _weighted_avg_valid([(q_scfa, 0.60), (q_feed, 0.40)])
    skin_protection = get_median("fat_regulation_score")

    return pd.DataFrame([
        {"dimension": "蛋白质量", "score": protein_quality},
        {"dimension": "蛋白压力", "score": protein_pressure},
        {"dimension": "碳水负担", "score": carb_pressure},
        {"dimension": "脂肪负担", "score": fat_pressure},
        {"dimension": "纤维缓冲", "score": fiber_buffer},
        {"dimension": "菌群支持", "score": microbiome_support},
        {"dimension": "皮肤保护", "score": skin_protection},
    ])


def build_risk_relative_position(row: Optional[pd.Series]) -> str:
    """
    将 rank 转成用户友好的相对位置。
    假设 batch_rank 越小风险越靠前。
    不使用“前28%”这类易误导表达。
    """
    rank = safe_get(row, TABLE_CONFIG["rank_col"], None)
    percentile = safe_get(row, TABLE_CONFIG["percentile_col"], None)

    try:
        rank_value = int(rank)
    except Exception:
        rank_value = None

    try:
        pct = float(percentile)
    except Exception:
        pct = None

    # 如果 current_pool_percentile 是风险百分位，通常越高表示风险越靠前。
    # 若你的字段含义相反，可以在这里调整阈值逻辑。
    if pct is not None:
        if pct >= 0.70:
            return "相对位置：风险靠前"
        if pct >= 0.35:
            return "相对位置：中间区间"
        return "相对位置：低于多数产品"

    if rank_value is not None:
        return f"相对位置：排名第 {rank_value} 位"

    return "相对位置：暂无"


def build_product_context(product_name: str) -> Dict[str, Any]:
    data = get_product_data(product_name)

    score_row = data.get("score")
    black_chin_row = data.get("black_chin")
    soft_stool_row = data.get("soft_stool")

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
    profile_df = build_compare_profile(score_df)
    baseline_df = build_baseline_profile(data.get("pool_summary") or {})

    black_tags = merge_tag_cols(black_chin_row, TABLE_CONFIG["tag_cols"])
    stool_tags = merge_tag_cols(soft_stool_row, TABLE_CONFIG["tag_cols"])

    return {
        "query_name": product_name,
        "name": str(product_display_name),
        "brand_name": str(brand_name),
        "ingredient_composition": str(data.get("ingredient_composition") or ""),
        "data": data,
        "score_df": score_df,
        "profile_df": profile_df,
        "baseline_df": baseline_df,
        "black_chin_row": black_chin_row,
        "soft_stool_row": soft_stool_row,
        "black_chin_tags": black_tags,
        "soft_stool_tags": stool_tags,
        "black_chin_risk": {
            "risk_level": safe_get(black_chin_row, TABLE_CONFIG["risk_level_col"], "暂无"),
            "rank": safe_get(black_chin_row, TABLE_CONFIG["rank_col"], "暂无"),
            "percentile": safe_get(black_chin_row, TABLE_CONFIG["percentile_col"], "暂无"),
            "relative_position": build_risk_relative_position(black_chin_row),
            "tags": black_tags,
        },
        "soft_stool_risk": {
            "risk_level": safe_get(soft_stool_row, TABLE_CONFIG["risk_level_col"], "暂无"),
            "rank": safe_get(soft_stool_row, TABLE_CONFIG["rank_col"], "暂无"),
            "percentile": safe_get(soft_stool_row, TABLE_CONFIG["percentile_col"], "暂无"),
            "relative_position": build_risk_relative_position(soft_stool_row),
            "tags": stool_tags,
        },
    }


# =========================================================
# 8. 对比逻辑
# =========================================================

def build_profile_diff(product_a: Dict[str, Any], product_b: Dict[str, Any]) -> pd.DataFrame:
    a_df = product_a["profile_df"]
    b_df = product_b["profile_df"]

    a_map = {row["dimension"]: row for _, row in a_df.iterrows()}
    b_map = {row["dimension"]: row for _, row in b_df.iterrows()}

    dimensions = [
        "蛋白质量",
        "蛋白压力",
        "碳水负担",
        "脂肪负担",
        "纤维缓冲",
        "菌群支持",
        "皮肤保护",
    ]

    rows = []
    for dim in dimensions:
        a_row = a_map.get(dim, {})
        b_row = b_map.get(dim, {})

        a_score = a_row.get("score")
        b_score = b_row.get("score")

        diff = None
        if a_score is not None and b_score is not None and not pd.isna(a_score) and not pd.isna(b_score):
            diff = round(float(b_score) - float(a_score), 2)

        score_type = b_row.get("type") or a_row.get("type") or "mixed"

        rows.append({
            "dimension": dim,
            "product_a_score": a_score,
            "product_b_score": b_score,
            "diff_b_minus_a": diff,
            "type": score_type,
            "a_level": score_level(a_score, score_type),
            "b_level": score_level(b_score, score_type),
            "summary": b_row.get("summary") or a_row.get("summary") or "",
            "a_underlying_scores": a_row.get("underlying_scores", []),
            "b_underlying_scores": b_row.get("underlying_scores", []),
        })

    return pd.DataFrame(rows)


def describe_diff(row: pd.Series, product_a_name: str, product_b_name: str) -> str:
    dim = row["dimension"]
    diff = row["diff_b_minus_a"]
    score_type = row["type"]

    if diff is None or pd.isna(diff):
        return f"{dim}：两款产品暂无足够数据进行比较。"

    abs_diff = abs(float(diff))
    if abs_diff < 5:
        degree = "差异不大"
    elif abs_diff < 15:
        degree = "略有差异"
    elif abs_diff < 30:
        degree = "差异较明显"
    else:
        degree = "差异很明显"

    direction = "更高" if diff > 0 else "更低"

    if score_type == "pressure":
        meaning = "压力更高，需要更多观察" if diff > 0 else "压力更低，相对更轻一些"
    elif score_type == "protective":
        meaning = "支持更强" if diff > 0 else "支持更弱"
    else:
        meaning = "数值更高，需要结合搭配判断" if diff > 0 else "数值更低，也要结合具体需求判断"

    return f"{dim}：{product_b_name} 比 {product_a_name} {direction} {abs_diff:.1f} 分，{degree}；这通常意味着{meaning}。"


def build_core_diff_explanations(diff_df: pd.DataFrame, product_a_name: str, product_b_name: str) -> List[str]:
    """
    只解释差异，不直接做最终推荐。
    优先挑差异最大的 4-6 个维度。
    """
    df = diff_df.dropna(subset=["diff_b_minus_a"]).copy()
    if df.empty:
        return ["两款产品暂无足够画像数据进行差异解释。"]

    df["abs_diff"] = df["diff_b_minus_a"].abs()
    df = df.sort_values("abs_diff", ascending=False)

    selected = df[df["abs_diff"] >= 5].head(6)
    if selected.empty:
        selected = df.head(4)

    return [
        describe_diff(row, product_a_name, product_b_name)
        for _, row in selected.iterrows()
    ]


def filter_problem_tags(tags: List[str]) -> List[str]:
    """只保留真正的问题标签，不把“暂无明显...”这类占位内容作为标签展示。"""
    result = []
    for tag in tags or []:
        tag_text = str(tag).strip()
        if not tag_text:
            continue
        if tag_text.startswith("暂无") or tag_text in {"无", "None", "null", "nan"}:
            continue
        result.append(tag_text)
    return result


def build_tag_diff_summary(product_a: Dict[str, Any], product_b: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "black_chin": {
            "product_a_tags": filter_problem_tags(product_a["black_chin_tags"]),
            "product_b_tags": filter_problem_tags(product_b["black_chin_tags"]),
        },
        "soft_stool": {
            "product_a_tags": filter_problem_tags(product_a["soft_stool_tags"]),
            "product_b_tags": filter_problem_tags(product_b["soft_stool_tags"]),
        },
    }


def build_need_focus_table(diff_df: pd.DataFrame, product_a_name: str, product_b_name: str) -> pd.DataFrame:
    """
    不同需求下的关注点。
    这里只给“应该看哪些指标 + 当前差异提示”，不直接说谁绝对更好。
    """
    dim_map = {row["dimension"]: row for _, row in diff_df.iterrows()}

    def diff_text(dim: str) -> str:
        row = dim_map.get(dim)
        if row is None:
            return "暂无数据。"
        return describe_diff(row, product_a_name, product_b_name)

    rows = [
        {
            "猫咪情况 / 用户目标": "减重保肌肉",
            "更应该看什么": "蛋白质量、脂肪负担",
            "如何看 A 和 B": f"{diff_text('蛋白质量')} {diff_text('脂肪负担')}",
        },
        {
            "猫咪情况 / 用户目标": "肠胃敏感 / 易软便",
            "更应该看什么": "蛋白压力、碳水负担、纤维缓冲、菌群支持",
            "如何看 A 和 B": f"{diff_text('蛋白压力')} {diff_text('碳水负担')} {diff_text('纤维缓冲')} {diff_text('菌群支持')}",
        },
        {
            "猫咪情况 / 用户目标": "黑下巴 / 下巴易出油",
            "更应该看什么": "脂肪负担、皮肤保护、黑下巴标签",
            "如何看 A 和 B": f"{diff_text('脂肪负担')} {diff_text('皮肤保护')}",
        },
        {
            "猫咪情况 / 用户目标": "换粮容易应激",
            "更应该看什么": "蛋白压力、碳水负担、菌群支持",
            "如何看 A 和 B": f"{diff_text('蛋白压力')} {diff_text('碳水负担')} {diff_text('菌群支持')}",
        },
        {
            "猫咪情况 / 用户目标": "日常稳定喂养",
            "更应该看什么": "压力项是否突出、保护项是否短板明显",
            "如何看 A 和 B": "重点看两款产品是否存在明显高压力维度，以及皮肤保护、纤维缓冲是否出现短板。",
        },
    ]
    return pd.DataFrame(rows)


# =========================================================
# 9. 通义千问对比总结
# =========================================================

def profile_df_to_context(profile_df: pd.DataFrame) -> List[dict]:
    rows = []
    for _, row in profile_df.iterrows():
        score = row["score"]
        rows.append({
            "dimension": row["dimension"],
            "score": None if score is None or pd.isna(score) else score,
            "level": score_level(score, row["type"]),
            "type": row["type"],
            "summary": row["summary"],
            "underlying_scores": row["underlying_scores"],
        })
    return make_json_safe(rows)


def diff_df_to_context(diff_df: pd.DataFrame) -> List[dict]:
    rows = []
    for _, row in diff_df.iterrows():
        diff = row["diff_b_minus_a"]
        rows.append({
            "dimension": row["dimension"],
            "product_a_score": None if row["product_a_score"] is None or pd.isna(row["product_a_score"]) else row["product_a_score"],
            "product_b_score": None if row["product_b_score"] is None or pd.isna(row["product_b_score"]) else row["product_b_score"],
            "diff_b_minus_a": None if diff is None or pd.isna(diff) else diff,
            "type": row["type"],
            "product_a_level": row["a_level"],
            "product_b_level": row["b_level"],
            "meaning": row["summary"],
        })
    return make_json_safe(rows)


def build_llm_compare_context(
    product_a: Dict[str, Any],
    product_b: Dict[str, Any],
    diff_df: pd.DataFrame,
    core_diff_explanations: List[str],
    tag_diff_summary: Dict[str, Any],
    need_focus_df: pd.DataFrame,
) -> dict:
    return make_json_safe({
        "comparison_type": "候选产品对比，不默认存在旧粮/新粮或 A 换到 B 的关系。",
        "product_a": {
            "name": product_a["name"],
            "brand_name": product_a["brand_name"],
            "profile": profile_df_to_context(product_a["profile_df"]),
            "black_chin_risk": product_a["black_chin_risk"],
            "soft_stool_risk": product_a["soft_stool_risk"],
        },
        "product_b": {
            "name": product_b["name"],
            "brand_name": product_b["brand_name"],
            "profile": profile_df_to_context(product_b["profile_df"]),
            "black_chin_risk": product_b["black_chin_risk"],
            "soft_stool_risk": product_b["soft_stool_risk"],
        },
        "profile_diff": diff_df_to_context(diff_df),
        "tag_diff_summary": tag_diff_summary,
        "need_focus_for_llm_only": need_focus_df.to_dict(orient="records"),
        "note": "页面不单独展示核心差异解释和不同需求关注点；这些内容由通义千问在最终总结中整合。",
        "important_instruction": [
            "这是两个候选产品的中性对比，不要使用“从A换到B”“新粮”“旧粮”等表达，除非输入明确指定。",
            "蛋白质量和蛋白压力必须分开解释。protein_quality_score 高表示蛋白质量支持较好；protein_structure_score 高表示蛋白结构/消化压力偏高。",
            "风险等级、排序、标签均来自规则模型或数据库，大模型不得重新计算或推翻。",
            "大模型可以基于标签、风险等级和 profile_diff 做条件化推理，例如：如果更关注黑下巴……如果更关注软便……",
            "不要输出绝对推荐，不要说一定会黑下巴、一定会软便。",
            "不要编造成分、品牌背景、疾病诊断或临床结论。",
        ],
    })


def generate_qwen_compare_summary(context: dict) -> str:
    client = get_qwen_client()

    system_prompt = """
你是“宠析”的宠物食品配方对比分析助手。你的任务是把两款猫粮的配方画像差异、风险标签差异和规则模型结果，翻译成普通养猫用户能听懂的条件化对比判断。

你必须遵守：
1. 这是两个候选产品的中性比较，不一定存在旧粮/新粮关系。
2. 不要使用“从A换到B”“新粮”“旧粮”“换过去”等表达，除非输入明确指定。
3. 不要重新计算分数，不要改变风险等级、排序、标签。
4. 蛋白质量和蛋白压力必须分开解释：
   - 蛋白质量高 = 蛋白质量支持较好；
   - 蛋白压力高 = 蛋白结构/消化适应压力偏高；
   - 两者可以同时存在，不得互相抵消。
5. 风险结论必须基于标签、风险等级、相对位置和 profile_diff 进行逻辑推理。
6. 不能输出绝对推荐。请使用条件化表达：
   - 如果更关注黑下巴……
   - 如果更关注软便/肠胃稳定……
   - 如果更关注蛋白质量/保肌肉……
   - 如果只是日常稳定喂养……
7. 不要编造成分、品牌背景、疾病诊断或临床结论。
8. 不要使用“前xx%”这类容易误导的表达；使用“相对位置：低于多数产品/中间区间/风险靠前”等表达。
9. 不要输出 Markdown 表格。

输出结构固定为：

### 综合对比判断
用 2-4 句话概括两款产品的主要取舍，不要直接说谁绝对更好。

### 配方画像差异怎么看
解释 3-5 个最关键差异，必须包含蛋白质量与蛋白压力的区分；不要逐项复述所有分数。

### 标签差异与风险结构
只围绕已有问题标签解释黑下巴和软便的风险结构；如果某款产品没有对应问题标签，请写“未见明显问题标签”，不要创造新标签。

### 不同需求下怎么选
把不同猫咪需求放在这一节里，用条件化表达：
- 关注黑下巴/皮肤稳定
- 关注软便/肠胃稳定
- 关注蛋白质量/保肌肉
- 日常稳定喂养

### 需要重点观察的点
列出 3-5 个观察点，语气克制，不做医学诊断。
"""

    user_prompt = f"""
下面是两款猫粮的结构化对比数据。请基于这些数据生成页面展示用的自然语言对比总结。

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
# 10. 可视化
# =========================================================

def render_product_header(product_a: Dict[str, Any], product_b: Dict[str, Any]):
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown(
            f"""
            <div style="font-size:15px;font-weight:700;color:#374151;margin-bottom:2px;">产品 A</div>
            <div style="font-size:20px;font-weight:700;line-height:1.35;color:#111827;margin-bottom:4px;">{html.escape(product_a["name"])}</div>
            """,
            unsafe_allow_html=True,
        )
        if product_a["brand_name"]:
            st.caption(f"品牌：{product_a['brand_name']}")
        ingredient_text = product_a.get("ingredient_composition") or "暂无原始配料信息"
        st.markdown(
            f"""
            <div style="font-size:13px;line-height:1.6;color:#4B5563;border:1px solid #E5E7EB;border-radius:8px;padding:10px 12px;background:#F9FAFB;max-height:150px;overflow:auto;">
                <div style="font-weight:700;color:#374151;margin-bottom:4px;">原始配料信息</div>
                {html.escape(ingredient_text)}
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_b:
        st.markdown(
            f"""
            <div style="font-size:15px;font-weight:700;color:#374151;margin-bottom:2px;">产品 B</div>
            <div style="font-size:20px;font-weight:700;line-height:1.35;color:#111827;margin-bottom:4px;">{html.escape(product_b["name"])}</div>
            """,
            unsafe_allow_html=True,
        )
        if product_b["brand_name"]:
            st.caption(f"品牌：{product_b['brand_name']}")
        ingredient_text = product_b.get("ingredient_composition") or "暂无原始配料信息"
        st.markdown(
            f"""
            <div style="font-size:13px;line-height:1.6;color:#4B5563;border:1px solid #E5E7EB;border-radius:8px;padding:10px 12px;background:#F9FAFB;max-height:150px;overflow:auto;">
                <div style="font-weight:700;color:#374151;margin-bottom:4px;">原始配料信息</div>
                {html.escape(ingredient_text)}
            </div>
            """,
            unsafe_allow_html=True,
        )


def diff_bar_chart(diff_df: pd.DataFrame, product_a_name: str, product_b_name: str):
    """
    展示 A/B 两款产品在 7 维画像上的并列分数。

    不再使用 B-A 正负差异图，避免用户把负数误解成“变差”。
    读法：
    - 压力项：分数越低通常代表压力越轻。
    - 支持项：分数越高通常代表支持越强。
    - 双刃剑指标：需要结合风险标签和具体需求判断。
    """
    df = diff_df.dropna(subset=["product_a_score", "product_b_score"], how="all").copy()
    if df.empty:
        st.info("暂无足够数据生成画像对比图。")
        return

    # 反转顺序，让第一个维度显示在图上方。
    df = df.iloc[::-1]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["product_a_score"],
        y=df["dimension"],
        orientation="h",
        text=df["product_a_score"].apply(lambda x: "暂无" if pd.isna(x) else f"{x:.1f}"),
        textposition="auto",
        name=product_a_name,
    ))
    fig.add_trace(go.Bar(
        x=df["product_b_score"],
        y=df["dimension"],
        orientation="h",
        text=df["product_b_score"].apply(lambda x: "暂无" if pd.isna(x) else f"{x:.1f}"),
        textposition="auto",
        name=product_b_name,
    ))

    fig.update_layout(
        title="配方画像分数对比",
        xaxis=dict(title="画像分数（0-100）", range=[0, 100]),
        yaxis=dict(title=""),
        barmode="group",
        height=520,
        margin=dict(l=20, r=20, t=60, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    st.plotly_chart(fig, use_container_width=True)


def radar_chart(profile_df: pd.DataFrame, title: str, baseline_df: Optional[pd.DataFrame] = None):
    df = profile_df.dropna(subset=["score"]).copy()
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
        baseline_values = [baseline_map.get(category) for category in categories]
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
        title=title,
        polar=dict(
            radialaxis=dict(
                visible=True,
                range=[0, 100],
            )
        ),
        showlegend=True,
        height=420,
        margin=dict(l=40, r=40, t=60, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_side_by_side_radar(product_a: Dict[str, Any], product_b: Dict[str, Any]):
    col_a, col_b = st.columns(2)
    with col_a:
        radar_chart(product_a["profile_df"], f"A｜{product_a['name']}", product_a.get("baseline_df"))
    with col_b:
        radar_chart(product_b["profile_df"], f"B｜{product_b['name']}", product_b.get("baseline_df"))


def interpret_profile_diff(row: pd.Series, product_a_name: str, product_b_name: str) -> Tuple[str, str]:
    """为差异速览卡片生成更容易理解的标题和解释。"""
    dim = row["dimension"]
    diff = row["diff_b_minus_a"]
    score_type = row["type"]

    if diff is None or pd.isna(diff):
        return f"{dim}｜暂无足够数据", "两款产品在这个维度上暂无可比数据。"

    diff_value = float(diff)
    abs_diff = abs(diff_value)
    if abs_diff < 5:
        degree = "差异不大"
    elif abs_diff < 15:
        degree = "略有差异"
    elif abs_diff < 30:
        degree = "差异较明显"
    else:
        degree = "差异很明显"

    if score_type == "pressure":
        if diff_value > 0:
            title = f"{dim}｜{product_b_name} 压力更高"
            body = f"{product_b_name} 比 {product_a_name} 高 {abs_diff:.1f} 分，{degree}。压力项更高通常意味着需要更多观察。"
        else:
            title = f"{dim}｜{product_b_name} 压力更低"
            body = f"{product_b_name} 比 {product_a_name} 低 {abs_diff:.1f} 分，{degree}。压力项更低通常代表相对更轻。"
    elif score_type == "protective":
        if diff_value > 0:
            title = f"{dim}｜{product_b_name} 支持更强"
            body = f"{product_b_name} 比 {product_a_name} 高 {abs_diff:.1f} 分，{degree}。支持项更高通常代表保护力更足。"
        else:
            title = f"{dim}｜{product_b_name} 支持更弱"
            body = f"{product_b_name} 比 {product_a_name} 低 {abs_diff:.1f} 分，{degree}。支持项更低可能代表保护短板。"
    else:
        if diff_value > 0:
            title = f"{dim}｜{product_b_name} 数值更高"
            body = f"{product_b_name} 比 {product_a_name} 高 {abs_diff:.1f} 分，{degree}。这个维度不是越高越好，需要结合标签和猫咪需求判断。"
        else:
            title = f"{dim}｜{product_b_name} 数值更低"
            body = f"{product_b_name} 比 {product_a_name} 低 {abs_diff:.1f} 分，{degree}。这个维度需要结合标签和猫咪需求判断。"

    return title, body


def render_profile_score_table(diff_df: pd.DataFrame, product_a_name: str, product_b_name: str):
    rows = []
    for _, row in diff_df.iterrows():
        rows.append({
            "画像维度": row["dimension"],
            "产品 A 分数": row["product_a_score"],
            "产品 A 等级": row["a_level"],
            "产品 B 分数": row["product_b_score"],
            "产品 B 等级": row["b_level"],
            "差异 B-A": row["diff_b_minus_a"],
            "类型": {"pressure": "压力项", "protective": "支持项", "mixed": "双刃剑/需结合判断"}.get(row["type"], row["type"]),
        })
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "产品 A 分数": st.column_config.NumberColumn(f"A｜{product_a_name}", format="%.1f"),
            "产品 B 分数": st.column_config.NumberColumn(f"B｜{product_b_name}", format="%.1f"),
        },
    )


def render_core_diff(core_diff_explanations: List[str]):
    for item in core_diff_explanations:
        st.markdown(f"- {item}")


def _problem_tag_rows(product: Dict[str, Any]) -> List[dict]:
    rows = []
    for risk_name, tags in [
        ("黑下巴", filter_problem_tags(product.get("black_chin_tags", []))),
        ("软便", filter_problem_tags(product.get("soft_stool_tags", []))),
    ]:
        for tag in tags:
            rows.append({
                "风险类型": risk_name,
                "问题标签": tag,
                "解释": TAG_EXPLAIN_MAP.get(tag, "该标签表示该产品在这一维度上存在相对突出的配方特征。"),
            })
    return rows


def _canonical_tags(tags: List[str], aliases: Dict[str, str]) -> List[str]:
    result = []
    for tag in filter_problem_tags(tags):
        canonical = aliases.get(tag, tag)
        if canonical not in result:
            result.append(canonical)
    return result


def _has_hierarchy_tag(canonical_tags: List[str], label: str, children: Optional[List[str]] = None) -> bool:
    if label in canonical_tags:
        return True
    return any(child in canonical_tags for child in (children or []))


def _hit_text(hit: bool) -> str:
    return "命中" if hit else ""


def _risk_assessment_row(
    symptom_name: str,
    product_a: Dict[str, Any],
    product_b: Dict[str, Any],
    risk_key: str,
) -> dict:
    a_risk = product_a.get(risk_key, {}) or {}
    b_risk = product_b.get(risk_key, {}) or {}
    return {
        "二级病症": symptom_name,
        "产品 A 风险等级": a_risk.get("risk_level", "暂无"),
        "产品 A 风险概率": format_probability(a_risk.get("percentile")),
        "产品 A 相对位置": a_risk.get("relative_position", "暂无"),
        "产品 B 风险等级": b_risk.get("risk_level", "暂无"),
        "产品 B 风险概率": format_probability(b_risk.get("percentile")),
        "产品 B 相对位置": b_risk.get("relative_position", "暂无"),
    }


def render_risk_assessment_table(
    symptom_name: str,
    product_a: Dict[str, Any],
    product_b: Dict[str, Any],
    risk_key: str,
) -> None:
    st.dataframe(
        pd.DataFrame([_risk_assessment_row(symptom_name, product_a, product_b, risk_key)]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "二级病症": st.column_config.TextColumn("二级病症", width="small"),
            "产品 A 风险等级": st.column_config.TextColumn(f"A｜{product_a['name']} 风险等级", width="small"),
            "产品 A 风险概率": st.column_config.TextColumn("A 风险概率", width="small"),
            "产品 A 相对位置": st.column_config.TextColumn("A 相对位置", width="medium"),
            "产品 B 风险等级": st.column_config.TextColumn(f"B｜{product_b['name']} 风险等级", width="small"),
            "产品 B 风险概率": st.column_config.TextColumn("B 风险概率", width="small"),
            "产品 B 相对位置": st.column_config.TextColumn("B 相对位置", width="medium"),
        },
    )


def _build_hierarchy_tag_rows(
    product_a: Dict[str, Any],
    product_b: Dict[str, Any],
    tag_key: str,
    aliases: Dict[str, str],
    tag_tree: List[dict],
    label_column: str,
) -> List[dict]:
    a_tags = _canonical_tags(product_a.get(tag_key, []), aliases)
    b_tags = _canonical_tags(product_b.get(tag_key, []), aliases)
    rows = []
    for branch in tag_tree:
        parent = branch["label"]
        children = branch.get("children", [])
        parent_a_hit = _has_hierarchy_tag(a_tags, parent, children)
        parent_b_hit = _has_hierarchy_tag(b_tags, parent, children)
        if parent_a_hit or parent_b_hit:
            rows.append({
                "层级": "一级",
                label_column: parent,
                "产品 A": _hit_text(parent_a_hit),
                "产品 B": _hit_text(parent_b_hit),
                "标签解释": TAG_EXPLAIN_MAP.get(parent, ""),
            })
        for child in children:
            child_a_hit = child in a_tags
            child_b_hit = child in b_tags
            if child_a_hit or child_b_hit:
                rows.append({
                    "层级": "二级",
                    label_column: f"  └ {child}",
                    "产品 A": _hit_text(child_a_hit),
                    "产品 B": _hit_text(child_b_hit),
                    "标签解释": TAG_EXPLAIN_MAP.get(child, ""),
                })
    other_tags = [
        tag
        for tag in list(dict.fromkeys(a_tags + b_tags))
        if tag not in {branch["label"] for branch in tag_tree}
        and not any(tag in branch.get("children", []) for branch in tag_tree)
    ]
    for tag in other_tags:
        rows.append({
            "层级": "其他",
            label_column: tag,
            "产品 A": _hit_text(tag in a_tags),
            "产品 B": _hit_text(tag in b_tags),
            "标签解释": TAG_EXPLAIN_MAP.get(tag, "该标签表示该产品在这一维度上存在相对突出的配方特征。"),
        })
    if not rows:
        rows.append({
            "层级": "",
            label_column: "未见明显问题标签",
            "产品 A": "",
            "产品 B": "",
            "标签解释": "",
        })
    return rows


def _build_black_chin_tag_rows(product_a: Dict[str, Any], product_b: Dict[str, Any]) -> List[dict]:
    return _build_hierarchy_tag_rows(
        product_a,
        product_b,
        "black_chin_tags",
        BLACK_CHIN_TAG_ALIASES,
        BLACK_CHIN_TAG_TREE,
        "黑下巴标签",
    )


def _build_soft_stool_tag_rows(product_a: Dict[str, Any], product_b: Dict[str, Any]) -> List[dict]:
    return _build_hierarchy_tag_rows(
        product_a,
        product_b,
        "soft_stool_tags",
        SOFT_STOOL_TAG_ALIASES,
        SOFT_STOOL_TAG_TREE,
        "软便标签",
    )


def render_tag_cards(product_a: Dict[str, Any], product_b: Dict[str, Any]):
    """按风险类型拆成 tab，A/B 对齐展示。"""
    tab_black, tab_soft = st.tabs(["黑下巴", "软便"])
    with tab_black:
        st.markdown("#### 病症风险评估")
        render_risk_assessment_table("黑下巴", product_a, product_b, "black_chin_risk")
        st.markdown("#### 黑下巴标签逻辑")
        st.dataframe(
            pd.DataFrame(_build_black_chin_tag_rows(product_a, product_b)),
            use_container_width=True,
            hide_index=True,
            column_config={
                "层级": st.column_config.TextColumn("层级", width="small"),
                "黑下巴标签": st.column_config.TextColumn("黑下巴标签", width="medium"),
                "产品 A": st.column_config.TextColumn(f"A｜{product_a['name']}", width="small"),
                "产品 B": st.column_config.TextColumn(f"B｜{product_b['name']}", width="small"),
                "标签解释": st.column_config.TextColumn("标签解释", width="large"),
            },
        )
    with tab_soft:
        st.markdown("#### 病症风险评估")
        render_risk_assessment_table("软便", product_a, product_b, "soft_stool_risk")
        st.markdown("#### 软便标签逻辑")
        st.dataframe(
            pd.DataFrame(_build_soft_stool_tag_rows(product_a, product_b)),
            use_container_width=True,
            hide_index=True,
            column_config={
                "层级": st.column_config.TextColumn("层级", width="small"),
                "软便标签": st.column_config.TextColumn("软便标签", width="medium"),
                "产品 A": st.column_config.TextColumn(f"A｜{product_a['name']}", width="small"),
                "产品 B": st.column_config.TextColumn(f"B｜{product_b['name']}", width="small"),
                "标签解释": st.column_config.TextColumn("标签解释", width="large"),
            },
        )


def render_underlying_evidence(product_a: Dict[str, Any], product_b: Dict[str, Any]):
    st.markdown("### 核心差异速览")
    with st.expander("7维画像底层 score 证据", expanded=True):
        a_map = {row["dimension"]: row for _, row in product_a["profile_df"].iterrows()}
        b_map = {row["dimension"]: row for _, row in product_b["profile_df"].iterrows()}

        for dimension in ["蛋白质量", "蛋白压力", "碳水负担", "脂肪负担", "纤维缓冲", "菌群支持", "皮肤保护"]:
            a_row = a_map.get(dimension, {})
            b_row = b_map.get(dimension, {})
            st.markdown(f"#### {dimension}")
            st.caption(
                f"A：{score_level(a_row.get('score'), a_row.get('type', 'mixed'))}（{format_score_value(a_row.get('score'))}） ｜ "
                f"B：{score_level(b_row.get('score'), b_row.get('type', 'mixed'))}（{format_score_value(b_row.get('score'))}）"
            )

            evidence_by_field: Dict[str, Dict[str, Any]] = {}
            for side, row in [("a", a_row), ("b", b_row)]:
                for item in row.get("underlying_scores", []) or []:
                    field = str(item.get("field") or item.get("name") or "")
                    if not field:
                        continue
                    bucket = evidence_by_field.setdefault(
                        field,
                        {
                            "底层字段": field,
                            "展示名": item.get("name"),
                            "含义": item.get("meaning"),
                        },
                    )
                    bucket[f"{side}_score"] = item.get("score")
                    bucket[f"{side}_level"] = item.get("level")
                    bucket["展示名"] = bucket.get("展示名") or item.get("name")
                    bucket["含义"] = bucket.get("含义") or item.get("meaning")

            evidence_rows = []
            for item in evidence_by_field.values():
                evidence_rows.append({
                    "底层字段": item.get("底层字段"),
                    "展示名": item.get("展示名"),
                    "产品 A 分数": item.get("a_score"),
                    "产品 A 等级": item.get("a_level") or "暂无数据",
                    "产品 B 分数": item.get("b_score"),
                    "产品 B 等级": item.get("b_level") or "暂无数据",
                    "含义": item.get("含义"),
                })

            st.dataframe(
                pd.DataFrame(evidence_rows),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "底层字段": st.column_config.TextColumn("底层字段", width="medium"),
                    "展示名": st.column_config.TextColumn("展示名", width="medium"),
                    "产品 A 分数": st.column_config.NumberColumn(f"A｜{product_a['name']}", format="%.2f"),
                    "产品 A 等级": st.column_config.TextColumn("A 等级", width="small"),
                    "产品 B 分数": st.column_config.NumberColumn(f"B｜{product_b['name']}", format="%.2f"),
                    "产品 B 等级": st.column_config.TextColumn("B 等级", width="small"),
                    "含义": st.column_config.TextColumn("含义", width="large"),
                },
            )


# =========================================================
# 11. Streamlit 页面
# =========================================================

def render_page():
    st.set_page_config(
        page_title="宠析 - 双产品配方对比",
        layout="wide",
    )

    st.title("宠析｜双产品配方画像对比")

    with st.sidebar:
        st.markdown("## 配置检查")
        st.write(f"通义千问模型：`{QWEN_CONFIG['model']}`")

        if os.getenv("DASHSCOPE_API_KEY"):
            st.success("已检测到 DASHSCOPE_API_KEY")
        else:
            st.warning("未检测到 DASHSCOPE_API_KEY")

        st.markdown("## 说明")
        st.caption("本页是候选产品中性对比，不默认 A 是旧粮、B 是新粮。")

    try:
        product_options = load_product_options()
    except Exception as e:
        st.error(f"产品下拉列表加载失败：{e}")
        return
    if not product_options:
        st.warning("没有可选择的产品。请检查 score 宽表是否有品牌和产品数据。")
        return

    select_options = [""] + product_options
    col_input_a, col_input_b = st.columns(2)

    with col_input_a:
        product_a_query = st.selectbox(
            "请选择产品 A",
            select_options,
            format_func=lambda value: "输入关键词搜索品牌/产品" if not value else value,
            key="product_a_select",
        )

    with col_input_b:
        product_b_query = st.selectbox(
            "请选择产品 B",
            select_options,
            format_func=lambda value: "输入关键词搜索品牌/产品" if not value else value,
            key="product_b_select",
        )

    if not product_a_query or not product_b_query:
        st.info("请输入两个品牌产品名称后开始对比。")
        return

    if product_a_query.strip() == product_b_query.strip():
        st.warning("产品 A 和产品 B 品牌产品名称相同，请输入两款不同产品。")
        return

    try:
        product_a = build_product_context(product_a_query)
        product_b = build_product_context(product_b_query)
    except Exception as e:
        st.error(f"数据库查询失败：{e}")
        return

    if (
        product_a["data"].get("score") is None
        and product_a["data"].get("black_chin") is None
        and product_a["data"].get("soft_stool") is None
    ):
        st.warning("没有查询到产品 A。请检查品牌产品名称或数据库记录。")
        return

    if (
        product_b["data"].get("score") is None
        and product_b["data"].get("black_chin") is None
        and product_b["data"].get("soft_stool") is None
    ):
        st.warning("没有查询到产品 B。请检查品牌产品名称或数据库记录。")
        return

    render_product_header(product_a, product_b)

    diff_df = build_profile_diff(product_a, product_b)
    core_diff_explanations = build_core_diff_explanations(diff_df, product_a["name"], product_b["name"])
    tag_diff_summary = build_tag_diff_summary(product_a, product_b)
    need_focus_df = build_need_focus_table(diff_df, product_a["name"], product_b["name"])

    llm_context = build_llm_compare_context(
        product_a=product_a,
        product_b=product_b,
        diff_df=diff_df,
        core_diff_explanations=core_diff_explanations,
        tag_diff_summary=tag_diff_summary,
        need_focus_df=need_focus_df,
    )
    input_hash = calc_input_hash(llm_context)

    # =====================================================
    # 1. 配方画像差异
    # =====================================================

    st.markdown("---")
    st.markdown("## 1. 配方画像差异")
    st.caption("这里直接展示两款产品在 7 个画像维度上的分数。压力项越低通常越轻，支持项越高通常越强；双刃剑指标需要结合标签和具体需求判断。")

    render_side_by_side_radar(product_a, product_b)
    diff_bar_chart(diff_df, product_a["name"], product_b["name"])
    render_underlying_evidence(product_a, product_b)

    # =====================================================
    # 2. 病症概率评估
    # =====================================================

    st.markdown("---")
    st.markdown("## 2. 病症概率评估")
    st.caption("按黑下巴、软便两个二级病症展示风险等级、风险概率和命中的问题标签。风险概率来自当前池风险百分位。")
    render_tag_cards(product_a, product_b)

    # =====================================================
    # 3. 通义千问 AI 对比总结
    # =====================================================

    st.markdown("---")
    st.markdown("## 3. 通义千问 AI 对比总结")
    st.caption(f"当前对比输入指纹：`{input_hash[:12]}`。同一组产品数据不变时，AI 总结依据一致。")

    if st.button("生成通义千问 AI 对比总结", type="primary"):
        with st.spinner("正在调用通义千问生成对比总结..."):
            try:
                ai_text = generate_qwen_compare_summary(llm_context)
                st.session_state[f"ai_compare_text_{input_hash}"] = ai_text
            except Exception as e:
                st.error(f"通义千问对比总结生成失败：{e}")

    cached_text = st.session_state.get(f"ai_compare_text_{input_hash}")
    if cached_text:
        st.markdown(cached_text)
    else:
        st.info("点击按钮后，通义千问会基于配方画像差异、问题标签、风险等级，并结合不同猫咪需求生成条件化对比总结。")


if __name__ == "__main__":
    render_page()
