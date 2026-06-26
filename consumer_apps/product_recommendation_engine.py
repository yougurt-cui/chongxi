# -*- coding: utf-8 -*-
"""
宠析｜猫咪体质推荐引擎 Demo

实现流程：
1. 用户选择主问题 + 体质信号
2. 从数据库读取基础推荐目标画像池与用户信号微调规则
3. build_adjusted_profiles_for_case()
4. 得到微调后的推荐目标画像
5. 对产品池每款产品计算 fit_score
6. 输出 Top N 推荐产品
7. 调用通义千问解释推荐原因

运行：
streamlit run recommendation_engine_app.py

依赖：
pip install streamlit pandas sqlalchemy pymysql openai

说明：
- 本脚本默认使用 MySQL。
- 画像池配置表和用户信号规则表字段如果与你本地不同，主要修改 CONFIG_TABLES。
- 产品 score 宽表默认使用 catfood_protein_fat_fiber_score_wide。
- 风险结果表默认使用 sku_risk_score_result。
"""

import copy
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from openai import OpenAI
from sqlalchemy import create_engine, text

from app_config import get_feature_mysql_config


# =========================================================
# 1. 数据库与表配置
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

CONFIG_TABLES = {
    # 推荐目标画像池表
    # 推荐字段：profile_code, profile_name, symptom_type, mechanism_desc,
    # suitable_for_json, target_json, weight_json, threshold_json, avoid_rule_json,
    # target_explanation_json, is_active
    "profile_table": os.getenv("RECOMMENDATION_PROFILE_TABLE", "recommendation_profiles"),

    # 用户信号微调规则表
    # 推荐字段：signal_code, label, boost_profiles_json, weight_adjust_json,
    # threshold_adjust_json, is_active
    "signal_rule_table": os.getenv("RECOMMENDATION_SIGNAL_RULE_TABLE", "recommendation_signal_rules"),

    # 产品 score 宽表
    "score_table": "catfood_protein_fat_fiber_score_wide",

    # 风险结果表
    "risk_table": "sku_risk_score_result",
    "black_chin_score_model_like": "BLACK_CHIN%",
    "soft_stool_score_model_like": "SOFT_STOOL%",

    # 产品字段
    "product_name_col": "product_name",
    "brand_name_col": "brand",
    "risk_product_name_col": "sku_name",

    # 风险字段
    "risk_level_col": "current_pool_risk_level",
    "rank_col": "batch_rank",
    "percentile_col": "current_pool_percentile",
    "tag_cols": [
        "main_reason_tags",
        "support_reason_tags",
        "fat_detail_tags",
        "all_reason_tags",
    ],
}

QWEN_CONFIG = {
    "model": os.getenv("QWEN_MODEL", "qwen-plus"),
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "temperature": 0.35,
    "max_tokens": 1800,
}


# =========================================================
# 2. score 字段、量纲与展示名
# =========================================================

SCORE_COLS = [
    "protein_quality_score",
    "protein_score",
    "protein_structure_score",
    "carb_score",
    "starch_burden_score",
    "fiber_score",
    "p_total_score",
    "p_buffer",
    "fat_score",
    "fat_regulation_score",
    "prebiotic_score",
    "q_feed",
    "q_scfa",
    "antioxidant_score",
]

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
    "prebiotic_score": 5.0,
}

FEATURE_DISPLAY = {
    "protein_quality": "蛋白质量",
    "protein_pressure": "蛋白压力",
    "carb_pressure": "碳水负担",
    "fat_pressure": "脂肪负担",
    "fiber_buffer": "纤维缓冲",
    "p_buffer": "肠道缓冲力",
    "p_total_score": "纤维总支持",
    "prebiotic_score": "益生元支持",
    "q_feed": "供菌底物",
    "q_scfa": "菌群代谢支持",
    "skin_protection": "皮肤保护",
    "antioxidant_score": "抗氧化保护",
    "fat_regulation_score": "脂肪调节支持",
    "q_feed_excess_penalty": "供菌过量惩罚",
}

FEATURE_TYPE = {
    "protein_quality": "protective",
    "protein_pressure": "pressure",
    "carb_pressure": "pressure",
    "fat_pressure": "pressure",
    "fiber_buffer": "protective",
    "p_buffer": "protective",
    "p_total_score": "protective",
    "prebiotic_score": "mixed",
    "q_feed": "mixed",
    "q_scfa": "protective",
    "skin_protection": "protective",
    "antioxidant_score": "protective",
    "fat_regulation_score": "protective",
    "q_feed_excess_penalty": "penalty",
}

SYMPTOM_LABELS = {
    "soft_stool": "软便",
    "black_chin": "黑下巴",
    "tear_stain": "泪痕",
    "vomit": "呕吐",
    "weight_loss": "减肥",
    "weight_gain": "增重",
}

CAT_AGE_OPTIONS = ["0～1年", "1～3年", "3～6年", "6年以上"]
LONG_TERM_PROBLEM_OPTIONS = ["黑下巴反复", "肠胃敏感", "皮肤敏感", "泌尿问题", "挑食", "体重管理", "便软食物不耐受"]
CURRENT_OBSERVATION_OPTIONS = ["下巴出油", "特别/黑下巴", "软便", "拉稀", "呕吐", "食欲下降", "掉食", "泪痕加重", "便秘"]
ORIGIN_PREF_OPTIONS = ["不限", "国产", "进口"]
PRICE_PREF_OPTIONS = ["不限", "50元/斤内", "50-80元/斤", "80元+/斤"]
FUNCTION_PREF_OPTIONS = ["不限", "肠胃友好", "黑下巴友好", "美毛护肤", "控重管理", "低敏尝试"]

SYMPTOM_TRIGGER_RULES = [
    ("black_chin", ["黑下巴", "下巴出油"]),
    ("soft_stool", ["软便", "拉稀", "肠胃"]),
    ("tear_stain", ["泪痕"]),
    ("vomit", ["呕吐"]),
    ("weight_loss", ["体重管理", "控重"]),
]


# =========================================================
# 3. 基础工具函数
# =========================================================

def get_engine():
    url = (
        f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
        f"?charset={DB_CONFIG['charset']}"
    )
    return create_engine(url)


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


def json_loads_safe(value: Any, default: Any = None) -> Any:
    if default is None:
        default = {}
    if value is None or pd.isna(value):
        return default
    if isinstance(value, (dict, list)):
        return value
    text_value = str(value).strip()
    if not text_value:
        return default
    try:
        return json.loads(text_value)
    except Exception:
        return default


def normalize_score(x: Any, field: Optional[str] = None) -> Optional[float]:
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


def weighted_avg_valid(weighted_values: List[Tuple[Optional[float], float]]) -> Optional[float]:
    valid = [
        (float(value), float(weight))
        for value, weight in weighted_values
        if value is not None and not pd.isna(value) and weight > 0
    ]
    if not valid:
        return None
    total_weight = sum(weight for _, weight in valid)
    return round(sum(value * weight for value, weight in valid) / total_weight, 2)


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
    parts = re.split(r"[,，、;；|/]+", s)
    return [p.strip() for p in parts if p.strip()]


def merge_tag_cols(row: Optional[pd.Series], tag_cols: List[str]) -> List[str]:
    if row is None or row.empty:
        return []
    tags: List[str] = []
    for col in tag_cols:
        if col in row.index:
            tags.extend(parse_tags(row[col]))
    seen = set()
    result = []
    for tag in tags:
        if tag not in seen and not tag.startswith("暂无"):
            seen.add(tag)
            result.append(tag)
    return result


def safe_get(row: Optional[pd.Series], col: Optional[str], default: Any = None) -> Any:
    if row is None or col is None:
        return default
    if col not in row.index:
        return default
    value = row[col]
    if pd.isna(value):
        return default
    return value


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


# =========================================================
# 4. 从数据库读取画像池与信号规则
# =========================================================

def load_recommendation_profiles(engine) -> Dict[str, Dict[str, Any]]:
    table = CONFIG_TABLES["profile_table"]
    columns = get_table_columns(engine, table)

    sql = text(f"SELECT * FROM {quote_identifier(table)} WHERE COALESCE(is_active, 1) = 1") if "is_active" in columns else text(f"SELECT * FROM {quote_identifier(table)}")
    df = pd.read_sql(sql, engine)

    profiles: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        # 兼容：整行 JSON 存储模式
        if "profile_json" in df.columns:
            profile = json_loads_safe(row.get("profile_json"), {})
        elif "config_json" in df.columns:
            profile = json_loads_safe(row.get("config_json"), {})
        else:
            profile = {
                "profile_code": row.get("profile_code"),
                "profile_name": row.get("profile_name"),
                "symptom_type": row.get("symptom_type"),
                "suitable_for": json_loads_safe(row.get("suitable_for_json"), []),
                "mechanism": row.get("mechanism_desc") or row.get("mechanism") or "",
                "target": json_loads_safe(row.get("target_json"), {}),
                "weights": json_loads_safe(row.get("weight_json"), {}),
                "thresholds": json_loads_safe(row.get("threshold_json"), {}),
                "avoid_rules": json_loads_safe(row.get("avoid_rule_json"), []),
                "target_explanation": json_loads_safe(row.get("target_explanation_json"), []),
            }

        code = profile.get("profile_code")
        if code:
            profiles[str(code)] = make_json_safe(profile)

    return profiles


