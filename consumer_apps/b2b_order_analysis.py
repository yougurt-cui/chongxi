# -*- coding: utf-8 -*-
"""
宠析 B端接单分析脚本

实现功能：
1. 产品检索：按目标 SKU 或筛选条件检索相似产品
2. 相似产品问题聚类：聚合同类 SKU 的用户反馈问题
3. 工艺翻车点优先级排序：根据反馈问题 + 工艺结构标签推导翻车点
4. 质量结果指标清单：生成需要重点验证的品控指标
5. 生成厂家接单话术

依赖表：
- sku_b2b_tag_result
- catfood_sku_label_wide
- sku_similarity_result
- sku_symptom_compound_risk_wide
- sku_risk_score_result

输出表：
- b2b_order_analysis_result
"""

import json
import os
import uuid
import pymysql
from typing import Any, Dict, List, Optional

import sku_similarity_1 as similarity_recall


# =========================
# 1. 数据库配置
# =========================

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "protein_feature_platform"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


# =========================
# 2. 表配置
# =========================

B2B_TAG_TABLE = "sku_b2b_tag_result"
LABEL_WIDE_TABLE = os.getenv("B2B_LABEL_WIDE_TABLE", "catfood_sku_label_wide")
SIMILARITY_TABLE = "sku_similarity_result"
SYMPTOM_RISK_TABLE = "sku_symptom_compound_risk_wide"
RISK_SCORE_TABLE = "sku_risk_score_result"
PROCESS_FEATURE_TABLE = os.getenv("B2B_PROCESS_FEATURE_TABLE", "sku_process_feature_profile")
OUTPUT_TABLE = "b2b_order_analysis_result"


# =========================
# 3. 参数配置
# =========================

DEFAULT_TOP_N = 20
DEFAULT_MIN_SIMILARITY = 0.50
SIMILARITY_WEIGHTS = {
    "process_structure": 0.35,
    "ingredient": 0.275,
    "nutrition": 0.275,
    "risk_reason": 0.10,
}


def adjusted_similarity_sql(alias: str = "s") -> str:
    return f"""
        (
            {SIMILARITY_WEIGHTS["process_structure"]} * {alias}.process_structure_similarity
            + {SIMILARITY_WEIGHTS["ingredient"]} * {alias}.ingredient_similarity
            + {SIMILARITY_WEIGHTS["nutrition"]} * {alias}.nutrition_similarity
            + {SIMILARITY_WEIGHTS["risk_reason"]} * {alias}.risk_reason_similarity
        ) * GREATEST(0.75, {alias}.confidence_factor)
    """


# =========================
# 4. 工具函数
# =========================