def load_signal_rules(engine) -> Dict[str, Dict[str, Any]]:
    table = CONFIG_TABLES["signal_rule_table"]
    columns = get_table_columns(engine, table)

    sql = text(f"SELECT * FROM {quote_identifier(table)} WHERE COALESCE(is_active, 1) = 1") if "is_active" in columns else text(f"SELECT * FROM {quote_identifier(table)}")
    df = pd.read_sql(sql, engine)

    rules: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        # 兼容：整行 JSON 存储模式
        if "rule_json" in df.columns:
            rule = json_loads_safe(row.get("rule_json"), {})
        elif "config_json" in df.columns:
            rule = json_loads_safe(row.get("config_json"), {})
        else:
            rule = {
                "signal_code": row.get("signal_code"),
                "label": row.get("label"),
                "boost_profiles": json_loads_safe(row.get("boost_profiles_json"), []),
                "weight_adjust": json_loads_safe(row.get("weight_adjust_json"), {}),
                "threshold_adjust": json_loads_safe(row.get("threshold_adjust_json"), {}),
            }

        code = rule.get("signal_code") or row.get("signal_code") if "signal_code" in df.columns else None
        if code:
            rules[str(code)] = make_json_safe(rule)

    return rules


# =========================================================
# 5. 画像选择与微调
# =========================================================

def select_base_profiles(
    profiles: Dict[str, Dict[str, Any]],
    signal_rules: Dict[str, Dict[str, Any]],
    symptom_type: str,
    user_signals: Optional[List[str]] = None,
    top_n: int = 3,
) -> List[Dict[str, Any]]:
    user_signals = user_signals or []

    candidate_codes = [
        code for code, profile in profiles.items()
        if profile.get("symptom_type") == symptom_type
    ]
    if not candidate_codes:
        return []

    profile_score = {code: 1.0 for code in candidate_codes}

    for signal in user_signals:
        rule = signal_rules.get(signal)
        if not rule:
            continue
        for code in rule.get("boost_profiles", []):
            if code in profile_score:
                profile_score[code] += 1.0
            elif code in profiles:
                # 允许跨症状强相关画像进入候选，但初始分较低
                profile_score[code] = 0.75

    sorted_codes = sorted(profile_score.keys(), key=lambda c: profile_score[c], reverse=True)

    selected = []
    for code in sorted_codes[:top_n]:
        profile = copy.deepcopy(profiles[code])
        profile["_selection_score"] = round(float(profile_score[code]), 3)
        selected.append(profile)
    return selected


def adjust_profile_by_user_signals(
    profile: Dict[str, Any],
    signal_rules: Dict[str, Dict[str, Any]],
    user_signals: Optional[List[str]] = None,
) -> Dict[str, Any]:
    user_signals = user_signals or []
    adjusted = copy.deepcopy(profile)
    adjusted.setdefault("weights", {})
    adjusted.setdefault("thresholds", {})
    adjusted.setdefault("adjustment_notes", [])

    for signal in user_signals:
        rule = signal_rules.get(signal)
        if not rule:
            continue
        label = rule.get("label", signal)
        adjusted["adjustment_notes"].append(f"根据用户输入「{label}」进行画像微调。")

        for feature, delta in rule.get("weight_adjust", {}).items():
            old_value = float(adjusted["weights"].get(feature, 0.0))
            adjusted["weights"][feature] = round(old_value + float(delta), 4)

        for threshold_name, delta in rule.get("threshold_adjust", {}).items():
            old_value = adjusted["thresholds"].get(threshold_name)
            if old_value is None:
                continue
            adjusted["thresholds"][threshold_name] = max(0, min(100, round(float(old_value) + float(delta), 2)))

    return adjusted


def build_adjusted_profiles_for_case(
    profiles: Dict[str, Dict[str, Any]],
    signal_rules: Dict[str, Dict[str, Any]],
    symptom_type: str,
    user_signals: Optional[List[str]] = None,
    top_n: int = 3,
) -> List[Dict[str, Any]]:
    base_profiles = select_base_profiles(
        profiles=profiles,
        signal_rules=signal_rules,
        symptom_type=symptom_type,
        user_signals=user_signals,
        top_n=top_n,
    )
    return [
        adjust_profile_by_user_signals(profile, signal_rules, user_signals)
        for profile in base_profiles
    ]


# =========================================================
# 5.1 历史粮参考与目标画像微调
# =========================================================

HISTORY_REACTION_MODES = {
    "problem": "不适 / 疑似触发当前问题",
    "good": "适应良好 / 希望保留相似稳定结构",
    "context_only": "仅作为背景，不参与微调",
}

FEATURE_THRESHOLD_MAP = {
    "protein_pressure": ("protein_pressure_max", None),
    "carb_pressure": ("carb_pressure_max", None),
    "fat_pressure": ("fat_pressure_max", None),
    "q_feed": ("q_feed_max", None),
    "protein_quality": (None, "protein_quality_min"),
    "p_buffer": (None, "p_buffer_min"),
    "fiber_buffer": (None, "fiber_buffer_min"),
    "q_scfa": (None, "q_scfa_min"),
    "skin_protection": (None, "skin_protection_min"),
    "antioxidant_score": (None, "antioxidant_score_min"),
    "fat_regulation_score": (None, "fat_regulation_score_min"),
}


def split_history_food_names(text_value: str) -> List[str]:
    """支持换行、逗号、顿号、分号分隔历史粮名称。"""
    if not text_value:
        return []
    parts = re.split(r"[\n,，、;；]+", text_value)
    names = [part.strip() for part in parts if part.strip()]
    # 去重但保留顺序
    return list(dict.fromkeys(names))


def query_score_row_by_product_name(engine, product_name: str) -> Optional[pd.Series]:
    """按产品名/品牌/product_key 模糊查询 score 宽表，返回最相关的一条记录。"""
    table = CONFIG_TABLES["score_table"]
    product_col = CONFIG_TABLES["product_name_col"]
    brand_col = CONFIG_TABLES["brand_name_col"]
    columns = get_table_columns(engine, table)
    if product_col not in columns:
        return None

    query_value = str(product_name or "").strip()
    if not query_value:
        return None
    query_value = re.sub(r"\.(?:jpg|jpeg|png|webp|bmp|heic|heif|tif|tiff|jfif)$", "", query_value, flags=re.IGNORECASE)
    query_compact = re.sub(r"[\s_\-·•|｜]+", "", query_value)

    searchable_cols = [product_col]
    if brand_col in columns:
        searchable_cols.append(brand_col)
    if "product_key" in columns:
        searchable_cols.append("product_key")

    compact_exprs = [
        (
            "REPLACE(REPLACE(REPLACE(REPLACE("
            f"{quote_identifier(col)}, ' ', ''), '　', ''), '||', ''), '|', '')"
        )
        for col in searchable_cols
    ]
    if brand_col in columns:
        compact_exprs.append(
            "REPLACE(REPLACE(REPLACE(CONCAT("
            f"COALESCE({quote_identifier(brand_col)}, ''), COALESCE({quote_identifier(product_col)}, '')"
            "), ' ', ''), '　', ''), '|', '')"
        )

    order_sql = ""
    if "calculated_at" in columns:
        order_sql = ", calculated_at DESC"
    elif "created_at" in columns:
        order_sql = ", created_at DESC"
    elif "updated_at" in columns:
        order_sql = ", updated_at DESC"

    raw_match_conditions = [
        f"{quote_identifier(col)} LIKE :query_like" for col in searchable_cols
    ]
    compact_match_conditions = [
        f"{expr} LIKE :query_compact_like" for expr in compact_exprs
    ]
    match_conditions = " OR ".join(raw_match_conditions + compact_match_conditions)
    exact_rank = " + ".join(
        f"CASE WHEN {quote_identifier(col)} = :query_exact THEN 10 ELSE 0 END"
        for col in searchable_cols
    )
    contains_rank = " + ".join(
        f"CASE WHEN {quote_identifier(col)} LIKE :query_like THEN 1 ELSE 0 END"
        for col in searchable_cols
    )
    compact_rank = " + ".join(
        f"CASE WHEN {expr} LIKE :query_compact_like THEN 1 ELSE 0 END"
        for expr in compact_exprs
    )

    sql = text(f"""
        SELECT *
        FROM {quote_identifier(table)}
        WHERE {match_conditions}
        ORDER BY ({exact_rank}) DESC,
                 ({contains_rank}) DESC,
                 ({compact_rank}) DESC
                 {order_sql}
        LIMIT 1
    """)
    df = pd.read_sql(
        sql,
        engine,
        params={
            "query_exact": query_value,
            "query_like": f"%{query_value}%",
            "query_compact_like": f"%{query_compact}%",
        },
    )
    if df.empty:
        return None
    return df.iloc[0]


def normalize_single_score_row(score_row: pd.Series) -> pd.DataFrame:
    """把单条 score 记录转成含衍生特征的一行 DataFrame。"""
    row_dict = score_row.to_dict()
    df = pd.DataFrame([row_dict])
    for col in SCORE_COLS:
        if col in df.columns:
            df[col] = df[col].apply(lambda x, field=col: normalize_score(x, field))
    return add_derived_features(df)


def product_feature_snapshot(row: pd.Series) -> Dict[str, Any]:
    """提取历史粮用于展示和大模型解释的关键特征。"""
    keys = [
        "protein_quality", "protein_pressure", "carb_pressure", "fat_pressure",
        "fiber_buffer", "p_buffer", "q_feed", "q_scfa", "skin_protection",
        "q_feed_excess_penalty", "antioxidant_score", "fat_regulation_score",
    ]
    return {key: None if pd.isna(row.get(key)) else row.get(key) for key in keys}


def _add_weight_adjust(adjust: Dict[str, Any], feature: str, delta: float, reason: str):
    adjust.setdefault("weight_adjust", {})
    adjust.setdefault("notes", [])
    old_value = float(adjust["weight_adjust"].get(feature, 0.0))
    adjust["weight_adjust"][feature] = round(old_value + float(delta), 4)
    adjust["notes"].append(reason)


def _add_threshold_adjust(adjust: Dict[str, Any], threshold_name: Optional[str], delta: float):
    if not threshold_name:
        return
    adjust.setdefault("threshold_adjust", {})
    old_value = float(adjust["threshold_adjust"].get(threshold_name, 0.0))
    adjust["threshold_adjust"][threshold_name] = round(old_value + float(delta), 2)


def derive_history_adjustment_from_features(
    feature_row: pd.Series,
    symptom_type: str,
    reaction_mode: str,
    display_name: str,
) -> Dict[str, Any]:
    """
    根据历史粮画像生成对目标画像的微调。

    - problem：把历史粮中高压力/低保护结构作为“需要避开”的方向。
    - good：把历史粮中低压力/高保护结构作为“可以保留”的稳定方向。
    - context_only：不微调，只记录上下文。
    """
    adjust: Dict[str, Any] = {
        "source_product": display_name,
        "reaction_mode": reaction_mode,
        "weight_adjust": {},
        "threshold_adjust": {},
        "notes": [],
    }

    if reaction_mode == "context_only":
        adjust["notes"].append(f"历史粮「{display_name}」仅作为背景记录，未参与目标画像微调。")
        return adjust

    def val(feature: str, default: float = 50.0) -> float:
        return feature_value(feature_row, feature, default=default)

    pressure_features = [
        ("protein_pressure", "蛋白压力"),
        ("carb_pressure", "碳水负担"),
        ("fat_pressure", "脂肪负担"),
    ]
    protective_features = [
        ("protein_quality", "蛋白质量"),
        ("fiber_buffer", "纤维缓冲"),
        ("p_buffer", "肠道缓冲"),
        ("q_scfa", "菌群代谢支持"),
        ("skin_protection", "皮肤保护"),
        ("antioxidant_score", "抗氧化保护"),
        ("fat_regulation_score", "脂肪调节支持"),
    ]

    if reaction_mode == "problem":
        adjust["notes"].append(f"历史粮「{display_name}」被视为不适/疑似触发当前问题的参考粮，系统会尽量避开相似压力结构。")
        for feature, label in pressure_features:
            value = val(feature)
            if value >= 70:
                _add_weight_adjust(adjust, feature, -0.08, f"历史粮{label}偏高（{value:.1f}），提高对{label}的规避权重。")
                max_threshold, _ = FEATURE_THRESHOLD_MAP.get(feature, (None, None))
                _add_threshold_adjust(adjust, max_threshold, -6)

        for feature, label in protective_features:
            value = val(feature)
            if value <= 40:
                _add_weight_adjust(adjust, feature, 0.07, f"历史粮{label}偏弱（{value:.1f}），提高对{label}的正向要求。")
                _, min_threshold = FEATURE_THRESHOLD_MAP.get(feature, (None, None))
                _add_threshold_adjust(adjust, min_threshold, 5)

        q_feed = val("q_feed")
        q_scfa = val("q_scfa")
        q_excess = val("q_feed_excess_penalty", default=0)
        if q_feed >= 75 and q_scfa <= 55:
            _add_weight_adjust(adjust, "q_feed_excess_penalty", -0.14, f"历史粮供菌底物高但菌群代谢支持不足（q_feed={q_feed:.1f}, q_scfa={q_scfa:.1f}），提高供菌过量惩罚。")
            _add_weight_adjust(adjust, "q_scfa", 0.06, "提高菌群代谢支持要求。")
            _add_threshold_adjust(adjust, "q_feed_max", -8)
            _add_threshold_adjust(adjust, "q_scfa_min", 5)
        elif q_excess >= 20:
            _add_weight_adjust(adjust, "q_feed_excess_penalty", -0.10, f"历史粮供菌过量惩罚较高（{q_excess:.1f}），提高发酵压力规避。")

        # 按主问题追加更有针对性的微调
        if symptom_type == "soft_stool":
            if val("p_buffer") <= 45:
                _add_weight_adjust(adjust, "p_buffer", 0.06, "软便场景下历史粮肠道缓冲不足，进一步提高 p_buffer 要求。")
            if val("carb_pressure") >= 65:
                _add_weight_adjust(adjust, "carb_pressure", -0.06, "软便场景下历史粮碳水负担偏高，进一步提高碳水规避。")
        elif symptom_type == "black_chin":
            if val("fat_pressure") >= 60:
                _add_weight_adjust(adjust, "fat_pressure", -0.08, "黑下巴场景下历史粮脂肪负担偏高，进一步降低脂肪压力容忍度。")
            if val("skin_protection") <= 50:
                _add_weight_adjust(adjust, "skin_protection", 0.08, "黑下巴场景下历史粮皮肤保护不足，进一步提高皮肤保护要求。")
        elif symptom_type == "tear_stain":
            if val("antioxidant_score") <= 50:
                _add_weight_adjust(adjust, "antioxidant_score", 0.08, "泪痕场景下历史粮抗氧化保护不足，进一步提高抗氧化要求。")
        elif symptom_type == "vomit":
            if val("protein_pressure") >= 60:
                _add_weight_adjust(adjust, "protein_pressure", -0.08, "呕吐场景下历史粮蛋白压力偏高，进一步规避复杂蛋白结构。")
            if val("fat_pressure") >= 60:
                _add_weight_adjust(adjust, "fat_pressure", -0.08, "呕吐场景下历史粮脂肪负担偏高，进一步规避高脂压力。")
        elif symptom_type == "weight_loss":
            if val("fat_pressure") >= 60:
                _add_weight_adjust(adjust, "fat_pressure", -0.08, "减肥场景下历史粮脂肪负担偏高，进一步控制脂肪压力。")
            if val("carb_pressure") >= 60:
                _add_weight_adjust(adjust, "carb_pressure", -0.08, "减肥场景下历史粮碳水负担偏高，进一步控制碳水压力。")
        elif symptom_type == "weight_gain":
            if val("protein_quality") <= 55:
                _add_weight_adjust(adjust, "protein_quality", 0.08, "增重场景下历史粮蛋白质量不足，进一步提高蛋白质量要求。")
            if val("protein_pressure") >= 65:
                _add_weight_adjust(adjust, "protein_pressure", -0.06, "增重场景下历史粮蛋白压力偏高，避免用过重蛋白结构增重。")

    elif reaction_mode == "good":
        adjust["notes"].append(f"历史粮「{display_name}」被视为适应良好参考粮，系统会优先保留其相对稳定的结构特征。")
        for feature, label in pressure_features:
            value = val(feature)
            if value <= 45:
                _add_weight_adjust(adjust, feature, -0.04, f"历史粮{label}较低（{value:.1f}）且适应良好，轻微倾向保留低{label}结构。")
                max_threshold, _ = FEATURE_THRESHOLD_MAP.get(feature, (None, None))
                _add_threshold_adjust(adjust, max_threshold, -3)

        for feature, label in protective_features:
            value = val(feature)
            if value >= 60:
                _add_weight_adjust(adjust, feature, 0.04, f"历史粮{label}较好（{value:.1f}）且适应良好，轻微提高该支持项权重。")
                _, min_threshold = FEATURE_THRESHOLD_MAP.get(feature, (None, None))
                _add_threshold_adjust(adjust, min_threshold, 3)

        q_feed = val("q_feed")
        q_scfa = val("q_scfa")
        if q_feed <= 70 and q_scfa >= 50:
            _add_weight_adjust(adjust, "q_scfa", 0.03, "历史粮供菌与代谢支持较平衡，轻微保留 q_scfa 支持方向。")
            _add_weight_adjust(adjust, "q_feed_excess_penalty", -0.04, "继续规避供菌过量失衡。")

    return adjust