def safe_json_loads(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []

        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
            if isinstance(parsed, dict):
                result = []
                for key in ("tags", "main_reason_tags", "support_reason_tags", "fat_detail_tags", "all_reason_tags"):
                    tags = parsed.get(key)
                    if isinstance(tags, list):
                        result.extend(str(x).strip() for x in tags if str(x).strip())
                return result or [str(parsed)]
            return [str(parsed)]
        except json.JSONDecodeError:
            normalized = (
                value.replace("，", ",")
                .replace("、", ",")
                .replace(";", ",")
                .replace("；", ",")
            )
            return [x.strip().strip('"').strip("'") for x in normalized.split(",") if x.strip()]

    return [str(value).strip()]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def clamp_rate(value: Any) -> float:
    number = safe_float(value)
    if number > 1:
        number = number / 100
    return min(1.0, max(0.0, number))


def get_similarity_level(score: float) -> str:
    if score >= 0.80:
        return "高度相似"
    if score >= 0.65:
        return "较相似"
    if score >= 0.50:
        return "中等相似"
    if score >= 0.35:
        return "低相似"
    return "不相似"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def safe_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def safe_json_value(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def add_score(score_map: Dict[str, float], key: str, value: float):
    if not key:
        return
    score_map[key] = round(score_map.get(key, 0.0) + value, 4)


def normalize_score_map(score_map: Dict[str, float]) -> List[Dict[str, Any]]:
    if not score_map:
        return []

    max_score = max(score_map.values()) if score_map.values() else 1.0

    result = []
    for key, score in score_map.items():
        priority_score = score / max_score if max_score else 0.0
        if priority_score >= 0.75:
            priority = "P1"
        elif priority_score >= 0.45:
            priority = "P2"
        else:
            priority = "P3"

        result.append({
            "name": key,
            "raw_score": round(score, 4),
            "priority_score": round(priority_score, 4),
            "priority": priority,
        })

    result.sort(key=lambda x: x["priority_score"], reverse=True)
    return result


def has_any(tags: List[str], keywords: List[str]) -> bool:
    joined = " ".join(tags)
    return any(k in joined for k in keywords)


# =========================
# 5. 规则库：反馈问题 → 工艺翻车点
# =========================

FEEDBACK_TO_FAILURE_RULES = {
    "软便": [
        {
            "failure_point": "淀粉糊化与熟化不足风险",
            "weight": 0.35,
            "quality_indicators": ["淀粉糊化度", "颗粒硬度", "水分", "膨化度"],
            "reason": "软便反馈可能与豆薯淀粉熟化、颗粒结构和水分控制相关。"
        },
        {
            "failure_point": "高脂肪消化负担与喷涂压力",
            "weight": 0.25,
            "quality_indicators": ["粗脂肪实测", "表面残油", "酸价", "过氧化值"],
            "reason": "高脂结构可能放大肠胃波动，需要验证脂肪实测和油脂稳定。"
        },
        {
            "failure_point": "发酵底物与成形纤维平衡不足",
            "weight": 0.25,
            "quality_indicators": ["粉化率", "颗粒完整度", "便便试喂反馈"],
            "reason": "益生元或豆类底物较强时，需要确认成形纤维和颗粒稳定性。"
        }
    ],
    "拉稀": [
        {
            "failure_point": "淀粉糊化与熟化不足风险",
            "weight": 0.40,
            "quality_indicators": ["淀粉糊化度", "水分", "颗粒硬度"],
            "reason": "拉稀反馈需要优先验证淀粉熟化和颗粒结构。"
        }
    ],
    "呕吐": [
        {
            "failure_point": "颗粒硬度与熟化一致性风险",
            "weight": 0.35,
            "quality_indicators": ["颗粒硬度", "膨化度", "颗粒密度", "熟化一致性"],
            "reason": "呕吐反馈可能与颗粒过硬、熟化不均或高蛋白高脂结构有关。"
        },
        {
            "failure_point": "多原料混合与熟化一致性不足",
            "weight": 0.25,
            "quality_indicators": ["混合均匀性", "颗粒均匀度", "批次营养波动"],
            "reason": "多肉源或多原料结构需要关注熟化一致性。"
        }
    ],
    "黑下巴": [
        {
            "failure_point": "后喷涂表面残油控制风险",
            "weight": 0.45,
            "quality_indicators": ["表面残油", "粗脂肪实测", "油脂分布均匀性"],
            "reason": "黑下巴相关反馈需要优先排查表油、油脂分布和喷涂稳定。"
        },
        {
            "failure_point": "油脂氧化与抗氧化保护不足",
            "weight": 0.30,
            "quality_indicators": ["酸价", "过氧化值", "气味稳定性"],
            "reason": "高脂或功能油脂配方需要验证氧化控制。"
        }
    ],
    "油腻": [
        {
            "failure_point": "后喷涂表面残油控制风险",
            "weight": 0.50,
            "quality_indicators": ["表面残油", "粗脂肪实测", "油脂分布均匀性"],
            "reason": "油腻反馈直接指向喷涂均匀性和表面残油。"
        }
    ],
    "碗油": [
        {
            "failure_point": "后喷涂表面残油控制风险",
            "weight": 0.50,
            "quality_indicators": ["表面残油", "粗脂肪实测", "油脂分布均匀性"],
            "reason": "碗油反馈通常需要验证表面残油和喷涂控制。"
        }
    ],
    "哈喇味": [
        {
            "failure_point": "油脂氧化与抗氧化保护不足",
            "weight": 0.50,
            "quality_indicators": ["酸价", "过氧化值", "气味稳定性"],
            "reason": "哈喇味需要优先验证油脂氧化指标。"
        }
    ],
    "粉多": [
        {
            "failure_point": "高肉/高纤维结构下粉化控制不足",
            "weight": 0.45,
            "quality_indicators": ["粉化率", "碎粒率", "颗粒完整度"],
            "reason": "粉多反馈需要验证颗粒成型、干燥和运输后粉化。"
        }
    ],
    "碎渣": [
        {
            "failure_point": "颗粒成型与干燥曲线不稳定",
            "weight": 0.40,
            "quality_indicators": ["碎粒率", "粉化率", "颗粒硬度"],
            "reason": "碎渣反馈需要关注成型、干燥和颗粒强度。"
        }
    ],
    "太硬": [
        {
            "failure_point": "颗粒硬度控制风险",
            "weight": 0.45,
            "quality_indicators": ["颗粒硬度", "膨化度", "颗粒密度"],
            "reason": "太硬反馈需要优先验证颗粒硬度和膨化状态。"
        }
    ],
    "不吃": [
        {
            "failure_point": "适口性与喷涂均匀性波动",
            "weight": 0.35,
            "quality_indicators": ["适口性测试", "喷涂均匀性", "气味稳定性"],
            "reason": "不吃反馈需要同时验证适口性、喷涂均匀性和氧化气味。"
        },
        {
            "failure_point": "颗粒口感与硬度控制风险",
            "weight": 0.20,
            "quality_indicators": ["颗粒硬度", "粉化率", "粒径分布"],
            "reason": "颗粒硬度、粉化和大小也会影响采食。"
        }
    ],
    "换批不吃": [
        {
            "failure_point": "批次一致性控制不足",
            "weight": 0.50,
            "quality_indicators": ["多批次粗脂肪波动", "多批次水分波动", "多批次硬度波动", "气味稳定性"],
            "reason": "换批不吃需要优先验证批次间营养、喷涂、气味和颗粒状态稳定性。"
        }
    ]
}


# =========================
# 6. 规则库：工艺结构标签 → 工艺翻车点
# =========================

PROCESS_TAG_TO_FAILURE_RULES = {
    "高肉低淀粉成型关注型": [
        {
            "failure_point": "高肉低淀粉成型稳定风险",
            "weight": 0.45,
            "quality_indicators": ["膨化度", "颗粒硬度", "粉化率", "颗粒密度", "成型率"],
            "reason": "高肉低淀粉结构会提高膨化成型和硬度控制难度。"
        }
    ],
    "高蛋白成型关注型": [
        {
            "failure_point": "高蛋白结构下颗粒硬度与粉化风险",
            "weight": 0.35,
            "quality_indicators": ["颗粒硬度", "粉化率", "膨化度"],
            "reason": "高蛋白配方需要验证颗粒成型和口感稳定。"
        }
    ],
    "高脂高喷涂关注型": [
        {
            "failure_point": "后喷涂表面残油控制风险",
            "weight": 0.50,
            "quality_indicators": ["表面残油", "粗脂肪实测", "油脂分布均匀性", "酸价", "过氧化值"],
            "reason": "高脂高适口结构需要重点关注后喷涂、表油和油脂稳定。"
        }
    ],
    "油脂氧化与喷涂保护关注型": [
        {
            "failure_point": "油脂氧化与抗氧化保护不足",
            "weight": 0.45,
            "quality_indicators": ["酸价", "过氧化值", "气味稳定性"],
            "reason": "功能油脂或高脂配方需要重点验证氧化控制。"
        }
    ],
    "豆类淀粉糊化关注型": [
        {
            "failure_point": "豆类淀粉糊化与熟化稳定风险",
            "weight": 0.40,
            "quality_indicators": ["淀粉糊化度", "颗粒硬度", "水分", "膨化度"],
            "reason": "豆类结构需要关注糊化和熟化稳定。"
        }
    ],
    "薯类淀粉糊化关注型": [
        {
            "failure_point": "薯类淀粉糊化与成型风险",
            "weight": 0.35,
            "quality_indicators": ["淀粉糊化度", "膨化度", "颗粒硬度", "水分"],
            "reason": "薯类/木薯结构需要验证糊化、膨化和干燥稳定。"
        }
    ],
    "高纤维成型与粉化关注型": [
        {
            "failure_point": "高纤维结构下粉化与适口性风险",
            "weight": 0.45,
            "quality_indicators": ["粉化率", "碎粒率", "颗粒完整度", "颗粒硬度", "适口性测试"],
            "reason": "高纤维结构容易影响成型、粉化和口感。"
        }
    ],
    "多原料混合与熟化一致性关注型": [
        {
            "failure_point": "多原料混合与熟化一致性不足",
            "weight": 0.35,
            "quality_indicators": ["混合均匀性", "颗粒均匀度", "批次营养波动", "熟化一致性"],
            "reason": "多肉源/多原料结构需要关注混合与熟化一致性。"
        }
    ],
    "清线与交叉污染关注型": [
        {
            "failure_point": "低敏/单一蛋白清线与交叉污染风险",
            "weight": 0.40,
            "quality_indicators": ["清线记录", "残留检测", "批次追溯", "原料隔离记录"],
            "reason": "单一蛋白/低敏方向需要证明清线和交叉污染控制。"
        }
    ],
    "功能添加稳定性关注型": [
        {
            "failure_point": "功能添加物稳定性与均匀性风险",
            "weight": 0.35,
            "quality_indicators": ["功能成分实测", "混合均匀性", "活性稳定性", "批次波动"],
            "reason": "益生菌、抗氧化物等功能添加需要验证添加时机和稳定性。"
        }
    ]
}


# =========================
# 7. 数据读取函数
# =========================

def label_wide_select_sql(prefix: str = "w") -> str:
    return f"""
            {prefix}.source_id,
            {prefix}.product_key,
            {prefix}.brand,
            {prefix}.product_name,
            {prefix}.ingredient_composition AS ingredient_text,
            {prefix}.guarantee_crude_protein_value AS crude_protein,
            {prefix}.guarantee_crude_fat_value AS crude_fat,
            {prefix}.guarantee_crude_fiber_value AS crude_fiber,
            {prefix}.guarantee_moisture_value AS moisture,
            {prefix}.guarantee_crude_ash_value AS ash,
            {prefix}.carb_score AS estimated_carb,
            {prefix}.fat_reason_tags,
            {prefix}.risk_types,
            {prefix}.main_reason_tags,
            {prefix}.support_reason_tags,
            {prefix}.fat_detail_tags,
            {prefix}.all_reason_tags,
            {prefix}.black_chin_risk_level,
            {prefix}.soft_stool_risk_level
    """


def process_feature_select_sql(prefix: str = "pf") -> str:
    return f"""
            {prefix}.main_process_tags AS pf_main_process_tags,
            {prefix}.process_structure_summary AS pf_process_structure_summary,
            {prefix}.candidate_process_watch_tags AS pf_candidate_process_watch_tags,
            {prefix}.candidate_quality_result_tags AS pf_candidate_quality_result_tags,
            {prefix}.candidate_feedback_risk_tags AS pf_candidate_feedback_risk_tags,
            {prefix}.main_process_modules AS pf_main_process_modules,
            {prefix}.process_tag_details AS pf_process_tag_details
    """


def fetch_target_sku(conn, target_sku_id: str) -> Optional[Dict[str, Any]]:
    sql = f"""
        SELECT
            b.sku_id,
            b.source_id AS b2b_source_id,
            b.brand AS b2b_brand,
            b.product_name AS b2b_product_name,
            b.factory_business_tags,
            b.process_structure_tags,
            b.process_attention_tags,
            b.quality_validation_tags,
            {label_wide_select_sql("w")},
            {process_feature_select_sql("pf")}
        FROM {B2B_TAG_TABLE} b
        LEFT JOIN {LABEL_WIDE_TABLE} w
            ON b.sku_id = w.product_key
        LEFT JOIN {PROCESS_FEATURE_TABLE} pf
            ON b.sku_id = pf.sku_id
        WHERE b.sku_id = %s
        LIMIT 1
    """

    with conn.cursor() as cursor:
        cursor.execute(sql, (target_sku_id,))
        row = cursor.fetchone()

    if not row:
        return None

    return normalize_sku_row(row)


def normalize_sku_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sku_id": row.get("sku_id"),
        "source_id": row.get("source_id") or row.get("b2b_source_id"),
        "brand": row.get("brand") or row.get("b2b_brand") or "",
        "product_name": row.get("product_name") or row.get("b2b_product_name") or "",
        "factory_business_tags": safe_json_loads(row.get("factory_business_tags")),
        "process_structure_tags": safe_json_loads(row.get("process_structure_tags")),
        "process_attention_tags": safe_json_loads(row.get("process_attention_tags")),
        "quality_validation_tags": safe_json_loads(row.get("quality_validation_tags")),
        "risk_reason_tags": list(dict.fromkeys(
            safe_json_loads(row.get("risk_types"))
            + safe_json_loads(row.get("main_reason_tags"))
            + safe_json_loads(row.get("support_reason_tags"))
            + safe_json_loads(row.get("fat_detail_tags"))
            + safe_json_loads(row.get("fat_reason_tags"))
            + safe_json_loads(row.get("all_reason_tags"))
        )),
        "black_chin_risk_level": row.get("black_chin_risk_level") or "",
        "soft_stool_risk_level": row.get("soft_stool_risk_level") or "",
        "ingredient_text": row.get("ingredient_text") or "",
        "crude_protein": safe_float(row.get("crude_protein")),
        "crude_fat": safe_float(row.get("crude_fat")),
        "crude_fiber": safe_float(row.get("crude_fiber")),
        "moisture": safe_float(row.get("moisture")),
        "ash": safe_float(row.get("ash")),
        "estimated_carb": safe_float(row.get("estimated_carb")),
        "main_process_tags": safe_json_loads(row.get("pf_main_process_tags")),
        "process_structure_summary": row.get("pf_process_structure_summary") or "",
        "candidate_process_watch_tags": safe_json_loads(row.get("pf_candidate_process_watch_tags")),
        "candidate_quality_result_tags": safe_json_loads(row.get("pf_candidate_quality_result_tags")),
        "candidate_feedback_risk_tags": safe_json_loads(row.get("pf_candidate_feedback_risk_tags")),
        "main_process_modules": safe_json_value(row.get("pf_main_process_modules"), []),
        "process_tag_details": safe_json_value(row.get("pf_process_tag_details"), []),
    }


def search_products_by_filters(
    conn,
    keyword: Optional[str] = None,
    brand_keyword: Optional[str] = None,
    product_keyword: Optional[str] = None,
    black_chin_risk_levels: Optional[List[str]] = None,
    soft_stool_risk_levels: Optional[List[str]] = None,
    crude_protein_min: Optional[float] = None,
    crude_protein_max: Optional[float] = None,
    crude_fat_min: Optional[float] = None,
    crude_fat_max: Optional[float] = None,
    crude_fiber_min: Optional[float] = None,
    crude_fiber_max: Optional[float] = None,
    carb_score_min: Optional[float] = None,
    carb_score_max: Optional[float] = None,
    required_factory_tags: Optional[List[str]] = None,
    required_process_tags: Optional[List[str]] = None,
    top_n: int = DEFAULT_TOP_N,
) -> List[Dict[str, Any]]:
    """
    功能1：产品检索。
    支持按粗蛋白、粗脂肪、工厂业务标签、工艺结构标签筛选。
    """
    conditions = []
    params = []

    if keyword:
        like = f"%{keyword}%"
        conditions.append("(b.sku_id LIKE %s OR b.brand LIKE %s OR b.product_name LIKE %s)")
        params.extend([like, like, like])

    if brand_keyword:
        conditions.append("b.brand LIKE %s")
        params.append(f"%{brand_keyword}%")

    if product_keyword:
        conditions.append("b.product_name LIKE %s")
        params.append(f"%{product_keyword}%")

    if black_chin_risk_levels:
        placeholders = ",".join(["%s"] * len(black_chin_risk_levels))
        conditions.append(f"w.black_chin_risk_level IN ({placeholders})")
        params.extend(black_chin_risk_levels)

    if soft_stool_risk_levels:
        placeholders = ",".join(["%s"] * len(soft_stool_risk_levels))
        conditions.append(f"w.soft_stool_risk_level IN ({placeholders})")
        params.extend(soft_stool_risk_levels)

    if crude_protein_min is not None:
        conditions.append("w.guarantee_crude_protein_value >= %s")
        params.append(crude_protein_min)

    if crude_protein_max is not None:
        conditions.append("w.guarantee_crude_protein_value <= %s")
        params.append(crude_protein_max)

    if crude_fat_min is not None:
        conditions.append("w.guarantee_crude_fat_value >= %s")
        params.append(crude_fat_min)

    if crude_fat_max is not None:
        conditions.append("w.guarantee_crude_fat_value <= %s")
        params.append(crude_fat_max)

    if crude_fiber_min is not None:
        conditions.append("w.guarantee_crude_fiber_value >= %s")
        params.append(crude_fiber_min)

    if crude_fiber_max is not None:
        conditions.append("w.guarantee_crude_fiber_value <= %s")
        params.append(crude_fiber_max)

    if carb_score_min is not None:
        conditions.append("w.carb_score >= %s")
        params.append(carb_score_min)

    if carb_score_max is not None:
        conditions.append("w.carb_score <= %s")
        params.append(carb_score_max)

    # JSON 字段这里用 LIKE 兼容性最好，后续可以换 JSON_CONTAINS。
    if required_factory_tags:
        for tag in required_factory_tags:
            conditions.append("b.factory_business_tags LIKE %s")
            params.append(f"%{tag}%")

    if required_process_tags:
        for tag in required_process_tags:
            conditions.append("b.process_structure_tags LIKE %s")
            params.append(f"%{tag}%")

    where_sql = " AND ".join(conditions) if conditions else "1=1"

    sql = f"""
        SELECT
            b.sku_id,
            b.source_id AS b2b_source_id,
            b.brand AS b2b_brand,
            b.product_name AS b2b_product_name,
            b.factory_business_tags,
            b.process_structure_tags,
            b.process_attention_tags,
            b.quality_validation_tags,
            {label_wide_select_sql("w")}
        FROM {B2B_TAG_TABLE} b
        LEFT JOIN {LABEL_WIDE_TABLE} w
            ON b.sku_id = w.product_key
        WHERE {where_sql}
        LIMIT %s
    """
    params.append(top_n)

    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    return [normalize_sku_row(row) for row in rows]


def get_similar_products(
    conn,
    target_sku_id: str,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    top_n: int = DEFAULT_TOP_N,
    recall_mode: str = "default",
) -> List[Dict[str, Any]]:
    """
    功能1：如果输入 target_sku_id，则使用三路召回方式实时取相似 SKU。
    """
    engine = similarity_recall.get_engine()
    feature_df = similarity_recall.load_sku_features(engine)
    recall_df = similarity_recall.build_similarity_recommendations(
        df=feature_df,
        target_sku_id=target_sku_id,
        mode=recall_mode,
        top_n=max(top_n, similarity_recall.RECALL_TOP_N),
    )

    if recall_df.empty:
        return []

    recall_df = recall_df[recall_df["final_similarity"] >= float(min_similarity)].copy()
    if recall_df.empty:
        return []

    selected_ids = []
    if recall_mode == "nutrition_structure":
        recall_groups = {
            "营养结构相似": recall_df.sort_values("nutrition_structure_similarity", ascending=False).head(top_n),
        }
    else:
        recall_groups = {
            "综合相似": recall_df.sort_values("final_similarity", ascending=False).head(top_n),
            "配方骨架相似": recall_df.sort_values("ingredient_category_similarity", ascending=False).head(3),
            "营养压力相似": recall_df.sort_values("nutrition_structure_similarity", ascending=False).head(3),
        }
    recall_group_by_sku: Dict[str, List[str]] = {}
    for group_name, group_df in recall_groups.items():
        for _, item in group_df.iterrows():
            candidate_id = str(item["candidate_sku_id"])
            if candidate_id not in selected_ids:
                selected_ids.append(candidate_id)
            recall_group_by_sku.setdefault(candidate_id, [])
            if group_name not in recall_group_by_sku[candidate_id]:
                recall_group_by_sku[candidate_id].append(group_name)

    recall_df = recall_df[recall_df["candidate_sku_id"].astype(str).isin(selected_ids)].copy()

    recall_by_sku = {
        str(row["candidate_sku_id"]): row
        for _, row in recall_df.iterrows()
    }
    candidate_ids = [sku_id for sku_id in selected_ids if sku_id in recall_by_sku]
    candidate_product_names = list(dict.fromkeys(
        str(recall_by_sku[sku_id].get("candidate_product_name") or "").strip()
        for sku_id in candidate_ids
        if str(recall_by_sku[sku_id].get("candidate_product_name") or "").strip()
    ))
    id_placeholders = ",".join(["%s"] * len(candidate_ids))
    product_placeholders = ",".join(["%s"] * len(candidate_product_names))
    candidate_where = f"b.sku_id IN ({id_placeholders})"
    query_params = list(candidate_ids)
    if candidate_product_names:
        candidate_where += f" OR b.product_name IN ({product_placeholders})"
        query_params.extend(candidate_product_names)

    sql = f"""
        SELECT
            b.sku_id AS target_sku_id,
            b.source_id AS b2b_source_id,
            b.brand AS b2b_brand,
            b.product_name AS b2b_product_name,
            b.factory_business_tags,
            b.process_structure_tags,
            b.process_attention_tags,
            b.quality_validation_tags,
            {label_wide_select_sql("w")},
            {process_feature_select_sql("pf")}

        FROM {B2B_TAG_TABLE} b
        LEFT JOIN {LABEL_WIDE_TABLE} w
            ON b.sku_id = w.product_key
        LEFT JOIN {PROCESS_FEATURE_TABLE} pf
            ON b.sku_id = pf.sku_id
        WHERE {candidate_where}
    """

    with conn.cursor() as cursor:
        cursor.execute(sql, query_params)
        rows = cursor.fetchall()

    rows_by_sku = {str(row.get("target_sku_id")): row for row in rows}
    rows_by_normalized_sku = {
        similarity_recall.normalize_sku_id(row.get("target_sku_id")): row
        for row in rows
        if row.get("target_sku_id")
    }
    results = []

    for sku_id in candidate_ids:
        row = rows_by_sku.get(sku_id) or rows_by_normalized_sku.get(
            similarity_recall.normalize_sku_id(sku_id)
        )
        if not row:
            continue

        recall_row = recall_by_sku[sku_id]
        overall_similarity = safe_float(recall_row.get("final_similarity"))
        ingredient_similarity = safe_float(recall_row.get("ingredient_category_similarity"))
        nutrition_similarity = safe_float(recall_row.get("nutrition_structure_similarity"))
        process_similarity = safe_float(recall_row.get("process_structure_similarity"))
        difference_summary = {
            "shared_ingredient_category_tags": recall_row.get("shared_ingredient_features") or [],
            "ingredient_role_similarity_details": recall_row.get("ingredient_role_similarity_details") or [],
            "shared_nutrition_features": recall_row.get("shared_nutrition_features") or [],
            "nutrition_component_similarities": recall_row.get("nutrition_component_similarities") or [],
            "high_nutrition_similarity_parts": recall_row.get("high_nutrition_similarity_parts") or [],
            "shared_process_structure_tags": recall_row.get("shared_process_features") or [],
            "key_differences": recall_row.get("key_differences") or [],
            "observation_points": recall_row.get("observation_points") or [],
            "similarity_pattern": recall_row.get("similarity_pattern") or "",
            "business_interpretation": recall_row.get("business_interpretation") or "",
            "source_only_ingredient_category_tags": [],
            "target_only_ingredient_category_tags": [],
            "source_only_process_structure_tags": [],
            "target_only_process_structure_tags": [],
            "source_only_risk_reason_tags": [],
            "target_only_risk_reason_tags": [],
        }

        results.append({
            "sku_id": row.get("target_sku_id"),
            "brand": row.get("brand") or row.get("b2b_brand") or "",
            "product_name": row.get("product_name") or row.get("b2b_product_name") or "",
            "overall_similarity": round(overall_similarity, 4),
            "process_structure_similarity": process_similarity,
            "ingredient_similarity": ingredient_similarity,
            "nutrition_similarity": nutrition_similarity,
            "factory_business_similarity": 0.0,
            "risk_reason_similarity": 0.0,
            "quality_validation_similarity": 0.0,
            "confidence_factor": 1.0,
            "similarity_level": get_similarity_level(overall_similarity),

            "shared_process_structure_tags": recall_row.get("shared_process_features") or [],
            "shared_factory_business_tags": [],
            "shared_ingredient_category_tags": recall_row.get("shared_ingredient_features") or [],
            "ingredient_role_similarity_details": recall_row.get("ingredient_role_similarity_details") or [],
            "nutrition_component_similarities": recall_row.get("nutrition_component_similarities") or [],
            "high_nutrition_similarity_parts": recall_row.get("high_nutrition_similarity_parts") or [],
            "recall_groups": recall_group_by_sku.get(sku_id, []),
            "difference_summary": difference_summary,

            "factory_business_tags": safe_json_loads(row.get("factory_business_tags")),
            "process_structure_tags": safe_json_loads(row.get("process_structure_tags")),
            "process_attention_tags": safe_json_loads(row.get("process_attention_tags")),
            "quality_validation_tags": safe_json_loads(row.get("quality_validation_tags")),
            "risk_reason_tags": normalize_sku_row(row)["risk_reason_tags"],
            "black_chin_risk_level": row.get("black_chin_risk_level") or "",
            "soft_stool_risk_level": row.get("soft_stool_risk_level") or "",

            "ingredient_text": row.get("ingredient_text") or "",
            "crude_protein": safe_float(row.get("crude_protein")),
            "crude_fat": safe_float(row.get("crude_fat")),
            "crude_fiber": safe_float(row.get("crude_fiber")),
            "moisture": safe_float(row.get("moisture")),
            "ash": safe_float(row.get("ash")),
            "estimated_carb": safe_float(row.get("estimated_carb")),
            "main_process_tags": safe_json_loads(row.get("pf_main_process_tags")),
            "process_structure_summary": row.get("pf_process_structure_summary") or "",
            "candidate_process_watch_tags": safe_json_loads(row.get("pf_candidate_process_watch_tags")),
            "candidate_quality_result_tags": safe_json_loads(row.get("pf_candidate_quality_result_tags")),
            "candidate_feedback_risk_tags": safe_json_loads(row.get("pf_candidate_feedback_risk_tags")),
            "main_process_modules": safe_json_value(row.get("pf_main_process_modules"), []),
            "process_tag_details": safe_json_value(row.get("pf_process_tag_details"), []),
        })

    return results


def fetch_feedback_for_skus(conn, sku_ids: List[str]) -> List[Dict[str, Any]]:
    """
    功能2：获取相似 SKU 的问题信号。
    当前库没有独立 feedback 表，因此用风险宽表和风险原因标签派生近似反馈率。
    """
    if not sku_ids:
        return []

    placeholders = ",".join(["%s"] * len(sku_ids))

    sql = f"""
        SELECT
            w.product_key AS sku_id,
            w.soft_stool_symptom_probability,
            w.soft_stool_soft_stool_risk_index_value,
            w.soft_stool_soft_stool_risk_level,
            w.black_chin_symptom_probability,
            w.black_chin_black_chin_compound_risk_value,
            w.black_chin_black_chin_risk_level,
            lw.risk_types,
            lw.main_reason_tags,
            lw.support_reason_tags,
            lw.fat_detail_tags,
            lw.fat_reason_tags,
            lw.all_reason_tags,
            b.process_attention_tags,
            b.quality_validation_tags
        FROM {SYMPTOM_RISK_TABLE} w
        LEFT JOIN {LABEL_WIDE_TABLE} lw
            ON w.product_key = lw.product_key
        LEFT JOIN {B2B_TAG_TABLE} b
            ON w.product_key = b.sku_id
        WHERE w.product_key IN ({placeholders})
    """

    with conn.cursor() as cursor:
        cursor.execute(sql, sku_ids)
        rows = cursor.fetchall()

    found = {row.get("sku_id") for row in rows}
    missing = [sku_id for sku_id in sku_ids if sku_id not in found]
    if missing:
        placeholders = ",".join(["%s"] * len(missing))
        sql = f"""
            SELECT
                b.sku_id,
                lw.risk_types,
                lw.main_reason_tags,
                lw.support_reason_tags,
                lw.fat_detail_tags,
                lw.fat_reason_tags,
                lw.all_reason_tags,
                lw.black_chin_risk_level AS black_chin_black_chin_risk_level,
                lw.soft_stool_risk_level AS soft_stool_soft_stool_risk_level,
                b.process_attention_tags,
                b.quality_validation_tags
            FROM {B2B_TAG_TABLE} b
            LEFT JOIN {LABEL_WIDE_TABLE} lw
                ON b.sku_id = lw.product_key
            WHERE b.sku_id IN ({placeholders})
        """
        with conn.cursor() as cursor:
            cursor.execute(sql, missing)
            rows.extend(cursor.fetchall())

    return [derive_feedback_row(row) for row in rows]


def level_to_rate(level: Any) -> float:
    text = str(level or "")
    if "极高" in text:
        return 0.85
    if "高" in text:
        return 0.65
    if "中" in text:
        return 0.40
    if "低" in text:
        return 0.15
    return 0.0


def tag_rate(tags: List[str], keywords: List[str], base: float = 0.25) -> float:
    if not tags:
        return 0.0
    joined = " ".join(tags)
    hits = sum(1 for keyword in keywords if keyword in joined)
    if hits <= 0:
        return 0.0
    return min(0.85, base + 0.12 * hits)


def derive_feedback_row(row: Dict[str, Any]) -> Dict[str, Any]:
    tags = list(dict.fromkeys(
        safe_json_loads(row.get("risk_types"))
        + safe_json_loads(row.get("main_reason_tags"))
        + safe_json_loads(row.get("support_reason_tags"))
        + safe_json_loads(row.get("fat_detail_tags"))
        + safe_json_loads(row.get("fat_reason_tags"))
        + safe_json_loads(row.get("all_reason_tags"))
        + safe_json_loads(row.get("process_attention_tags"))
        + safe_json_loads(row.get("quality_validation_tags"))
    ))

    soft_stool_rate = max(
        clamp_rate(row.get("soft_stool_symptom_probability")),
        clamp_rate(row.get("soft_stool_soft_stool_risk_index_value")),
        level_to_rate(row.get("soft_stool_soft_stool_risk_level")),
        tag_rate(tags, ["软便", "拉稀", "肠胃", "消化", "粪便"], 0.20),
    )
    black_chin_rate = max(
        clamp_rate(row.get("black_chin_symptom_probability")),
        clamp_rate(row.get("black_chin_black_chin_compound_risk_value")),
        level_to_rate(row.get("black_chin_black_chin_risk_level")),
        tag_rate(tags, ["黑下巴", "油脂", "表油", "油腻", "氧化"], 0.20),
    )

    oily_rate = max(
        tag_rate(tags, ["油腻", "碗油", "表油", "后喷涂", "高脂", "氧化"], 0.20),
        black_chin_rate * 0.45 if black_chin_rate else 0.0,
    )
    dust_rate = tag_rate(tags, ["粉化", "粉多", "碎渣", "颗粒完整", "成型"], 0.18)
    hard_rate = tag_rate(tags, ["太硬", "硬度", "颗粒密度", "膨化"], 0.18)
    palatability_rate = tag_rate(tags, ["不吃", "适口", "挑食", "气味", "喷涂均匀"], 0.18)
    batch_rate = tag_rate(tags, ["批次", "波动", "一致性", "稳定性"], 0.18)

    return {
        "sku_id": row.get("sku_id"),
        "soft_stool_rate": round(soft_stool_rate, 4),
        "vomiting_rate": tag_rate(tags, ["呕吐", "反胃"], 0.18),
        "black_chin_rate": round(black_chin_rate, 4),
        "tear_stain_rate": tag_rate(tags, ["泪痕", "上火"], 0.18),
        "oily_feedback_rate": round(oily_rate, 4),
        "dust_feedback_rate": round(dust_rate, 4),
        "too_hard_feedback_rate": round(hard_rate, 4),
        "palatability_negative_rate": round(palatability_rate, 4),
        "batch_inconsistency_rate": round(batch_rate, 4),
        "complaint_rate": round(max(soft_stool_rate, black_chin_rate, oily_rate, dust_rate, hard_rate, palatability_rate, batch_rate), 4),
        "derived_tags": tags,
    }


# =========================
# 8. 功能2：相似产品问题聚类
# =========================

def cluster_feedback_problems(feedback_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    把多个相似 SKU 的反馈比例聚合成问题聚类。
    """
    if not feedback_rows:
        return []

    feedback_fields = {
        "软便": "soft_stool_rate",
        "呕吐": "vomiting_rate",
        "黑下巴": "black_chin_rate",
        "泪痕": "tear_stain_rate",
        "油腻/碗油": "oily_feedback_rate",
        "粉多/碎渣": "dust_feedback_rate",
        "太硬": "too_hard_feedback_rate",
        "不吃/适口性差": "palatability_negative_rate",
        "换批不稳定": "batch_inconsistency_rate",
        "客诉": "complaint_rate",
    }

    clusters = []

    for problem_name, field in feedback_fields.items():
        values = []
        affected_sku_count = 0

        for row in feedback_rows:
            value = safe_float(row.get(field), 0.0)
            if value > 0:
                values.append(value)
                affected_sku_count += 1

        if not values:
            continue

        avg_rate = sum(values) / len(values)
        max_rate = max(values)

        clusters.append({
            "problem": problem_name,
            "avg_rate": round(avg_rate, 4),
            "max_rate": round(max_rate, 4),
            "affected_sku_count": affected_sku_count,
            "total_sku_count": len(feedback_rows),
            "coverage": round(affected_sku_count / len(feedback_rows), 4),
        })

    clusters.sort(key=lambda x: (x["avg_rate"], x["coverage"]), reverse=True)
    return clusters


# =========================
# 9. 功能3：工艺翻车点优先级排序
# =========================

def infer_failure_points(
    similar_products: List[Dict[str, Any]],
    problem_clusters: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    根据：
    1. 相似产品的问题聚类
    2. 相似产品的工艺结构标签
    推导工艺翻车点优先级。
    """
    failure_score_map: Dict[str, float] = {}
    failure_reason_map: Dict[str, List[str]] = {}
    failure_quality_map: Dict[str, List[str]] = {}

    # 1. 根据用户反馈问题推导翻车点
    for cluster in problem_clusters:
        problem = cluster["problem"]
        avg_rate = safe_float(cluster.get("avg_rate"), 0.0)
        coverage = safe_float(cluster.get("coverage"), 0.0)

        # 问题强度
        problem_strength = avg_rate * 0.7 + coverage * 0.3

        matched_rules = []

        for key, rules in FEEDBACK_TO_FAILURE_RULES.items():
            if key in problem:
                matched_rules.extend(rules)

        for rule in matched_rules:
            failure_point = rule["failure_point"]
            weight = rule["weight"]
            score = problem_strength * weight * 10

            add_score(failure_score_map, failure_point, score)

            failure_reason_map.setdefault(failure_point, [])
            failure_reason_map[failure_point].append(
                f"相似产品中「{problem}」反馈较集中，{rule['reason']}"
            )

            failure_quality_map.setdefault(failure_point, [])
            for q in rule["quality_indicators"]:
                if q not in failure_quality_map[failure_point]:
                    failure_quality_map[failure_point].append(q)

    # 2. 根据工艺结构标签推导翻车点
    for product in similar_products:
        process_tags = product.get("process_structure_tags", [])
        similarity_weight = safe_float(product.get("overall_similarity"), 0.5)

        for process_tag in process_tags:
            rules = PROCESS_TAG_TO_FAILURE_RULES.get(process_tag, [])
            for rule in rules:
                failure_point = rule["failure_point"]
                score = rule["weight"] * similarity_weight

                add_score(failure_score_map, failure_point, score)

                failure_reason_map.setdefault(failure_point, [])
                failure_reason_map[failure_point].append(
                    f"相似 SKU 命中工艺结构标签「{process_tag}」，{rule['reason']}"
                )

                failure_quality_map.setdefault(failure_point, [])
                for q in rule["quality_indicators"]:
                    if q not in failure_quality_map[failure_point]:
                        failure_quality_map[failure_point].append(q)

    ranked = normalize_score_map(failure_score_map)

    for item in ranked:
        name = item["name"]
        item["reasons"] = list(dict.fromkeys(failure_reason_map.get(name, [])))[:5]
        item["quality_indicators"] = failure_quality_map.get(name, [])

    return ranked


# =========================
# 10. 功能4：质量结果指标清单
# =========================

def build_quality_validation_plan(
    failure_points: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    根据翻车点生成质量验证指标清单。
    """
    indicator_score_map: Dict[str, float] = {}
    indicator_related_failure_map: Dict[str, List[str]] = {}

    for fp in failure_points:
        priority = fp.get("priority")
        raw_score = safe_float(fp.get("raw_score"), 0.0)
        failure_name = fp.get("name")

        if priority == "P1":
            multiplier = 1.5
        elif priority == "P2":
            multiplier = 1.0
        else:
            multiplier = 0.6

        for indicator in fp.get("quality_indicators", []):
            add_score(indicator_score_map, indicator, raw_score * multiplier)
            indicator_related_failure_map.setdefault(indicator, [])
            if failure_name not in indicator_related_failure_map[indicator]:
                indicator_related_failure_map[indicator].append(failure_name)

    ranked_indicators = normalize_score_map(indicator_score_map)

    result = []
    for item in ranked_indicators:
        indicator = item["name"]
        result.append({
            "indicator": indicator,
            "priority": item["priority"],
            "priority_score": item["priority_score"],
            "related_failure_points": indicator_related_failure_map.get(indicator, []),
            "validation_advice": build_validation_advice(indicator),
        })

    return result


def build_validation_advice(indicator: str) -> str:
    advice_map = {
        "表面残油": "建议在高脂/高适口配方打样和中试阶段检测，用于判断表面油腻和喷涂吸附情况。",
        "粗脂肪实测": "建议多批次检测，与营养保证值和喷涂量记录对比，判断喷涂稳定性。",
        "油脂分布均匀性": "建议结合颗粒不同位置取样，判断喷涂是否均匀。",
        "酸价": "用于判断油脂水解和劣变情况，适合高脂、鱼油、功能油脂配方。",
        "过氧化值": "用于判断油脂氧化程度，适合验证哈喇味、气味波动和高脂配方稳定性。",
        "淀粉糊化度": "适合豆薯淀粉或高淀粉结构，用于验证熟化程度。",
        "颗粒硬度": "适合高肉、高蛋白、小颗粒、幼猫粮等，用于验证入口体验和颗粒结构。",
        "粉化率": "适合高肉、高纤维、运输敏感产品，用于验证开袋粉末和颗粒完整度。",
        "碎粒率": "用于判断颗粒强度和运输后破碎风险。",
        "膨化度": "用于验证膨化成型状态，尤其适合高肉低淀粉或高淀粉结构。",
        "颗粒密度": "用于判断颗粒是否过实、过硬，适合高肉高蛋白配方。",
        "成型率": "用于验证目标配方是否能够稳定量产。",
        "水分": "用于验证干燥稳定和储存安全。",
        "水活度": "用于验证储存稳定、微生物风险和返潮风险。",
        "混合均匀性": "适合多原料、益生元、功能添加密集型配方。",
        "熟化一致性": "适合多肉源、多蛋白、高蛋白结构。",
        "适口性测试": "适合高适口、挑食猫、小颗粒、功能添加型产品。",
        "气味稳定性": "适合高脂、油脂喷涂、鱼油和换批不吃风险产品。",
        "多批次粗脂肪波动": "用于证明喷涂和高脂产品的批次一致性。",
        "多批次水分波动": "用于证明干燥稳定性。",
        "多批次硬度波动": "用于证明颗粒口感和批次一致性。",
        "清线记录": "适合单一蛋白、低敏方向，作为低敏卖点可信度证据。",
        "残留检测": "适合单一蛋白/低敏产品，验证交叉污染风险。",
        "批次追溯": "适合所有需要能力证明的 B 端项目。",
        "原料隔离记录": "适合低敏、单一蛋白、高端定制项目。",
    }
    return advice_map.get(indicator, "建议结合产品定位、配方结构和工厂品控能力确定检测频次。")


# =========================
# 11. 功能5：生成厂家接单话术
# =========================

def generate_sales_pitch(
    target_sku: Optional[Dict[str, Any]],
    similar_products: List[Dict[str, Any]],
    problem_clusters: List[Dict[str, Any]],
    failure_points: List[Dict[str, Any]],
    quality_plan: List[Dict[str, Any]],
) -> str:
    """
    生成厂家接单话术。
    """
    if target_sku:
        product_desc = f"目标产品 SKU「{target_sku['sku_id']}」"
        target_tags = target_sku.get("factory_business_tags", [])
        process_tags = target_sku.get("process_structure_tags", [])
    else:
        product_desc = "该品牌需求对应的目标产品"
        target_tags = []
        process_tags = []

    top_problem_names = [x["problem"] for x in problem_clusters[:3]]
    top_failure_names = [x["name"] for x in failure_points[:3]]
    top_quality_names = [x["indicator"] for x in quality_plan[:6]]

    parts = []

    parts.append(
        f"针对{product_desc}，系统检索到 {len(similar_products)} 个相似参考产品。"
    )

    if target_tags:
        parts.append(
            f"从工厂业务语言看，该产品可归入「{'、'.join(target_tags[:6])}」等方向。"
        )

    if process_tags:
        parts.append(
            f"从宠析工艺结构看，该产品涉及「{'、'.join(process_tags[:6])}」等工艺关注类型。"
        )

    if top_problem_names:
        parts.append(
            f"相似产品的用户反馈主要集中在「{'、'.join(top_problem_names)}」。"
        )

    if top_failure_names:
        parts.append(
            f"接单前建议优先排查「{'、'.join(top_failure_names)}」等工艺/质量翻车点。"
        )

    if top_quality_names:
        parts.append(
            f"打样和中试阶段建议重点提供「{'、'.join(top_quality_names)}」等质量验证指标。"
        )

    parts.append(
        "对品牌方可以表达为：我们不是只做报价和生产，而是会基于相似产品反馈，提前识别该类产品常见翻车点，并用可检测的质量指标证明产品能稳定落地，从而降低上市后的油腻、软便、粉多、不吃或批次不稳定等售后风险。"
    )

    return "\n".join(parts)


# =========================
# 12. 结果写入
# =========================

def ensure_output_table(conn):
    sql = f"""
        CREATE TABLE IF NOT EXISTS {OUTPUT_TABLE} (
            analysis_id VARCHAR(64) NOT NULL PRIMARY KEY,
            target_sku_id VARCHAR(255) NULL,
            analysis_type VARCHAR(32) NOT NULL,
            product_search_result JSON NULL,
            similar_product_problem_clusters JSON NULL,
            process_failure_priority JSON NULL,
            quality_validation_plan JSON NULL,
            sales_pitch_summary TEXT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_target_sku_id (target_sku_id),
            KEY idx_analysis_type (analysis_type),
            KEY idx_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with conn.cursor() as cursor:
        cursor.execute(sql)


def save_analysis_result(
    conn,
    analysis_id: str,
    target_sku_id: Optional[str],
    analysis_type: str,
    product_search_result: List[Dict[str, Any]],
    problem_clusters: List[Dict[str, Any]],
    failure_points: List[Dict[str, Any]],
    quality_plan: List[Dict[str, Any]],
    sales_pitch: str,
):
    sql = f"""
        INSERT INTO {OUTPUT_TABLE}
        (
            analysis_id,
            target_sku_id,
            analysis_type,
            product_search_result,
            similar_product_problem_clusters,
            process_failure_priority,
            quality_validation_plan,
            sales_pitch_summary,
            created_at
        )
        VALUES
        (
            %s, %s, %s, %s, %s, %s, %s, %s, NOW()
        )
        ON DUPLICATE KEY UPDATE
            target_sku_id = VALUES(target_sku_id),
            analysis_type = VALUES(analysis_type),
            product_search_result = VALUES(product_search_result),
            similar_product_problem_clusters = VALUES(similar_product_problem_clusters),
            process_failure_priority = VALUES(process_failure_priority),
            quality_validation_plan = VALUES(quality_validation_plan),
            sales_pitch_summary = VALUES(sales_pitch_summary),
            created_at = NOW()
    """

    with conn.cursor() as cursor:
        cursor.execute(
            sql,
            (
                analysis_id,
                target_sku_id,
                analysis_type,
                json_dumps(product_search_result),
                json_dumps(problem_clusters),
                json_dumps(failure_points),
                json_dumps(quality_plan),
                sales_pitch,
            )
        )


# =========================
# 13. 主分析函数：按目标 SKU 分析
# =========================

def analyze_by_target_sku(
    conn,
    target_sku_id: str,
    top_n: int = DEFAULT_TOP_N,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    recall_mode: str = "default",
) -> Dict[str, Any]:
    """
    输入一个目标 SKU，完成 4 个功能 + 接单话术。
    """
    target_sku = fetch_target_sku(conn, target_sku_id)
    if not target_sku:
        raise ValueError(f"未找到目标 SKU：{target_sku_id}")

    similar_products = get_similar_products(
        conn=conn,
        target_sku_id=target_sku_id,
        min_similarity=min_similarity,
        top_n=top_n,
        recall_mode=recall_mode,
    )

    similar_sku_ids = [x["sku_id"] for x in similar_products]

    feedback_rows = fetch_feedback_for_skus(conn, similar_sku_ids)

    problem_clusters = cluster_feedback_problems(feedback_rows)

    failure_points = infer_failure_points(
        similar_products=similar_products,
        problem_clusters=problem_clusters,
    )

    quality_plan = build_quality_validation_plan(failure_points)

    sales_pitch = generate_sales_pitch(
        target_sku=target_sku,
        similar_products=similar_products,
        problem_clusters=problem_clusters,
        failure_points=failure_points,
        quality_plan=quality_plan,
    )

    product_search_result = [
        {
            "sku_id": p["sku_id"],
            "brand": p["brand"],
            "product_name": p["product_name"],
            "overall_similarity": p["overall_similarity"],
            "process_structure_similarity": p["process_structure_similarity"],
            "ingredient_similarity": p["ingredient_similarity"],
            "nutrition_similarity": p["nutrition_similarity"],
            "risk_reason_similarity": p["risk_reason_similarity"],
            "confidence_factor": p["confidence_factor"],
            "similarity_level": p["similarity_level"],
            "factory_business_tags": p["factory_business_tags"],
            "process_structure_tags": p["process_structure_tags"],
            "shared_process_structure_tags": p["shared_process_structure_tags"],
            "shared_ingredient_category_tags": p["shared_ingredient_category_tags"],
            "ingredient_role_similarity_details": p.get("ingredient_role_similarity_details", []),
            "nutrition_component_similarities": p.get("nutrition_component_similarities", []),
            "high_nutrition_similarity_parts": p.get("high_nutrition_similarity_parts", []),
            "recall_groups": p.get("recall_groups", []),
            "difference_summary": p["difference_summary"],
            "similarity_pattern": p.get("difference_summary", {}).get("similarity_pattern", ""),
            "business_interpretation": p.get("difference_summary", {}).get("business_interpretation", ""),
            "key_differences": p.get("difference_summary", {}).get("key_differences", []),
            "observation_points": p.get("difference_summary", {}).get("observation_points", []),
            "crude_protein": p["crude_protein"],
            "crude_fat": p["crude_fat"],
            "crude_fiber": p["crude_fiber"],
            "estimated_carb": p["estimated_carb"],
            "ingredient_text": p["ingredient_text"],
            "main_process_tags": p["main_process_tags"],
            "process_structure_summary": p["process_structure_summary"],
            "candidate_process_watch_tags": p["candidate_process_watch_tags"],
            "candidate_quality_result_tags": p["candidate_quality_result_tags"],
            "candidate_feedback_risk_tags": p["candidate_feedback_risk_tags"],
            "main_process_modules": p["main_process_modules"],
            "process_tag_details": p["process_tag_details"],
        }
        for p in similar_products
    ]

    analysis_id = f"ANALYSIS_{uuid.uuid4().hex[:12]}"

    save_analysis_result(
        conn=conn,
        analysis_id=analysis_id,
        target_sku_id=target_sku_id,
        analysis_type="target_sku",
        product_search_result=product_search_result,
        problem_clusters=problem_clusters,
        failure_points=failure_points,
        quality_plan=quality_plan,
        sales_pitch=sales_pitch,
    )

    return {
        "analysis_id": analysis_id,
        "target_sku": target_sku,
        "product_search_result": product_search_result,
        "similar_product_problem_clusters": problem_clusters,
        "process_failure_priority": failure_points,
        "quality_validation_plan": quality_plan,
        "sales_pitch_summary": sales_pitch,
    }


# =========================
# 14. 主分析函数：按筛选条件分析
# =========================

def analyze_by_filters(
    conn,
    keyword: Optional[str] = None,
    brand_keyword: Optional[str] = None,
    product_keyword: Optional[str] = None,
    black_chin_risk_levels: Optional[List[str]] = None,
    soft_stool_risk_levels: Optional[List[str]] = None,
    crude_protein_min: Optional[float] = None,
    crude_protein_max: Optional[float] = None,
    crude_fat_min: Optional[float] = None,
    crude_fat_max: Optional[float] = None,
    crude_fiber_min: Optional[float] = None,
    crude_fiber_max: Optional[float] = None,
    carb_score_min: Optional[float] = None,
    carb_score_max: Optional[float] = None,
    required_factory_tags: Optional[List[str]] = None,
    required_process_tags: Optional[List[str]] = None,
    top_n: int = DEFAULT_TOP_N,
) -> Dict[str, Any]:
    """
    输入产品需求筛选条件，完成 4 个功能 + 接单话术。
    用于品牌方只有需求、没有明确 SKU 的场景。
    """
    products = search_products_by_filters(
        conn=conn,
        keyword=keyword,
        brand_keyword=brand_keyword,
        product_keyword=product_keyword,
        black_chin_risk_levels=black_chin_risk_levels,
        soft_stool_risk_levels=soft_stool_risk_levels,
        crude_protein_min=crude_protein_min,
        crude_protein_max=crude_protein_max,
        crude_fat_min=crude_fat_min,
        crude_fat_max=crude_fat_max,
        crude_fiber_min=crude_fiber_min,
        crude_fiber_max=crude_fiber_max,
        carb_score_min=carb_score_min,
        carb_score_max=carb_score_max,
        required_factory_tags=required_factory_tags,
        required_process_tags=required_process_tags,
        top_n=top_n,
    )

    sku_ids = [x["sku_id"] for x in products]

    feedback_rows = fetch_feedback_for_skus(conn, sku_ids)

    problem_clusters = cluster_feedback_problems(feedback_rows)

    # 筛选检索没有相似度字段，统一给一个默认相似权重
    similar_like_products = []
    for p in products:
        item = dict(p)
        item["overall_similarity"] = 0.7
        similar_like_products.append(item)

    failure_points = infer_failure_points(
        similar_products=similar_like_products,
        problem_clusters=problem_clusters,
    )

    quality_plan = build_quality_validation_plan(failure_points)

    sales_pitch = generate_sales_pitch(
        target_sku=None,
        similar_products=similar_like_products,
        problem_clusters=problem_clusters,
        failure_points=failure_points,
        quality_plan=quality_plan,
    )

    product_search_result = [
        {
            "sku_id": p["sku_id"],
            "brand": p["brand"],
            "product_name": p["product_name"],
            "factory_business_tags": p["factory_business_tags"],
            "process_structure_tags": p["process_structure_tags"],
            "black_chin_risk_level": p["black_chin_risk_level"],
            "soft_stool_risk_level": p["soft_stool_risk_level"],
            "crude_protein": p["crude_protein"],
            "crude_fat": p["crude_fat"],
            "crude_fiber": p["crude_fiber"],
            "estimated_carb": p["estimated_carb"],
        }
        for p in products
    ]

    analysis_id = f"ANALYSIS_{uuid.uuid4().hex[:12]}"

    save_analysis_result(
        conn=conn,
        analysis_id=analysis_id,
        target_sku_id=None,
        analysis_type="filter",
        product_search_result=product_search_result,
        problem_clusters=problem_clusters,
        failure_points=failure_points,
        quality_plan=quality_plan,
        sales_pitch=sales_pitch,
    )

    return {
        "analysis_id": analysis_id,
        "product_search_result": product_search_result,
        "similar_product_problem_clusters": problem_clusters,
        "process_failure_priority": failure_points,
        "quality_validation_plan": quality_plan,
        "sales_pitch_summary": sales_pitch,
    }


def fetch_default_target_sku_id(conn) -> Optional[str]:
    sql = f"""
        SELECT s.source_sku_id AS sku_id, COUNT(*) AS similar_count
        FROM {SIMILARITY_TABLE} s
        INNER JOIN {B2B_TAG_TABLE} b
            ON s.source_sku_id = b.sku_id
        GROUP BY s.source_sku_id
        ORDER BY similar_count DESC, s.source_sku_id
        LIMIT 1
    """
    with conn.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    return row.get("sku_id") if row else None


# =========================
# 15. 示例运行
# =========================

def main():
    conn = pymysql.connect(**DB_CONFIG)

    try:
        ensure_output_table(conn)

        # 示例 1：按目标 SKU 分析
        target_sku_id = os.getenv("B2B_TARGET_SKU") or fetch_default_target_sku_id(conn)
        if not target_sku_id:
            raise ValueError("没有找到可分析的 SKU，请先生成 sku_b2b_tag_result 和 sku_similarity_result。")

        result = analyze_by_target_sku(
            conn=conn,
            target_sku_id=target_sku_id,
            top_n=20,
            min_similarity=0.50,
        )

        conn.commit()

        print("分析完成。")
        print("analysis_id:", result["analysis_id"])
        print("\n接单话术：")
        print(result["sales_pitch_summary"])

        print("\n工艺翻车点优先级：")
        for item in result["process_failure_priority"][:5]:
            print(item["priority"], item["name"], item["priority_score"])

        print("\n质量验证指标：")
        for item in result["quality_validation_plan"][:10]:
            print(item["priority"], item["indicator"], item["priority_score"])

        # 示例 2：按条件分析
        # result = analyze_by_filters(
        #     conn=conn,
        #     crude_protein_min=38,
        #     crude_protein_max=45,
        #     crude_fat_min=16,
        #     crude_fat_max=22,
        #     required_factory_tags=["无谷配方", "高蛋白主粮"],
        #     required_process_tags=["高脂高喷涂关注型"],
        #     top_n=30,
        # )
        # conn.commit()
        # print(result["sales_pitch_summary"])

    except Exception as e:
        conn.rollback()
        print("执行失败，已回滚：", str(e))
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()