def build_history_food_context(
    engine,
    history_food_names: List[str],
    symptom_type: str,
    reaction_mode: str,
) -> Dict[str, Any]:
    """查询历史粮画像，并生成用于微调目标画像的汇总上下文。"""
    product_col = CONFIG_TABLES["product_name_col"]
    brand_col = CONFIG_TABLES["brand_name_col"]

    items: List[Dict[str, Any]] = []
    combined_adjust: Dict[str, Any] = {"weight_adjust": {}, "threshold_adjust": {}, "notes": []}
    found_product_names: List[str] = []

    for query_name in history_food_names:
        score_row = query_score_row_by_product_name(engine, query_name)
        if score_row is None:
            items.append({
                "query_name": query_name,
                "found": False,
                "message": "未在产品池中找到该历史粮。",
            })
            continue

        feature_df = normalize_single_score_row(score_row)
        feature_row = feature_df.iloc[0]
        display_name = str(score_row.get(product_col) or query_name)
        brand_name = str(score_row.get(brand_col) or "")
        found_product_names.append(display_name)

        black_row = query_risk_row(engine, display_name, CONFIG_TABLES["black_chin_score_model_like"])
        stool_row = query_risk_row(engine, display_name, CONFIG_TABLES["soft_stool_score_model_like"])

        adjustment = derive_history_adjustment_from_features(
            feature_row=feature_row,
            symptom_type=symptom_type,
            reaction_mode=reaction_mode,
            display_name=display_name,
        )

        for feature, delta in adjustment.get("weight_adjust", {}).items():
            old_value = float(combined_adjust["weight_adjust"].get(feature, 0.0))
            combined_adjust["weight_adjust"][feature] = round(old_value + float(delta), 4)
        for threshold_name, delta in adjustment.get("threshold_adjust", {}).items():
            old_value = float(combined_adjust["threshold_adjust"].get(threshold_name, 0.0))
            combined_adjust["threshold_adjust"][threshold_name] = round(old_value + float(delta), 2)
        combined_adjust["notes"].extend(adjustment.get("notes", []))

        items.append({
            "query_name": query_name,
            "found": True,
            "product_name": display_name,
            "brand_name": brand_name,
            "reaction_mode": reaction_mode,
            "features": product_feature_snapshot(feature_row),
            "black_chin_risk_level": safe_get(black_row, CONFIG_TABLES["risk_level_col"], "暂无"),
            "black_chin_tags": merge_tag_cols(black_row, CONFIG_TABLES["tag_cols"]),
            "soft_stool_risk_level": safe_get(stool_row, CONFIG_TABLES["risk_level_col"], "暂无"),
            "soft_stool_tags": merge_tag_cols(stool_row, CONFIG_TABLES["tag_cols"]),
            "adjustment": adjustment,
        })

    combined_adjust["notes"] = list(dict.fromkeys(combined_adjust["notes"]))

    return make_json_safe({
        "reaction_mode": reaction_mode,
        "reaction_label": HISTORY_REACTION_MODES.get(reaction_mode, reaction_mode),
        "history_food_names": history_food_names,
        "found_product_names": found_product_names,
        "items": items,
        "combined_adjustment": combined_adjust,
    })


def apply_history_adjustments_to_profiles(
    adjusted_profiles: List[Dict[str, Any]],
    history_context: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把历史粮生成的微调叠加到已由体质信号微调后的目标画像上。"""
    if not history_context:
        return adjusted_profiles

    combined = history_context.get("combined_adjustment") or {}
    weight_adjust = combined.get("weight_adjust") or {}
    threshold_adjust = combined.get("threshold_adjust") or {}
    notes = combined.get("notes") or []

    if not weight_adjust and not threshold_adjust and not notes:
        return adjusted_profiles

    result = []
    for profile in adjusted_profiles:
        p = copy.deepcopy(profile)
        p.setdefault("weights", {})
        p.setdefault("thresholds", {})
        p.setdefault("adjustment_notes", [])

        for feature, delta in weight_adjust.items():
            old_value = float(p["weights"].get(feature, 0.0))
            p["weights"][feature] = round(old_value + float(delta), 4)

        # 历史粮阈值微调：如果画像原本没有该阈值，也允许新增一个轻量阈值，便于推荐时产生约束。
        for threshold_name, delta in threshold_adjust.items():
            old_value = p["thresholds"].get(threshold_name)
            if old_value is None:
                # 给常见阈值一个基础值，再叠加 delta。
                if threshold_name.endswith("_max"):
                    old_value = 70
                elif threshold_name.endswith("_min"):
                    old_value = 45
                else:
                    old_value = 50
            p["thresholds"][threshold_name] = max(0, min(100, round(float(old_value) + float(delta), 2)))

        for note in notes:
            p["adjustment_notes"].append(f"历史粮参考：{note}")
        p["history_food_adjustment"] = {
            "reaction_label": history_context.get("reaction_label"),
            "weight_adjust": weight_adjust,
            "threshold_adjust": threshold_adjust,
        }
        result.append(p)

    return result


# =========================================================
# 6. 产品池读取与特征构建
# =========================================================

def load_product_pool(engine) -> pd.DataFrame:
    table = CONFIG_TABLES["score_table"]
    table_cols = get_table_columns(engine, table)

    product_col = CONFIG_TABLES["product_name_col"]
    brand_col = CONFIG_TABLES["brand_name_col"]
    if product_col not in table_cols:
        raise ValueError(f"产品 score 表缺少字段：{product_col}")

    if "formula_id" in table_cols and "source_id" in table_cols:
        selected_exprs = [
            f"fi.product_name AS {quote_identifier(product_col)}",
            "s.formula_id",
        ]
        if brand_col in table_cols:
            selected_exprs.append(f"fi.brand AS {quote_identifier(brand_col)}")
        selected_exprs.extend(
            f"s.{quote_identifier(col)}"
            for col in SCORE_COLS
            if col in table_cols
        )
        sql = text(f"""
            SELECT {", ".join(selected_exprs)}
            FROM {quote_identifier(table)} s
            JOIN csv_labeling.catfood_formula_feature_input fi
              ON fi.formula_id = s.formula_id
             AND fi.source_id = s.source_id
            WHERE fi.is_current = 1
        """)
    else:
        selected_cols = [product_col]
        if "formula_id" in table_cols:
            selected_cols.append("formula_id")
        if brand_col in table_cols:
            selected_cols.append(brand_col)
        selected_cols.extend([col for col in SCORE_COLS if col in table_cols])
        sql = text(
            f"SELECT {', '.join(quote_identifier(c) for c in selected_cols)} "
            f"FROM {quote_identifier(table)}"
        )
    df = pd.read_sql(sql, engine)

    for col in SCORE_COLS:
        if col in df.columns:
            df[col] = df[col].apply(lambda x, field=col: normalize_score(x, field))

    return df


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()

    def col(name: str) -> pd.Series:
        if name in result.columns:
            return result[name]
        return pd.Series([None] * len(result), index=result.index, dtype="float")

    result["protein_quality"] = col("protein_quality_score")
    result["protein_pressure"] = col("protein_score").combine_first(col("protein_structure_score"))
    result["carb_pressure"] = col("carb_score").combine_first(col("starch_burden_score"))
    result["fat_pressure"] = col("fat_score")
    result["p_buffer"] = col("p_buffer")
    result["p_total_score"] = col("p_total_score")
    result["prebiotic_score"] = col("prebiotic_score")
    result["q_feed"] = col("q_feed")
    result["q_scfa"] = col("q_scfa")
    result["antioxidant_score"] = col("antioxidant_score")
    result["fat_regulation_score"] = col("fat_regulation_score")

    result["fiber_buffer"] = result.apply(
        lambda r: weighted_avg_valid([
            (r.get("fiber_score"), 0.30),
            (r.get("p_total_score"), 0.25),
            (r.get("p_buffer"), 0.45),
        ]),
        axis=1,
    )

    result["skin_protection"] = result.apply(
        lambda r: weighted_avg_valid([
            (r.get("antioxidant_score"), 0.55),
            (r.get("fat_regulation_score"), 0.45),
        ]),
        axis=1,
    )

    result["microbiome_support"] = result.apply(
        lambda r: weighted_avg_valid([
            (r.get("prebiotic_score"), 0.35),
            (r.get("q_scfa"), 0.45),
            (r.get("q_feed"), 0.20),
        ]),
        axis=1,
    )

    def q_feed_excess(row: pd.Series) -> Optional[float]:
        q_feed = row.get("q_feed")
        q_scfa = row.get("q_scfa")
        if q_feed is None or q_scfa is None or pd.isna(q_feed) or pd.isna(q_scfa):
            return None
        # q_feed 高且 q_scfa 跟不上时惩罚更明显
        return round(max(0.0, float(q_feed) - float(q_scfa)) * float(q_feed) / 100.0, 2)

    result["q_feed_excess_penalty"] = result.apply(q_feed_excess, axis=1)

    return result


def build_risk_relative_position(row: Optional[pd.Series]) -> str:
    percentile = safe_get(row, CONFIG_TABLES["percentile_col"], None)
    rank = safe_get(row, CONFIG_TABLES["rank_col"], None)
    try:
        pct = float(percentile)
    except Exception:
        pct = None

    if pct is not None:
        if pct >= 0.70:
            return "相对位置：风险靠前"
        if pct >= 0.35:
            return "相对位置：中间区间"
        return "相对位置：低于多数产品"

    try:
        return f"相对位置：排名第 {int(rank)} 位"
    except Exception:
        return "相对位置：暂无"


def query_risk_row(
    engine,
    product_name: str,
    model_like: str,
    formula_id: Any = None,
) -> Optional[pd.Series]:
    table = CONFIG_TABLES["risk_table"]
    product_col = CONFIG_TABLES["risk_product_name_col"]
    columns = get_table_columns(engine, table)
    if product_col not in columns:
        return None

    order_sql = ""
    if "calculated_at" in columns:
        order_sql = "ORDER BY calculated_at DESC"
    elif "created_at" in columns:
        order_sql = "ORDER BY created_at DESC"

    identity_clause = f"{quote_identifier(product_col)} = :product_name"
    params = {"product_name": product_name, "model_like": model_like}
    if "formula_id" in columns and formula_id is not None and not pd.isna(formula_id):
        identity_clause = "formula_id = :formula_id"
        params["formula_id"] = int(formula_id)
    sql = text(f"""
        SELECT *
        FROM {quote_identifier(table)}
        WHERE {identity_clause}
          AND score_model_version LIKE :model_like
        {order_sql}
        LIMIT 1
    """)
    df = pd.read_sql(sql, engine, params=params)
    if df.empty:
        return None
    return df.iloc[0]


def enrich_top_products_with_risk(engine, rec_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    product_col = CONFIG_TABLES["product_name_col"]
    tag_cols = CONFIG_TABLES["tag_cols"]

    for _, row in rec_df.iterrows():
        product_name = row[product_col]
        formula_id = row.get("formula_id")
        black_row = query_risk_row(
            engine,
            product_name,
            CONFIG_TABLES["black_chin_score_model_like"],
            formula_id=formula_id,
        )
        stool_row = query_risk_row(
            engine,
            product_name,
            CONFIG_TABLES["soft_stool_score_model_like"],
            formula_id=formula_id,
        )

        item = row.to_dict()
        item["black_chin_risk_level"] = safe_get(black_row, CONFIG_TABLES["risk_level_col"], "暂无")
        item["black_chin_position"] = build_risk_relative_position(black_row)
        item["black_chin_tags"] = merge_tag_cols(black_row, tag_cols)
        item["soft_stool_risk_level"] = safe_get(stool_row, CONFIG_TABLES["risk_level_col"], "暂无")
        item["soft_stool_position"] = build_risk_relative_position(stool_row)
        item["soft_stool_tags"] = merge_tag_cols(stool_row, tag_cols)
        rows.append(item)

    return pd.DataFrame(rows)


# =========================================================
# 7. fit_score 计算、阈值与 avoid_rules
# =========================================================

def feature_value(row: pd.Series, feature: str, default: float = 50.0) -> float:
    value = row.get(feature)
    if value is None or pd.isna(value):
        return default
    return float(max(0, min(100, value)))


def feature_utility(row: pd.Series, feature: str, weight: float) -> float:
    value = feature_value(row, feature)
    if feature == "q_feed_excess_penalty":
        return max(0.0, 100.0 - value)
    if weight >= 0:
        return value
    return max(0.0, 100.0 - value)


def evaluate_avoid_rules(row: pd.Series, avoid_rules: List[str]) -> Tuple[float, List[str]]:
    penalty = 0.0
    reasons: List[str] = []

    protein_pressure = feature_value(row, "protein_pressure")
    carb_pressure = feature_value(row, "carb_pressure")
    fat_pressure = feature_value(row, "fat_pressure")
    fiber_buffer = feature_value(row, "fiber_buffer")
    p_buffer = feature_value(row, "p_buffer")
    q_feed = feature_value(row, "q_feed")
    q_scfa = feature_value(row, "q_scfa")
    skin_protection = feature_value(row, "skin_protection")
    antioxidant = feature_value(row, "antioxidant_score")
    fat_regulation = feature_value(row, "fat_regulation_score")
    protein_quality = feature_value(row, "protein_quality")

    for rule in avoid_rules or []:
        if rule == "protein_pressure_high" and protein_pressure >= 75:
            penalty += 8; reasons.append("蛋白压力偏高")
        elif rule == "carb_pressure_high" and carb_pressure >= 75:
            penalty += 8; reasons.append("碳水负担偏高")
        elif rule == "fat_pressure_high" and fat_pressure >= 75:
            penalty += 8; reasons.append("脂肪负担偏高")
        elif rule == "fat_pressure_extreme_high" and fat_pressure >= 85:
            penalty += 12; reasons.append("脂肪负担过高")
        elif rule == "p_buffer_low" and p_buffer <= 35:
            penalty += 8; reasons.append("肠道缓冲偏弱")
        elif rule == "fiber_buffer_low" and fiber_buffer <= 35:
            penalty += 8; reasons.append("纤维缓冲偏弱")
        elif rule == "q_feed_high" and q_feed >= 75:
            penalty += 6; reasons.append("供菌底物偏高")
        elif rule == "q_feed_high_q_scfa_low" and q_feed >= 75 and q_scfa <= 55:
            penalty += 12; reasons.append("供菌底物偏高但菌群代谢支持不足")
        elif rule == "skin_protection_low" and skin_protection <= 35:
            penalty += 8; reasons.append("皮肤保护偏弱")
        elif rule == "antioxidant_low" and antioxidant <= 35:
            penalty += 8; reasons.append("抗氧化保护偏弱")
        elif rule == "fat_regulation_low" and fat_regulation <= 35:
            penalty += 8; reasons.append("脂肪调节支持偏弱")
        elif rule == "protein_quality_low" and protein_quality <= 35:
            penalty += 8; reasons.append("蛋白质量支持偏弱")

    # 去重
    reasons = list(dict.fromkeys(reasons))
    return penalty, reasons


def evaluate_thresholds(row: pd.Series, thresholds: Dict[str, Any]) -> Tuple[float, List[str]]:
    penalty = 0.0
    reasons: List[str] = []

    def check_max(feature: str, threshold_name: str, label: str):
        nonlocal penalty
        max_value = thresholds.get(threshold_name)
        if max_value is None:
            return
        value = feature_value(row, feature)
        if value > float(max_value):
            over = min(15, (value - float(max_value)) / 3)
            penalty += over
            reasons.append(f"{label}超过目标上限")

    def check_min(feature: str, threshold_name: str, label: str):
        nonlocal penalty
        min_value = thresholds.get(threshold_name)
        if min_value is None:
            return
        value = feature_value(row, feature)
        if value < float(min_value):
            under = min(15, (float(min_value) - value) / 3)
            penalty += under
            reasons.append(f"{label}低于目标下限")

    check_max("protein_pressure", "protein_pressure_max", "蛋白压力")
    check_max("carb_pressure", "carb_pressure_max", "碳水负担")
    check_max("fat_pressure", "fat_pressure_max", "脂肪负担")
    check_max("q_feed", "q_feed_max", "供菌底物")

    check_min("protein_quality", "protein_quality_min", "蛋白质量")
    check_min("p_buffer", "p_buffer_min", "肠道缓冲")
    check_min("fiber_buffer", "fiber_buffer_min", "纤维缓冲")
    check_min("q_scfa", "q_scfa_min", "菌群代谢支持")
    check_min("skin_protection", "skin_protection_min", "皮肤保护")
    check_min("antioxidant_score", "antioxidant_score_min", "抗氧化保护")
    check_min("fat_regulation_score", "fat_regulation_score_min", "脂肪调节支持")

    reasons = list(dict.fromkeys(reasons))
    return round(float(penalty), 2), reasons


def score_product_for_profile(row: pd.Series, profile: Dict[str, Any]) -> Dict[str, Any]:
    weights: Dict[str, float] = profile.get("weights", {}) or {}
    thresholds: Dict[str, Any] = profile.get("thresholds", {}) or {}
    avoid_rules: List[str] = profile.get("avoid_rules", []) or []

    if not weights:
        return {
            "fit_score": 0,
            "base_score": 0,
            "penalty": 0,
            "strengths": [],
            "cautions": ["画像权重为空，无法计算推荐分。"],
            "feature_contrib": {},
        }

    weighted_sum = 0.0
    weight_total = 0.0
    feature_contrib: Dict[str, float] = {}

    for feature, weight in weights.items():
        weight = float(weight)
        if weight == 0:
            continue
        util = feature_utility(row, feature, weight)
        weighted_sum += abs(weight) * util
        weight_total += abs(weight)
        feature_contrib[feature] = round(abs(weight) * util, 2)

    base_score = weighted_sum / weight_total if weight_total > 0 else 0

    avoid_penalty, avoid_reasons = evaluate_avoid_rules(row, avoid_rules)
    threshold_penalty, threshold_reasons = evaluate_thresholds(row, thresholds)
    total_penalty = avoid_penalty + threshold_penalty

    fit_score = round(max(0.0, min(100.0, base_score - total_penalty)), 2)

    strengths = build_strengths(row, weights)
    cautions = list(dict.fromkeys(avoid_reasons + threshold_reasons))

    return {
        "fit_score": fit_score,
        "base_score": round(base_score, 2),
        "penalty": round(total_penalty, 2),
        "strengths": strengths,
        "cautions": cautions,
        "feature_contrib": feature_contrib,
    }


def build_strengths(row: pd.Series, weights: Dict[str, float]) -> List[str]:
    strengths = []
    for feature, weight in sorted(weights.items(), key=lambda kv: abs(float(kv[1])), reverse=True):
        if len(strengths) >= 5:
            break
        value = feature_value(row, feature)
        display = FEATURE_DISPLAY.get(feature, feature)
        weight = float(weight)
        if feature == "q_feed_excess_penalty":
            if value <= 20:
                strengths.append("供菌底物与菌群代谢相对更平衡")
            continue
        if weight > 0 and value >= 60:
            strengths.append(f"{display}较好")
        elif weight < 0 and value <= 45:
            strengths.append(f"{display}较低，压力相对更轻")
    return list(dict.fromkeys(strengths))


def recommend_products(
    product_pool: pd.DataFrame,
    adjusted_profiles: List[Dict[str, Any]],
    top_n: int = 10,
) -> pd.DataFrame:
    if product_pool.empty or not adjusted_profiles:
        return pd.DataFrame()

    product_col = CONFIG_TABLES["product_name_col"]
    brand_col = CONFIG_TABLES["brand_name_col"]

    profile_candidate_rows: Dict[str, List[Dict[str, Any]]] = {}
    profile_order: Dict[str, int] = {}
    for idx, profile in enumerate(adjusted_profiles):
        code = str(profile.get("profile_code") or f"profile_{idx}")
        profile_candidate_rows[code] = []
        profile_order[code] = idx

    for _, product_row in product_pool.iterrows():
        profile_results = []
        for profile in adjusted_profiles:
            result = score_product_for_profile(product_row, profile)
            result["profile_code"] = profile.get("profile_code")
            result["profile_name"] = profile.get("profile_name")
            profile_results.append(result)

        if not profile_results:
            continue

        avg_fit = round(sum(x["fit_score"] for x in profile_results) / len(profile_results), 2)

        # 按目标画像分别保留候选，避免全局最高分画像挤占其他画像的推荐位。
        for profile_result in profile_results:
            profile_code = str(profile_result.get("profile_code") or "")
            row = {
                product_col: product_row.get(product_col),
                "brand_name": product_row.get(brand_col, ""),
                "fit_score": profile_result["fit_score"],
                "avg_fit_score": avg_fit,
                "matched_profile_code": profile_result["profile_code"],
                "matched_profile_name": profile_result["profile_name"],
                "base_score": profile_result["base_score"],
                "penalty": profile_result["penalty"],
                "strengths": profile_result["strengths"],
                "cautions": profile_result["cautions"],
                "profile_results": profile_results,
                "_profile_order": profile_order.get(profile_code, 999),
            }

            # 展示关键特征
            for feature in [
                "protein_quality", "protein_pressure", "carb_pressure", "fat_pressure",
                "fiber_buffer", "p_buffer", "q_feed", "q_scfa", "skin_protection",
                "q_feed_excess_penalty",
            ]:
                row[feature] = product_row.get(feature)

            profile_candidate_rows.setdefault(profile_code, []).append(row)

    profile_count = max(1, len(adjusted_profiles))
    total_limit = max(profile_count, int(top_n))
    base_quota = max(1, total_limit // profile_count)
    remainder = total_limit % profile_count
    selected_rows: List[Dict[str, Any]] = []

    for idx, profile in enumerate(adjusted_profiles):
        profile_code = str(profile.get("profile_code") or f"profile_{idx}")
        quota = base_quota + (1 if idx < remainder else 0)
        candidates = sorted(
            profile_candidate_rows.get(profile_code, []),
            key=lambda item: (item["fit_score"], item["avg_fit_score"]),
            reverse=True,
        )
        for profile_rank, row in enumerate(candidates[:quota], start=1):
            row = dict(row)
            row["profile_recommend_rank"] = profile_rank
            selected_rows.append(row)

    rec_df = pd.DataFrame(selected_rows)
    if rec_df.empty:
        return rec_df

    rec_df = rec_df.sort_values(
        ["_profile_order", "profile_recommend_rank", "fit_score", "avg_fit_score"],
        ascending=[True, True, False, False],
    ).drop(columns=["_profile_order"]).reset_index(drop=True)
    rec_df.insert(0, "recommend_rank", range(1, len(rec_df) + 1))
    return rec_df


# =========================================================
# 8. 通义千问解释推荐原因
# =========================================================

def get_qwen_client() -> OpenAI:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("未检测到环境变量 DASHSCOPE_API_KEY。请先配置阿里云百炼 API Key。")
    return OpenAI(api_key=api_key, base_url=QWEN_CONFIG["base_url"])


def build_llm_context(
    symptom_type: str,
    user_signals: List[str],
    adjusted_profiles: List[Dict[str, Any]],
    rec_df: pd.DataFrame,
    history_food_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    top_products = []
    product_col = CONFIG_TABLES["product_name_col"]

    for _, row in rec_df.head(8).iterrows():
        top_products.append({
            "rank": int(row.get("recommend_rank")),
            "product_name": row.get(product_col),
            "brand_name": row.get("brand_name"),
            "fit_score": row.get("fit_score"),
            "matched_profile_name": row.get("matched_profile_name"),
            "strengths": row.get("strengths"),
            "cautions": row.get("cautions"),
            "key_features": {
                "protein_quality": row.get("protein_quality"),
                "protein_pressure": row.get("protein_pressure"),
                "carb_pressure": row.get("carb_pressure"),
                "fat_pressure": row.get("fat_pressure"),
                "fiber_buffer": row.get("fiber_buffer"),
                "p_buffer": row.get("p_buffer"),
                "q_feed": row.get("q_feed"),
                "q_scfa": row.get("q_scfa"),
                "skin_protection": row.get("skin_protection"),
            },
            "risk_info": {
                "black_chin_risk_level": row.get("black_chin_risk_level"),
                "black_chin_position": row.get("black_chin_position"),
                "black_chin_tags": row.get("black_chin_tags"),
                "soft_stool_risk_level": row.get("soft_stool_risk_level"),
                "soft_stool_position": row.get("soft_stool_position"),
                "soft_stool_tags": row.get("soft_stool_tags"),
            },
        })

    return make_json_safe({
        "symptom_type": symptom_type,
        "symptom_label": SYMPTOM_LABELS.get(symptom_type, symptom_type),
        "user_signals": user_signals,
        "history_food_context": history_food_context or {},
        "adjusted_target_profiles": adjusted_profiles,
        "top_products": top_products,
        "important_instruction": [
            "推荐排序由规则模型和产品池 score 计算得出，大模型不得重新排序。",
            "大模型只负责解释推荐原因、适合场景和需要观察点。",
            "不要编造成分、品牌背景、疾病诊断或临床结论。",
            "不要使用绝对表达，例如一定会改善、一定不会软便。",
            "需要解释为什么目标画像这样设定，以及为什么这些产品更接近目标画像。",
            "如果提供了历史粮参考，需要说明历史粮如何影响目标画像微调；如果历史粮仅作为背景，则不要夸大其影响。",
        ],
    })


def generate_qwen_recommendation_explanation(context: dict) -> str:
    client = get_qwen_client()

    system_prompt = """
你是“宠析”的猫粮推荐解释助手。你的任务是把规则推荐引擎输出的目标画像、适配分和候选产品，翻译成普通养猫用户能理解的推荐解释。

你必须遵守：
1. 推荐排序已经由规则模型完成，不要重新排序。
2. 只能基于输入的目标画像、产品特征、适配理由和风险标签进行解释，不要编造成分或品牌背景。
3. 不要给医学诊断，不要承诺疗效。
4. 使用克制表达：更适合、相对更稳、需要观察、不建议作为唯一依据。
5. 输出不要使用 Markdown 表格。
6. 如果输入中包含历史粮参考，需要单独说明：历史粮是作为“不适参考”“适应良好参考”还是“仅背景”，以及它如何改变目标画像；不要把历史粮影响说成绝对因果。

输出结构固定为：

### 推荐目标画像说明
解释这次推荐为什么关注这些指标，例如 p_buffer、q_scfa、蛋白压力、碳水负担等。

### 历史粮参考如何影响推荐
如果没有历史粮或历史粮未命中产品池，就简短说明暂无历史粮微调；如果有历史粮，则解释它对规避项/保留项的影响。

### 推荐产品整体逻辑
概括 Top 产品共同特点，以及它们为什么更接近目标画像。

### Top 产品解读
逐个解释前 3-5 个产品：推荐理由 + 需要观察点。

### 使用提醒
列出 3-5 条喂养观察建议，语气克制。
"""

    user_prompt = f"""
下面是推荐引擎输出的结构化数据，请生成页面展示用的推荐解释。

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
# 9. Streamlit 页面
# =========================================================

def signal_options_for_symptom(signal_rules: Dict[str, Dict[str, Any]], symptom_type: str) -> Dict[str, str]:
    """
    第一版简单展示所有信号。后续你可以在 signal_rule 表增加 symptom_type 字段做过滤。
    """
    options = {}
    for code, rule in signal_rules.items():
        label = rule.get("label", code)
        options[code] = label
    return options


def inject_recommendation_styles() -> None:
    st.markdown(
        """
        <style>
          .stApp {
            background: #f5f8ff;
            color: #0f172a;
          }
          [data-testid="stSidebar"] {
            display: none;
          }
          .block-container {
            max-width: 1840px;
            padding: 26px 20px 42px;
            padding-bottom: 40px;
          }
          .cx-header {
            margin-bottom: 14px;
          }
          .cx-title {
            font-size: 28px;
            line-height: 1.2;
            font-weight: 700;
            color: #020617;
            margin: 0;
          }
          .cx-subtitle {
            margin-top: 6px;
            color: #64748b;
            font-size: 14px;
            font-weight: 500;
          }
          [data-testid="stVerticalBlockBorderWrapper"] {
            border: 1px solid #dbe3ef !important;
            border-radius: 24px !important;
            background: #fff;
            box-shadow: 0 10px 24px rgba(15, 23, 42, .04);
          }
          .cx-module-title {
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 2px 0 14px;
          }
          .cx-marker {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 28px;
            height: 24px;
            padding: 0 8px;
            border-radius: 8px;
            background: #0f172a;
            color: #fff;
            font-size: 12px;
            font-weight: 700;
          }
          .cx-module-heading {
            color: #0f172a;
            font-size: 16px;
            font-weight: 650;
          }
          .cx-module-hint {
            color: #94a3b8;
            font-size: 12px;
            font-weight: 500;
          }
          .cx-help {
            margin-top: -10px;
            margin-bottom: 12px;
            color: #64748b;
            font-size: 13px;
          }
          .cx-food-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 11px 14px;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            background: #fff;
            margin-bottom: 8px;
          }
          .cx-food-left {
            display: flex;
            align-items: center;
            min-width: 0;
            gap: 14px;
          }
          .cx-food-tag {
            min-width: 84px;
            border-radius: 999px;
            background: #eff6ff;
            color: #2563eb;
            padding: 5px 12px;
            text-align: center;
            font-size: 13px;
            font-weight: 650;
          }
          .cx-food-tag.history {
            background: #f1f5f9;
            color: #64748b;
          }
          .cx-food-name {
            color: #0f172a;
            font-size: 14px;
            font-weight: 650;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          }
          .cx-badges {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 4px;
          }
          .cx-badge {
            display: inline-flex;
            align-items: center;
            border-radius: 8px;
            background: #eff6ff;
            color: #2563eb;
            padding: 7px 12px;
            font-size: 13px;
            font-weight: 650;
          }
          .stSelectbox label,
          .stMultiSelect label,
          .stTextInput label,
          .stTextArea label {
            color: #0f172a !important;
            font-size: 14px !important;
            font-weight: 600 !important;
          }
          div[data-baseweb="select"] > div,
          div[data-testid="stTextInput"] input,
          div[data-testid="stTextArea"] textarea {
            border-radius: 12px !important;
            border-color: #dbe3ef !important;
            background: #fff !important;
          }
          div[data-testid="stButton"] > button[kind="primary"],
          div[data-testid="stButton"] > button[kind="secondary"] {
            border-radius: 12px;
            font-weight: 700;
          }
          div[data-testid="stButton"] > button[kind="primary"] {
            background: #2563eb;
            border-color: #2563eb;
            box-shadow: 0 12px 24px rgba(37, 99, 235, .22);
          }
          div[data-testid="stPills"] button {
            border-radius: 999px !important;
            min-height: 36px;
            font-size: 13px;
            font-weight: 600;
            border-color: #dbe3ef !important;
            background: #fff !important;
          }
          div[data-testid="stPills"] button[aria-pressed="true"],
          div[data-testid="stPills"] button[kind="primary"] {
            background: #2563eb !important;
            border-color: #2563eb !important;
            color: #fff !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_module_title(marker: str, title: str, hint: str = "") -> None:
    hint_html = f'<span class="cx-module-hint">{hint}</span>' if hint else ""
    st.markdown(
        f"""
        <div class="cx-module-title">
          <span class="cx-marker">{marker}</span>
          <span class="cx-module-heading">{title}</span>
          {hint_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=600, show_spinner=False)
def load_product_label_options() -> List[str]:
    try:
        engine = get_engine()
        table = CONFIG_TABLES["score_table"]
        product_col = CONFIG_TABLES["product_name_col"]
        brand_col = CONFIG_TABLES["brand_name_col"]
        columns = get_table_columns(engine, table)
        if product_col not in columns:
            return []
        select_cols = [quote_identifier(product_col)]
        if brand_col in columns:
            select_cols.insert(0, quote_identifier(brand_col))
        sql = text(
            f"""
            SELECT DISTINCT {', '.join(select_cols)}
            FROM {quote_identifier(table)}
            WHERE {quote_identifier(product_col)} IS NOT NULL
              AND {quote_identifier(product_col)} <> ''
            ORDER BY {quote_identifier(brand_col) if brand_col in columns else quote_identifier(product_col)},
                     {quote_identifier(product_col)}
            LIMIT 300
            """
        )
        df = pd.read_sql(sql, engine)
        labels = []
        for _, row in df.iterrows():
            brand = str(row.get(brand_col) or "").strip() if brand_col in df.columns else ""
            product = str(row.get(product_col) or "").strip()
            label = f"{brand} {product}".strip()
            if label:
                labels.append(label)
        return labels
    except Exception:
        return []


def infer_symptom_type_from_ui(long_term: List[str], current_observations: List[str]) -> str:
    joined = " ".join([*(long_term or []), *(current_observations or [])])
    for symptom, keywords in SYMPTOM_TRIGGER_RULES:
        if any(keyword in joined for keyword in keywords):
            return symptom
    return "black_chin"


def resolve_signal_codes(signal_rules: Dict[str, Dict[str, Any]], selected_labels: List[str]) -> List[str]:
    selected_codes: List[str] = []
    selected_text = " ".join(selected_labels or [])
    for code, rule in signal_rules.items():
        label = str(rule.get("label") or code)
        if label in selected_labels or any(part and part in label for part in selected_labels) or any(part and part in selected_text for part in [label]):
            selected_codes.append(code)
    return list(dict.fromkeys(selected_codes))


def render_food_feedback_rows(current_food: str, history_foods: List[str]) -> None:
    rows = []
    if current_food:
        rows.append(("当前在吃", current_food, ""))
    for food in history_foods[:3]:
        rows.append(("历史", food, "history"))
    if not rows:
        rows = [("当前在吃", "暂未选择", ""), ("历史", "可添加吃过的粮", "history")]

    for tag, name, extra_class in rows:
        st.markdown(
            f"""
            <div class="cx-food-row">
              <div class="cx-food-left">
                <span class="cx-food-tag {extra_class}">{tag}</span>
                <span class="cx-food-name">{name}</span>
              </div>
              <span style="color:#94a3b8;font-weight:700;">×</span>
            </div>
            """,
            unsafe_allow_html=True,
        )



def render_history_food_context(history_context: Optional[Dict[str, Any]]):
    """展示历史粮命中、关键画像和本次微调方向。"""
    if not history_context or not history_context.get("history_food_names"):
        return

    st.markdown("### 历史粮参考")
    st.caption(f"历史粮反馈模式：{history_context.get('reaction_label', '暂无')}")

    items = history_context.get("items") or []
    if not items:
        st.info("未提供可用历史粮信息。")
        return

    overview_rows = []
    for item in items:
        if not item.get("found"):
            overview_rows.append({
                "输入名称": item.get("query_name"),
                "匹配产品": "未命中",
                "品牌": "",
                "蛋白压力": None,
                "碳水负担": None,
                "脂肪负担": None,
                "肠道缓冲": None,
                "q_feed": None,
                "q_scfa": None,
                "皮肤保护": None,
                "说明": item.get("message", "未命中"),
            })
            continue
        features = item.get("features") or {}
        overview_rows.append({
            "输入名称": item.get("query_name"),
            "匹配产品": item.get("product_name"),
            "品牌": item.get("brand_name"),
            "蛋白压力": features.get("protein_pressure"),
            "碳水负担": features.get("carb_pressure"),
            "脂肪负担": features.get("fat_pressure"),
            "肠道缓冲": features.get("p_buffer"),
            "q_feed": features.get("q_feed"),
            "q_scfa": features.get("q_scfa"),
            "皮肤保护": features.get("skin_protection"),
            "说明": "已参与微调" if history_context.get("reaction_mode") != "context_only" else "仅背景",
        })

    st.dataframe(pd.DataFrame(overview_rows), use_container_width=True, hide_index=True)

    notes = (history_context.get("combined_adjustment") or {}).get("notes") or []
    if notes:
        with st.expander("查看历史粮触发的画像微调记录"):
            for note in notes:
                st.markdown(f"- {note}")

def render_profile_cards(adjusted_profiles: List[Dict[str, Any]]):
    st.markdown("### 微调后的推荐目标画像")
    if not adjusted_profiles:
        st.info("暂无可用目标画像。")
        return

    for profile in adjusted_profiles:
        with st.expander(f"{profile.get('profile_name')}｜选择分 {profile.get('_selection_score', '-')}"):
            st.markdown(f"**机制：** {profile.get('mechanism', '')}")
            st.markdown("**目标方向：**")
            target_rows = [
                {"指标": FEATURE_DISPLAY.get(k, k), "目标": v}
                for k, v in (profile.get("target") or {}).items()
            ]
            st.dataframe(pd.DataFrame(target_rows), use_container_width=True, hide_index=True)

            st.markdown("**主要权重：**")
            weight_rows = [
                {"指标": FEATURE_DISPLAY.get(k, k), "权重": v}
                for k, v in sorted((profile.get("weights") or {}).items(), key=lambda kv: abs(float(kv[1])), reverse=True)
            ]
            st.dataframe(pd.DataFrame(weight_rows), use_container_width=True, hide_index=True)

            notes = profile.get("adjustment_notes") or []
            if notes:
                st.markdown("**微调记录：**")
                for note in notes:
                    st.markdown(f"- {note}")


def render_recommendation_table(rec_df: pd.DataFrame):
    if rec_df.empty:
        st.info("暂无推荐结果。")
        return

    product_col = CONFIG_TABLES["product_name_col"]
    display_df = rec_df[[
        "recommend_rank",
        "profile_recommend_rank",
        product_col,
        "brand_name",
        "fit_score",
        "matched_profile_name",
        "strengths",
        "cautions",
        "protein_quality",
        "protein_pressure",
        "carb_pressure",
        "fat_pressure",
        "fiber_buffer",
        "p_buffer",
        "q_feed",
        "q_scfa",
        "skin_protection",
    ]].copy()

    display_df = display_df.rename(columns={
        "recommend_rank": "排名",
        "profile_recommend_rank": "画像内排名",
        product_col: "产品名称",
        "brand_name": "品牌",
        "fit_score": "适配分",
        "matched_profile_name": "匹配画像",
        "strengths": "推荐理由",
        "cautions": "观察点",
        "protein_quality": "蛋白质量",
        "protein_pressure": "蛋白压力",
        "carb_pressure": "碳水负担",
        "fat_pressure": "脂肪负担",
        "fiber_buffer": "纤维缓冲",
        "p_buffer": "肠道缓冲",
        "q_feed": "供菌底物",
        "q_scfa": "菌群代谢支持",
        "skin_protection": "皮肤保护",
    })

    st.dataframe(display_df, use_container_width=True, hide_index=True)


def render_page():
    st.set_page_config(page_title="宠析 - 猫咪体质推荐", layout="wide")
    inject_recommendation_styles()
    st.markdown(
        """
        <div class="cx-header">
          <h1 class="cx-title">猫粮智能推荐</h1>
          <div class="cx-subtitle">结合猫咪年龄、吃粮反馈、长期问题和当前观察，生成更适合的主粮推荐。</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        engine = get_engine()
        profiles = load_recommendation_profiles(engine)
        signal_rules = load_signal_rules(engine)
    except Exception as e:
        st.error(f"读取画像池或微调规则失败：{e}")
        return

    if not profiles:
        st.warning(f"画像池为空，请检查 {CONFIG_TABLES['profile_table']} 表。")
        return
    if not signal_rules:
        st.warning(f"用户信号规则为空，请检查 {CONFIG_TABLES['signal_rule_table']} 表。")
        return

    product_options = load_product_label_options()
    product_select_options = product_options or [""]
    default_current_index = 0
    for index, label in enumerate(product_select_options):
        if "GO" in label and "美毛" in label:
            default_current_index = index
            break

    with st.container(border=True):
        left_col, right_col = st.columns([1.02, 1.55], gap="large")

        with left_col:
            render_module_title("A", "基本信息")
            age_col1, age_col2 = st.columns([1, 2.2])
            with age_col1:
                st.markdown("猫龄")
            with age_col2:
                cat_age = st.selectbox("猫龄", CAT_AGE_OPTIONS, index=2, label_visibility="collapsed")

            food_col1, food_col2 = st.columns([1, 2.2])
            with food_col1:
                st.markdown("当前在吃的粮")
            with food_col2:
                current_food = st.selectbox(
                    "当前在吃的粮",
                    product_select_options,
                    index=default_current_index if product_options else None,
                    placeholder="选择当前在吃的粮",
                    accept_new_options=True,
                    label_visibility="collapsed",
                )

            st.markdown(
                """
                <div class="cx-badges">
                  <span class="cx-badge">品牌：GO</span>
                  <span class="cx-badge">进口品牌</span>
                  <span class="cx-badge">70-80元/斤</span>
                  <span class="cx-badge">美毛毛发</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.divider()

            render_module_title("B", "过往吃粮反馈", "记录猫咪吃过的粮，帮助更准确推荐")
            default_history = [
                option for option in product_options
                if any(keyword in option for keyword in ["皇家 BK34", "百利 高蛋白", "爱肯拿 牧场盛宴"])
            ][:3]
            history_foods = st.multiselect(
                "历史吃过的粮",
                product_options,
                default=default_history,
                placeholder="选择历史吃过的粮",
                accept_new_options=True,
                label_visibility="collapsed",
            )
            render_food_feedback_rows(current_food, history_foods)

            custom_history_food = st.text_input(
                "添加吃过的粮",
                placeholder="+ 添加吃过的粮",
                label_visibility="collapsed",
            )

        with right_col:
            render_module_title("C", "长期问题 / 体质困扰", "可多选")
            st.markdown('<div class="cx-help">选择猫咪长期存在或反复出现的问题</div>', unsafe_allow_html=True)
            long_term_problems = st.pills(
                "长期问题 / 体质困扰",
                LONG_TERM_PROBLEM_OPTIONS,
                default=["黑下巴反复", "肠胃敏感"],
                selection_mode="multi",
                label_visibility="collapsed",
            )

            st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
            render_module_title("D", "当前重点观察", "可多选")
            st.markdown('<div class="cx-help">选择最近最注意的点，协助更精准推荐关注的表现</div>', unsafe_allow_html=True)
            current_observations = st.pills(
                "当前重点观察",
                CURRENT_OBSERVATION_OPTIONS,
                default=["特别/黑下巴", "软便", "掉食"],
                selection_mode="multi",
                label_visibility="collapsed",
            )

            st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
            render_module_title("E", "采购偏好设置", "可选")
            pref_box = st.container(border=True)
            with pref_box:
                p1, p2 = st.columns([1, 5])
                with p1:
                    st.markdown("产地偏好：")
                with p2:
                    origin_pref = st.pills("产地偏好", ORIGIN_PREF_OPTIONS, default="不限", label_visibility="collapsed")

                p3, p4 = st.columns([1, 5])
                with p3:
                    st.markdown("价格带：")
                with p4:
                    price_pref = st.pills("价格带", PRICE_PREF_OPTIONS, default="50-80元/斤", label_visibility="collapsed")

                p5, p6 = st.columns([1, 5])
                with p5:
                    st.markdown("功能倾向：")
                with p6:
                    function_pref = st.pills("功能倾向", FUNCTION_PREF_OPTIONS, default="黑下巴友好", label_visibility="collapsed")

            btn_col1, btn_col2 = st.columns([4, 1.45])
            with btn_col2:
                if st.button("✧  生成推荐", type="primary", use_container_width=True):
                    st.session_state["run_recommendation"] = True

    if not st.session_state.get("run_recommendation"):
        st.info("填写基础信息、体质困扰和当前观察后，点击“生成推荐”。")
        return

    selected_labels = [
        *(long_term_problems or []),
        *(current_observations or []),
        *([function_pref] if function_pref and function_pref != "不限" else []),
        *([origin_pref] if origin_pref and origin_pref != "不限" else []),
        *([price_pref] if price_pref and price_pref != "不限" else []),
        cat_age,
    ]
    symptom_type = infer_symptom_type_from_ui(long_term_problems or [], current_observations or [])
    selected_signals = resolve_signal_codes(signal_rules, selected_labels)
    if not selected_signals:
        signal_options = signal_options_for_symptom(signal_rules, symptom_type)
        selected_signals = list(signal_options.keys())[:2]
    top_n_profiles = 3
    top_n_products = 10
    history_food_names = [
        name for name in [current_food, *(history_foods or []), custom_history_food]
        if str(name or "").strip()
    ]
    history_reaction_mode = "problem"
    exclude_history_foods = True

    adjusted_profiles = build_adjusted_profiles_for_case(
        profiles=profiles,
        signal_rules=signal_rules,
        symptom_type=symptom_type,
        user_signals=selected_signals,
        top_n=int(top_n_profiles),
    )

    history_food_context = None
    if history_food_names:
        with st.spinner("正在读取历史粮画像并微调推荐目标..."):
            history_food_context = build_history_food_context(
                engine=engine,
                history_food_names=history_food_names,
                symptom_type=symptom_type,
                reaction_mode=history_reaction_mode,
            )
            adjusted_profiles = apply_history_adjustments_to_profiles(
                adjusted_profiles=adjusted_profiles,
                history_context=history_food_context,
            )

    st.markdown("---")
    st.markdown("## 2. 推荐目标画像")
    render_history_food_context(history_food_context)
    render_profile_cards(adjusted_profiles)

    st.markdown("---")
    st.markdown("## 3. 产品池匹配结果")
    with st.spinner("正在计算产品池适配分..."):
        try:
            product_pool = add_derived_features(load_product_pool(engine))
            if history_food_context and exclude_history_foods:
                found_history_names = set(history_food_context.get("found_product_names") or [])
                product_col = CONFIG_TABLES["product_name_col"]
                if found_history_names and product_col in product_pool.columns:
                    product_pool = product_pool[~product_pool[product_col].astype(str).isin(found_history_names)].copy()
            rec_df = recommend_products(product_pool, adjusted_profiles, top_n=int(top_n_products))
            rec_df = enrich_top_products_with_risk(engine, rec_df)
        except Exception as e:
            st.error(f"产品池推荐计算失败：{e}")
            return

    render_recommendation_table(rec_df)

    with st.expander("查看推荐结果完整 JSON"):
        st.json(make_json_safe(rec_df.to_dict(orient="records")))

    st.markdown("---")
    st.markdown("## 4. 通义千问推荐解释")
    llm_context = build_llm_context(
        symptom_type=symptom_type,
        user_signals=selected_signals,
        adjusted_profiles=adjusted_profiles,
        rec_df=rec_df,
        history_food_context=history_food_context,
    )
    input_hash = calc_input_hash(llm_context)
    st.caption(f"当前推荐输入指纹：`{input_hash[:12]}`")

    if st.button("生成通义千问推荐解释"):
        with st.spinner("正在生成推荐解释..."):
            try:
                text = generate_qwen_recommendation_explanation(llm_context)
                st.session_state[f"recommendation_explain_{input_hash}"] = text
            except Exception as e:
                st.error(f"通义千问解释生成失败：{e}")

    cached_text = st.session_state.get(f"recommendation_explain_{input_hash}")
    if cached_text:
        st.markdown(cached_text)
    else:
        st.info("点击按钮后，通义千问会基于目标画像、适配分和候选产品生成推荐解释。")


if __name__ == "__main__":
    render_page()
