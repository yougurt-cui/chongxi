"""View-model adapter for the competitor growth breakdown page.

V2: 基于 catfood_formula_profile (结构标签 + 市场排名 + 优势/短板) 做召回和证据层。
保留旧版 8503 同源算法作为 fallback。
"""

from __future__ import annotations

import json
import sys
import os
import importlib
from pathlib import Path
from typing import Any
from decimal import Decimal

import pymysql
from app_config import get_feature_mysql_config


CONSUMER_APPS_DIR = Path(__file__).resolve().parents[1] / "consumer_apps"
if str(CONSUMER_APPS_DIR) not in sys.path:
    sys.path.insert(0, str(CONSUMER_APPS_DIR))

import b2b_order_analysis as b2b  # noqa: E402


def _connection():
    config = _db_config()
    return pymysql.connect(**config)


def _db_config() -> dict[str, Any]:
    config = get_feature_mysql_config()
    config.setdefault("charset", "utf8mb4")
    config.setdefault("cursorclass", pymysql.cursors.DictCursor)
    return config


def _text(value: Any, fallback: str = "—") -> str:
    value = str(value or "").strip()
    return value or fallback


def _tags(value: Any, limit: int = 4) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, "") for item in value if _text(item, "")][:limit]


def _nutrition_rows(product: dict[str, Any]) -> list[dict[str, Any]]:
    rows = product.get("nutrition_component_similarities") or []
    normalized = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = item.get("component") or item.get("name") or item.get("label") or item.get("营养项")
        score = item.get("similarity")
        if score is None:
            score = item.get("score") or item.get("相似度")
        try:
            score = float(score)
            if score > 1:
                score /= 100
        except (TypeError, ValueError):
            continue
        normalized.append({"name": _text(name, "营养结构"), "score": round(score, 4)})
    normalized.sort(key=lambda row: row["score"], reverse=True)
    return normalized[:3]


def _module_rows(product: dict[str, Any]) -> list[dict[str, Any]]:
    modules = []
    differences = product.get("key_differences") or []
    observations = product.get("observation_points") or []
    module_names = ["蛋白模块", "碳水模块", "工艺/成品"]
    for index, name in enumerate(module_names):
        detail = differences[index] if index < len(differences) else None
        if isinstance(detail, dict):
            detail = detail.get("description") or detail.get("label") or detail.get("name") or str(detail)
        feedback = observations[index] if index < len(observations) else "需结合用户反馈继续验证"
        if isinstance(feedback, dict):
            feedback = feedback.get("description") or feedback.get("label") or str(feedback)
        modules.append({"name": name, "detail": _text(detail, "当前结果未识别显著差异"), "feedback": _text(feedback)})
    return modules


def _factor_value(row: dict[str, Any], factor: str) -> str:
    if factor == "目标人群":
        return _text(" / ".join(_tags(row.get("factory_business_tags"), 3)))
    if factor == "共同配方骨架":
        return _text(" / ".join(_tags(row.get("shared_ingredient_category_tags"), 4)))
    if factor == "主动物蛋白路径":
        ingredients = [x.strip() for x in _text(row.get("ingredient_text"), "").replace("，", ",").split(",") if x.strip()]
        return _text(" + ".join(ingredients[:3]))
    if factor == "碳水路径":
        return f"估算碳水 {float(row.get('estimated_carb') or 0):.1f}%"
    if factor == "工艺观察点":
        return _text(" / ".join(_tags(row.get("main_process_tags"), 3)))
    if factor == "成品指标":
        return f"蛋白 {float(row.get('crude_protein') or 0):.1f}% · 脂肪 {float(row.get('crude_fat') or 0):.1f}% · 纤维 {float(row.get('crude_fiber') or 0):.1f}%"
    if factor == "风险短板":
        return _text(" / ".join(_tags(row.get("candidate_feedback_risk_tags"), 3)), "暂无明确高风险信号")
    return "—"


def _json_parse(value: Any) -> Any:
    """安全解析 JSON 字段（可能是字符串或已解析对象）"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


# ============================================================
# V2: 基于 formula_profile 的新召回逻辑
# ============================================================

# 病症与模块权重的映射（来自 recommendation_profiles）
SYMPTOM_WEIGHTS = {
    "软便/拉稀": {
        "消化支持": 0.22, "纤维": 0.18, "功能性营养": 0.16,
        "淀粉碳水": -0.16, "益生元": 0.16, "脂肪": -0.10, "动物蛋白": 0.12,
    },
    "黑下巴": {
        "脂肪": -0.25, "功能性营养": 0.20, "适口性": 0.15,
        "矿物微量营养": 0.15, "淀粉碳水": -0.15, "动物蛋白": 0.10,
    },
    "呕吐": {
        "消化支持": 0.25, "适口性": 0.15, "纤维": 0.15,
        "淀粉碳水": -0.15, "脂肪": -0.15, "动物蛋白": 0.15,
    },
    "拒食/不爱吃": {
        "适口性": 0.35, "脂肪": 0.20, "动物蛋白": 0.20,
        "淀粉碳水": 0.10, "纤维": 0.05, "益生元": 0.05, "消化支持": 0.05,
    },
    "掉毛": {
        "功能性营养": 0.30, "脂肪": 0.25, "矿物微量营养": 0.20,
        "动物蛋白": 0.15, "淀粉碳水": -0.10,
    },
    "便秘": {
        "纤维": 0.30, "消化支持": 0.25, "益生元": 0.20,
        "淀粉碳水": -0.15, "脂肪": 0.10,
    },
}

# 病症对应优先保留的结构标签
SYMPTOM_STRUCTURE_FILTER = {
    "软便/拉稀": {
        "include": ["高吸水纤维结构", "益生元+纤维复合结构", "不溶性纤维结构"],
        "exclude": ["谷物膨化结构"],
    },
    "便秘": {
        "include": ["高吸水纤维结构", "益生元发酵结构", "益生元+纤维复合结构"],
        "exclude": [],
    },
    "呕吐": {
        "include": ["水解蛋白增味结构"],
        "exclude": ["高动物脂肪结构"],
    },
}

MODULE_COL_MAP = {
    "动物蛋白": "animal_protein_pct",
    "淀粉碳水": "starch_carb_pct",
    "脂肪": "fat_pct",
    "纤维": "fiber_pct",
    "益生元": "prebiotic_pct",
    "消化支持": "digestion_support_pct",
    "适口性": "palatability_pct",
    "矿物微量营养": "mineral_micronutrient_pct",
    "功能性营养": "functional_nutrition_pct",
}


def list_disease_target_options(symptom: str = "软便/拉稀", limit: int = 50) -> dict[str, Any]:
    """从 cat_disease_clues 取改善品牌 → 品牌下所有产品作为目标选项

    筛选条件：
    1. 改善条数 > 加重条数（净改善正向）
    2. 改善条数 >= 5（最低证据量）
    3. 总条数 >= 10（避免极小样本）
    """
    # Step 1: 从 clues 表取该病症下改善>加重 的品牌
    brand_sql = """
        SELECT brand,
               SUM(direct='改善') AS improve_cnt,
               SUM(direct='加重') AS worsen_cnt,
               COUNT(*) AS total_cnt
        FROM protein_feature_platform.cat_disease_clues
        WHERE (secondary_symptom = %s OR primary_symptom LIKE %s OR secondary_symptom LIKE %s)
        GROUP BY brand
        HAVING improve_cnt > worsen_cnt
           AND improve_cnt >= 5
           AND total_cnt >= 10
        ORDER BY improve_cnt DESC
        LIMIT 20
    """
    with _connection() as conn, conn.cursor() as cursor:
        cursor.execute(brand_sql, (symptom, f"%{symptom}%", f"%{symptom}%"))
        brand_rows = cursor.fetchall()

    brand_map = {
        row["brand"]: {"clue_count": row["improve_cnt"], "worsen_cnt": row["worsen_cnt"], "total_cnt": row["total_cnt"]}
        for row in brand_rows if row["brand"]
    }
    brands = list(brand_map.keys())
    if not brands:
        return {"ok": True, "items": [], "symptom": symptom, "brands": []}

    # Step 2: 从 formula_profile 取这些品牌的所有产品
    placeholders = ",".join(["%s"] * len(brands))
    product_sql = f"""
        SELECT formula_id, product_key, brand, product_name
        FROM protein_feature_platform.catfood_formula_profile
        WHERE brand IN ({placeholders})
        ORDER BY brand, product_name
        LIMIT %s
    """
    with _connection() as conn, conn.cursor() as cursor:
        cursor.execute(product_sql, (*brands, max(1, min(limit, 200))))
        product_rows = cursor.fetchall()

    items = [{
        "formula_id": row["formula_id"],
        "sku_id": row.get("product_key") or f"formula_{row['formula_id']}",
        "brand": row["brand"],
        "product_name": row["product_name"],
        "brand_clue_count": brand_map[row["brand"]]["clue_count"],
        "brand_worsen_cnt": brand_map[row["brand"]]["worsen_cnt"],
    } for row in product_rows]

    # 按品牌改善条数降序排列，同品牌按产品名排列
    items.sort(key=lambda x: (-x["brand_clue_count"], x["brand"], x["product_name"] or ""))

    brand_summary = [{
        "brand": b,
        "improve_cnt": brand_map[b]["clue_count"],
        "worsen_cnt": brand_map[b]["worsen_cnt"],
        "total_cnt": brand_map[b]["total_cnt"],
    } for b in brands]
    return {"ok": True, "items": items, "symptom": symptom, "brands": brand_summary}


def recall_by_profile(
    seed_formula_ids: list[int],
    symptom: str = "软便/拉稀",
    top_n: int = 5,
) -> dict[str, Any]:
    """基于 catfood_formula_profile 的三阶段召回

    Stage 1: 结构标签过滤
    Stage 2: 加权向量相似度排序
    Stage 3: 证据层组装
    """
    if not seed_formula_ids:
        return {"candidates": [], "seed_profiles": [], "evidence": {}}

    sql = """
        SELECT formula_id, product_key, brand, product_name,
               structure_labels, market_rankings, advantage_tags, weakness_tags,
               profile_summary
        FROM protein_feature_platform.catfood_formula_profile
        WHERE formula_id IN ({placeholders})
    """.format(placeholders=",".join(["%s"] * len(seed_formula_ids)))

    with _connection() as conn, conn.cursor() as cursor:
        # 加载种子画像
        cursor.execute(sql, seed_formula_ids)
        seed_rows = cursor.fetchall()

        # 加载全部候选画像
        cursor.execute("""
            SELECT formula_id, product_key, brand, product_name,
                   structure_labels, market_rankings, advantage_tags, weakness_tags,
                   profile_summary
            FROM protein_feature_platform.catfood_formula_profile
            WHERE market_rankings IS NOT NULL
        """)
        all_rows = cursor.fetchall()

    # 解析 JSON 字段
    def parse_row(row):
        return {
            **row,
            "_structure": _json_parse(row.get("structure_labels")) or [],
            "_rankings": _json_parse(row.get("market_rankings")) or {},
            "_advantages": _json_parse(row.get("advantage_tags")) or [],
            "_weaknesses": _json_parse(row.get("weakness_tags")) or [],
        }

    seeds = [parse_row(r) for r in seed_rows]
    # 获取自有品牌列表，召回时排除同品牌产品
    seed_brands = {s.get("brand", "") for s in seeds if s.get("brand")}
    candidates = [
        parse_row(r) for r in all_rows
        if r["formula_id"] not in seed_formula_ids
        and r.get("brand", "") not in seed_brands
    ]

    if not seeds or not candidates:
        return {"candidates": [], "seed_profiles": seeds, "evidence": {}}

    # 种子向量：取各模块百分位均值
    seed_vector = {}
    for module, col in MODULE_COL_MAP.items():
        vals = [s["_rankings"].get(module) for s in seeds if s["_rankings"].get(module) is not None]
        seed_vector[module] = sum(vals) / len(vals) if vals else 50.0

    # 获取权重
    weights = SYMPTOM_WEIGHTS.get(symptom, SYMPTOM_WEIGHTS["软便/拉稀"])
    structure_filter = SYMPTOM_STRUCTURE_FILTER.get(symptom, {})
    include_tags = set(structure_filter.get("include", []))
    exclude_tags = set(structure_filter.get("exclude", []))

    # Stage 1 + 2: 结构过滤 + 加权相似度
    scored = []
    for cand in candidates:
        # Stage 1: 结构标签过滤（软过滤：不匹配的扣分而非淘汰）
        cand_level2 = {lb.get("level2") for lb in cand["_structure"]}
        structure_bonus = 0.0
        if include_tags:
            if cand_level2 & include_tags:
                structure_bonus += 0.1
        if exclude_tags and cand_level2 & exclude_tags:
            structure_bonus -= 0.15

        # Stage 2: 加权向量相似度
        sim_score = 0.0
        weight_sum = 0.0
        for module, weight in weights.items():
            cand_val = cand["_rankings"].get(module)
            seed_val = seed_vector.get(module, 50.0)
            if cand_val is not None:
                # 距离越小越相似，转换为相似度 (0-1)
                diff = abs(float(cand_val) - seed_val)
                similarity = max(0, 1 - diff / 100)
                abs_weight = abs(weight)
                sim_score += abs_weight * similarity
                weight_sum += abs_weight

        normalized_sim = (sim_score / weight_sum + structure_bonus) if weight_sum > 0 else 0
        scored.append((cand, round(normalized_sim, 4)))

    # 排序取 top_n
    scored.sort(key=lambda x: x[1], reverse=True)
    top_candidates = scored[:top_n]

    # Stage 3: 组装证据层
    evidence = _build_evidence(seeds, top_candidates, seed_vector, weights, symptom)

    return {
        "candidates": [
            {
                "formula_id": cand["formula_id"],
                "sku_id": cand.get("product_key") or f"formula_{cand['formula_id']}",
                "brand": cand["brand"],
                "product_name": cand["product_name"],
                "overall_similarity": sim,
                "structure_labels": cand["_structure"],
                "advantage_tags": cand["_advantages"],
                "weakness_tags": cand["_weaknesses"],
                "profile_summary": cand.get("profile_summary"),
                "market_rankings": cand["_rankings"],
            }
            for cand, sim in top_candidates
        ],
        "raw_candidates": [cand for cand, _ in top_candidates],
        "seed_profiles": seeds,
        "seed_vector": seed_vector,
        "evidence": evidence,
    }


def _build_evidence(
    seeds: list[dict],
    top_candidates: list[tuple[dict, float]],
    seed_vector: dict,
    weights: dict,
    symptom: str,
) -> dict[str, Any]:
    """组装证据层数据"""
    if not top_candidates:
        return {"formula_skeleton": {}, "module_similarity": [], "module_differences": [], "symptom_fit": []}

    lead_cand, lead_sim = top_candidates[0]

    # 1. 配方骨架：模块 -> 结构标签映射
    # level1 到模块名的映射
    LEVEL1_TO_MODULE = {
        "蛋白结构": "动物蛋白",
        "碳水/淀粉结构": "淀粉碳水",
        "脂肪结构": "脂肪",
        "纤维结构": "纤维",
        "肠道功能结构": "消化支持",
        "适口性结构": "适口性",
        "矿物/微量营养结构": "矿物微量营养",
    }

    # 标杆结构标签按模块分组
    seed_by_module = {}
    for s in seeds:
        for lb in s["_structure"]:
            l1 = lb.get("level1", "")
            l2 = lb.get("level2", "")
            module = LEVEL1_TO_MODULE.get(l1, "其他")
            if module not in seed_by_module:
                seed_by_module[module] = set()
            seed_by_module[module].add(l2)

    # 候选结构标签按模块分组
    cand_by_module = {}
    for lb in lead_cand["_structure"]:
        l1 = lb.get("level1", "")
        l2 = lb.get("level2", "")
        module = LEVEL1_TO_MODULE.get(l1, "其他")
        if module not in cand_by_module:
            cand_by_module[module] = set()
        cand_by_module[module].add(l2)

    # 组装模块骨架对比：只保留有共同结构的模块
    all_modules = sorted(set(list(seed_by_module.keys()) + list(cand_by_module.keys())))
    module_skeleton = []
    structure_diffs = []  # 结构差异收集给关键差异模块用
    for module in all_modules:
        seed_tags = sorted(seed_by_module.get(module, []))
        cand_tags = sorted(cand_by_module.get(module, []))
        shared = sorted(set(seed_tags) & set(cand_tags))
        if shared:  # 只保留有共同结构的
            module_skeleton.append({
                "module": module,
                "seed_tags": seed_tags,
                "cand_tags": cand_tags,
                "shared_count": len(shared),
            })
        else:
            # 结构不同，收集差异信息
            seed_str = " + ".join(seed_tags) if seed_tags else "无"
            cand_str = " + ".join(cand_tags) if cand_tags else "无"
            structure_diffs.append({
                "name": module,
                "type": "结构差异",
                "seed_struct": seed_str,
                "cand_struct": cand_str,
                "detail": f"标杆: {seed_str} → 候选: {cand_str}",
            })

    # 营养模块百分位对比
    def _level_desc(pct):
        if pct >= 75:
            return "高添加量"
        elif pct >= 50:
            return "中高添加量"
        elif pct >= 25:
            return "中低添加量"
        else:
            return "低添加量"

    module_sims = []
    # 只取配方骨架中有共同结构的模块，保持两个卡片一致
    skeleton_modules = [m["module"] for m in module_skeleton]
    for module in skeleton_modules:
        # 使用 MODULE_COL_MAP 获取英文字段名
        col_name = MODULE_COL_MAP.get(module, module)
        cand_val = lead_cand["_rankings"].get(col_name)
        seed_val = seed_vector.get(module, 50.0)
        # 即使候选没有百分位数据也显示，用默认值50
        cand_pct = float(cand_val) if cand_val is not None else 50.0
        diff = abs(cand_pct - seed_val)
        similarity = max(0, 1 - diff / 100)
        module_sims.append({
            "name": module,
            "score": round(similarity, 4),
            "diff": round(diff, 1),
            "seed_pct": round(seed_val, 1),
            "cand_pct": round(cand_pct, 1),
            "level": _level_desc(seed_val),
        })
    module_sims.sort(key=lambda x: x["score"], reverse=True)

    # 3. 关键差异模块：种子优势 vs 候选短板
    seed_advantages = set()
    for s in seeds:
        seed_advantages.update(s["_advantages"])
    cand_weaknesses = set(lead_cand["_weaknesses"])
    cand_advantages = set(lead_cand["_advantages"])
    seed_weaknesses = set()
    for s in seeds:
        seed_weaknesses.update(s["_weaknesses"])

    differences = []
    # 1. 先加结构差异（最直观的差异）
    differences.extend(structure_diffs)

    # 2. 种子优势但候选短板 = 差异化卖点
    for module in sorted(seed_advantages & cand_weaknesses):
        if module not in {d["name"] for d in differences}:
            differences.append({
                "name": module,
                "type": "标杆优势",
                "detail": f"标杆在此模块为市场优势(≥P75)，候选为短板(≤P25)",
                "feedback": "可作为竞品对比的差异化卖点",
            })
    # 3. 候选优势但种子短板 = 候选亮点
    for module in sorted(cand_advantages & seed_weaknesses):
        if module not in {d["name"] for d in differences}:
            differences.append({
                "name": module,
                "type": "候选亮点",
                "detail": f"候选在此模块为市场优势，标杆为短板",
                "feedback": "候选可能有更好的该维度表现",
            })
    # 4. 补位：如果差异不够，用分位差最大的模块
    if len(differences) < 3:
        for ms in sorted(module_sims, key=lambda x: x["score"])[:3 - len(differences)]:
            if ms["name"] not in {d["name"] for d in differences}:
                differences.append({
                    "name": ms["name"],
                    "type": "分位差异",
                    "detail": f"分位差 {ms['diff']}%，相似度 {ms['score']*100:.0f}%",
                    "feedback": "需结合用户反馈继续验证",
                })

    # 4. 病症适配度：候选对病症的适配/不适配
    symptom_fit = []
    for module, weight in weights.items():
        cand_val = lead_cand["_rankings"].get(module)
        if cand_val is None:
            continue
        is_positive = weight > 0
        fit_score = float(cand_val)
        if is_positive:
            if fit_score >= 75:
                symptom_fit.append({"module": module, "fit": "好", "score": fit_score, "note": f"{module}分位 {fit_score:.0f}，符合病症需求"})
            elif fit_score <= 25:
                symptom_fit.append({"module": module, "fit": "差", "score": fit_score, "note": f"{module}分位仅 {fit_score:.0f}，不满足病症需求"})
        else:
            if fit_score <= 25:
                symptom_fit.append({"module": module, "fit": "好", "score": 100 - fit_score, "note": f"{module}负担低(分位 {fit_score:.0f})，符合病症需求"})
            elif fit_score >= 75:
                symptom_fit.append({"module": module, "fit": "差", "score": 100 - fit_score, "note": f"{module}负担高(分位 {fit_score:.0f})，不适合该病症"})

    return {
        "formula_skeleton": {
            "title": "配方骨架",
            "modules": module_skeleton,
        },
        "module_similarity": module_sims,
        "module_differences": differences[:5],
        "symptom_fit": symptom_fit,
    }


# ============================================================
# V2: 标杆配方因子拆解 (per-module breakdown)
# ============================================================

# 模块显示名（与 02 证据层/结构标签映射保持一致）
MODULE_DISPLAY = {
    "益生元": "益生元",
    "脂肪": "脂肪",
    "动物蛋白": "动物蛋白",
    "纤维": "纤维",
    "淀粉碳水": "淀粉碳水",
    "消化支持": "消化支持",
    "适口性": "适口性",
    "矿物微量营养": "矿物微量营养",
    "功能性营养": "功能性营养",
}

# 模块极性：negative 表示该模块为负担类指标（越低=负担越重）
MODULE_POLARITY = {
    "动物蛋白": "positive",
    "淀粉碳水": "negative",
    "脂肪": "negative",
    "纤维": "positive",
    "益生元": "positive",
    "消化支持": "positive",
    "适口性": "positive",
    "矿物微量营养": "positive",
    "功能性营养": "negative",
}

# 极性对应的图表标题
MODULE_SCORE_LABEL = {
    "动物蛋白": "质量分位",
    "淀粉碳水": "负担分位",
    "脂肪": "负担分位",
    "纤维": "质量分位",
    "益生元": "质量分位",
    "消化支持": "质量分位",
    "适口性": "质量分位",
    "矿物微量营养": "质量分位",
    "功能性营养": "负担分位",
}

# 模块 -> structure_labels.level1 映射
MODULE_TO_LEVEL1 = {
    "益生元": "肠道功能结构",
    "消化支持": "肠道功能结构",
    "脂肪": "脂肪结构",
    "动物蛋白": "蛋白结构",
    "纤维": "纤维结构",
    "淀粉碳水": "碳水/淀粉结构",
    "适口性": "适口性结构",
    "矿物微量营养": "矿物/微量营养结构",
}

# 模块展示顺序
MODULE_ORDER = [
    "益生元", "脂肪", "动物蛋白", "纤维", "淀粉碳水",
    "消化支持", "适口性", "矿物微量营养", "功能性营养",
]


def _load_ingredient_data(formula_ids: list[int]) -> dict[int, dict]:
    """从 protein_source_aggregate / catfood_fat_material_features / catfood_fiber_feature_json 加载原料数据"""
    if not formula_ids:
        return {}
    result: dict[int, dict] = {}
    placeholders = ",".join(["%s"] * len(formula_ids))
    with _connection() as conn, conn.cursor() as cursor:
        # 蛋白原料
        cursor.execute(f"""
            SELECT formula_id, protein_source_details, animal_sources
            FROM protein_source_aggregate
            WHERE formula_id IN ({placeholders})
        """, formula_ids)
        for row in cursor.fetchall():
            fid = row["formula_id"]
            result.setdefault(fid, {})
            result[fid]["protein_ingredients"] = row.get("protein_source_details") or ""
            result[fid]["animal_sources"] = row.get("animal_sources") or ""

        # 脂肪原料
        cursor.execute(f"""
            SELECT formula_id, fat_sources
            FROM catfood_fat_material_features
            WHERE formula_id IN ({placeholders})
        """, formula_ids)
        for row in cursor.fetchall():
            fid = row["formula_id"]
            result.setdefault(fid, {})
            result[fid]["fat_ingredients"] = row.get("fat_sources") or ""

        # 纤维 & 益生元 & 淀粉
        cursor.execute(f"""
            SELECT formula_id, ingredient_feature_json, starch_ingredients_json
            FROM catfood_fiber_feature_json
            WHERE formula_id IN ({placeholders})
        """, formula_ids)
        for row in cursor.fetchall():
            fid = row["formula_id"]
            result.setdefault(fid, {})
            feat = _json_parse(row.get("ingredient_feature_json")) or {}
            result[fid]["prebiotic_ingredients"] = _extract_prebiotic_ingredients(feat)
            result[fid]["fiber_ingredients"] = _extract_fiber_ingredients(feat)
            starch = _json_parse(row.get("starch_ingredients_json")) or []
            result[fid]["starch_ingredients"] = _extract_starch_ingredients(starch)
    return result


def _extract_prebiotic_ingredients(feat_json: dict) -> str:
    tag_detail = feat_json.get("ingredient_tag_detail") or feat_json.get("tag_detail") or {}
    items = []
    for name, info in tag_detail.items():
        if isinstance(info, dict) and info.get("prebiotic_functions"):
            items.append(name)
    return " / ".join(items[:6]) if items else ""


def _extract_fiber_ingredients(feat_json: dict) -> str:
    tag_detail = feat_json.get("ingredient_tag_detail") or feat_json.get("tag_detail") or {}
    items = []
    for name, info in tag_detail.items():
        if isinstance(info, dict):
            cat = info.get("ingredient_category", "")
            if "纤维" in cat:
                items.append(name)
    return " / ".join(items[:6]) if items else ""


def _extract_starch_ingredients(starch_json: list) -> str:
    items = []
    for item in starch_json:
        if isinstance(item, dict):
            name = item.get("ingredient_name") or item.get("name") or ""
            if name:
                items.append(name)
    return " / ".join(items[:6]) if items else ""


def _get_module_ingredients(ingredient_data: dict, module: str) -> str:
    mapping = {
        "动物蛋白": "protein_ingredients",
        "脂肪": "fat_ingredients",
        "益生元": "prebiotic_ingredients",
        "纤维": "fiber_ingredients",
        "淀粉碳水": "starch_ingredients",
    }
    key = mapping.get(module)
    if not key:
        return ""
    return ingredient_data.get(key, "")


def _build_benchmark_modules(
    seeds: list[dict],
    raw_candidates: list[dict],
    seed_vector: dict,
    symptom: str,
) -> dict[str, Any]:
    """组装按模块维度的标杆配方因子拆解数据

    每个模块包含:
    - 差异度分数 (market_rankings 百分位)
    - 对比表 4 行: 原料 / 配方二级 / 工艺一级 / 工艺二级
    - 结论文字
    """
    all_formula_ids = [s.get("formula_id") for s in seeds if s.get("formula_id")]
    all_formula_ids.extend(c.get("formula_id") for c in raw_candidates if c.get("formula_id"))
    ingredient_map = _load_ingredient_data(all_formula_ids)

    # 统一产品列表
    all_products = []
    for s in seeds:
        all_products.append({
            "formula_id": s.get("formula_id"),
            "brand": s.get("brand", ""),
            "product_name": s.get("product_name", ""),
            "is_target": True,
            "rankings": s.get("_rankings") or {},
            "structure": s.get("_structure") or [],
            "ingredients": ingredient_map.get(s.get("formula_id"), {}),
        })
    for c in raw_candidates:
        all_products.append({
            "formula_id": c.get("formula_id"),
            "brand": c.get("brand", ""),
            "product_name": c.get("product_name", ""),
            "is_target": False,
            "rankings": c.get("_rankings") or {},
            "structure": c.get("_structure") or [],
            "ingredients": ingredient_map.get(c.get("formula_id"), {}),
        })

    modules_data = []
    for module in MODULE_ORDER:
        level1 = MODULE_TO_LEVEL1.get(module)
        col_name = MODULE_COL_MAP.get(module, module)

        # 目标百分位
        target_score = seed_vector.get(module)
        if target_score is None:
            target_score = all_products[0]["rankings"].get(module) if all_products else None
        if target_score is None:
            continue
        target_score = float(target_score)

        # 各产品百分位
        product_scores = []
        for p in all_products:
            val = p["rankings"].get(module)
            if val is None and col_name in p["rankings"]:
                val = p["rankings"][col_name]
            product_scores.append((p, float(val) if val is not None else None))

        # 候选均分 & 差距
        comp_scores = [s for p, s in product_scores if not p["is_target"] and s is not None]
        avg_comp = sum(comp_scores) / len(comp_scores) if comp_scores else 50.0
        diff_gap = target_score - avg_comp

        # 组装产品对比行
        products_data = []
        for p, score in product_scores:
            labels = [lb for lb in p["structure"] if lb.get("level1") == level1] if level1 else []
            level2_str = " / ".join(lb.get("level2", "") for lb in labels if lb.get("level2")) or "—"
            process1_str = " / ".join(lb.get("process1", "") for lb in labels if lb.get("process1")) or "—"
            process2_str = " / ".join(lb.get("process2", "") for lb in labels if lb.get("process2")) or "—"
            ingredient_str = _get_module_ingredients(p["ingredients"], module) or "—"

            products_data.append({
                "label": f"{p['brand']} · {p['product_name']}" if p.get("product_name") else p.get("brand", ""),
                "brand": p.get("brand", ""),
                "score": round(score, 1) if score is not None else 0,
                "is_target": p["is_target"],
                "rows": {
                    "原料": ingredient_str,
                    "配方二级": level2_str,
                    "工艺一级": process1_str,
                    "工艺二级": process2_str,
                },
            })

        # 结论（按极性区分措辞，避免负向指标被误读）
        display = MODULE_DISPLAY.get(module, module)
        polarity = MODULE_POLARITY.get(module, "positive")
        if polarity == "negative":
            if diff_gap > 20:
                conclusion = f"目标 SKU 在{display}模块负担明显更低，召回产品负担更高"
            elif diff_gap > 10:
                conclusion = f"目标 SKU 在{display}模块负担较低，召回产品整体负担偏高"
            elif diff_gap > 0:
                conclusion = f"目标 SKU 在{display}模块负担略低于竞品均值"
            else:
                conclusion = f"目标 SKU 在{display}模块与竞品负担差距不大"
        else:
            if diff_gap > 20:
                conclusion = f"目标 SKU 在{display}模块明显更强，召回产品存在显著差距"
            elif diff_gap > 10:
                conclusion = f"目标 SKU 在{display}模块有一定优势，召回产品整体偏弱"
            elif diff_gap > 0:
                conclusion = f"目标 SKU 在{display}模块略优于竞品均值"
            else:
                conclusion = f"目标 SKU 在{display}模块与竞品差距不大"

        modules_data.append({
            "name": module,
            "display_name": display,
            "polarity": polarity,
            "score_label": MODULE_SCORE_LABEL.get(module, "市场分位"),
            "polarity_note": "越低表示负担越重" if polarity == "negative" else "越高表示表现越好",
            "score": round(target_score, 1),
            "diff_gap": round(diff_gap, 1),
            "conclusion": conclusion,
            "products": products_data,
        })

    # 按差距降序排列
    modules_data.sort(key=lambda x: x["diff_gap"], reverse=True)
    for i, m in enumerate(modules_data):
        m["rank"] = i + 1

    return {"modules": modules_data}


def list_competitor_sku_options(q: str = "", limit: int = 80) -> dict[str, Any]:
    """本地 SKU 库选项：从 catfood_formula_profile 读取"""
    like = f"%{q.strip()}%"
    sql = """
        SELECT product_key AS sku_id,
               formula_id,
               brand,
               product_name
        FROM protein_feature_platform.catfood_formula_profile
        WHERE (%s = '' OR product_key LIKE %s OR brand LIKE %s OR product_name LIKE %s)
        ORDER BY brand, product_name, product_key
        LIMIT %s
    """
    with _connection() as conn, conn.cursor() as cursor:
        cursor.execute(sql, (q.strip(), like, like, like, max(1, min(limit, 300))))
        rows = cursor.fetchall()
    return {"ok": True, "items": rows, "source": "catfood_formula_profile"}


def _normalize_sku_id(sku_id: str) -> str:
    """把 SKU id 归一化：移除空格、把品牌后的标点去掉并小写，用于跨库匹配。"""
    if not sku_id:
        return sku_id
    parts = sku_id.split("||", 1)
    if len(parts) != 2:
        return sku_id.strip().lower()
    brand = parts[0].rstrip("!.。!！").strip().lower()
    name = parts[1].replace(" ", "").strip().lower()
    return f"{brand}||{name}"


def _resolve_b2b_sku_id(local_sku_id: str) -> str:
    """把本地 SKU（catfood_formula_profile.product_key）解析成 B2B 标准库的 sku_id。

    由于历史原因，B2B 表里的 sku_id 可能与本地 product_key 在大小写、空格、品牌写法上不一致，
    这里依次做：精确匹配、忽略大小写匹配、归一化匹配。
    """
    if not local_sku_id:
        return local_sku_id
    with _connection() as conn, conn.cursor() as cursor:
        # 1. 精确匹配
        cursor.execute(f"SELECT sku_id FROM {b2b.B2B_TAG_TABLE} WHERE sku_id = %s", (local_sku_id,))
        row = cursor.fetchone()
        if row:
            return row["sku_id"]
        # 2. 忽略大小写匹配
        cursor.execute(
            f"SELECT sku_id FROM {b2b.B2B_TAG_TABLE} WHERE LOWER(sku_id) = LOWER(%s)",
            (local_sku_id,),
        )
        row = cursor.fetchone()
        if row:
            return row["sku_id"]
        # 3. 归一化匹配（处理 GO! -> go、多余空格、标点等）
        normalized = _normalize_sku_id(local_sku_id)
        cursor.execute(f"SELECT sku_id FROM {b2b.B2B_TAG_TABLE}")
        for row in cursor.fetchall():
            if _normalize_sku_id(row["sku_id"]) == normalized:
                return row["sku_id"]
    return local_sku_id


def build_competitor_breakdown(payload: dict[str, Any]) -> dict[str, Any]:
    """V2: 基于 formula_profile 的竞品拆解"""
    target_formula_id = payload.get("target_formula_id")
    symptom = _text(payload.get("symptom"), "软便/拉稀")
    top_n = max(1, min(int(payload.get("top_n") or 3), 8))

    # 解析目标 formula_id
    seed_ids = []
    if target_formula_id:
        try:
            seed_ids = [int(target_formula_id)]
        except (ValueError, TypeError):
            pass
    if not seed_ids:
        # 默认取病症下改善率最高的产品
        options = list_disease_target_options(symptom, limit=1)
        if options.get("items"):
            seed_ids = [options["items"][0]["formula_id"]]

    if not seed_ids:
        return {"ok": False, "error": "未找到目标产品", "symptom": symptom}

    # 执行召回
    recall_result = recall_by_profile(seed_ids, symptom, top_n)
    seeds = recall_result.get("seed_profiles", [])
    candidates = recall_result.get("candidates", [])
    evidence = recall_result.get("evidence", {})
    seed_vector = recall_result.get("seed_vector", {})

    # 组装目标 SKU 信息
    target_seed = seeds[0] if seeds else {}
    target = {
        "formula_id": target_seed.get("formula_id"),
        "sku_id": target_seed.get("product_key") or f"formula_{target_seed.get('formula_id')}",
        "brand": target_seed.get("brand"),
        "product_name": target_seed.get("product_name"),
        "structure_labels": target_seed.get("_structure", []),
        "advantage_tags": target_seed.get("_advantages", []),
        "weakness_tags": target_seed.get("_weaknesses", []),
    }

    # 组装竞品池
    pool = [{
        "formula_id": c["formula_id"],
        "sku_id": c["sku_id"],
        "brand": c["brand"],
        "product_name": c["product_name"],
        "overall_similarity": c["overall_similarity"],
        "structure_labels": c["structure_labels"],
        "advantage_tags": c["advantage_tags"],
        "weakness_tags": c["weakness_tags"],
        "profile_summary": c.get("profile_summary"),
        "market_rankings": c.get("market_rankings", {}),
    } for c in candidates]

    # 组装标杆配方因子拆解 (per-module)
    raw_candidates = recall_result.get("raw_candidates", [])
    benchmark_factors = _build_benchmark_modules(seeds, raw_candidates, seed_vector, symptom)

    return {
        "ok": True,
        "analysis_id": f"PROFILE_{seed_ids[0]}_{symptom}",
        "symptom": symptom,
        "scope_note": f"基于 catfood_formula_profile 的结构标签+市场百分位召回；病症权重来自 recommendation_profiles({symptom})。",
        "target": target,
        "competitor_pool": pool,
        "evidence": evidence,
        "benchmark_factors": benchmark_factors,
        "seed_vector": seed_vector,
    }


def build_sku_risk_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the 8503 problem-cluster result as a page-oriented risk matrix."""
    target_sku_id = _text(payload.get("target_sku_id"), "")
    top_n = max(2, min(int(payload.get("top_n") or 7), 12))
    min_similarity = max(0.0, min(float(payload.get("min_similarity") or 0.45), 1.0))
    recall_mode = _text(payload.get("recall_mode"), "nutrition_structure")
    engine_config = dict(_db_config())
    engine_config.pop("cursorclass", None)
    b2b.similarity_recall.DB_CONFIG.update(engine_config)
    with _connection() as conn:
        if not target_sku_id:
            target_sku_id = b2b.fetch_default_target_sku_id(conn) or ""
        # 本地 SKU 先尝试映射到 B2B 标准 sku_id
        b2b_sku_id = _resolve_b2b_sku_id(target_sku_id)
        try:
            result = b2b.analyze_by_target_sku(conn, b2b_sku_id, top_n, min_similarity, recall_mode)
        except ValueError as exc:
            if str(exc).startswith("未找到目标 SKU"):
                return {
                    "ok": False,
                    "error": f"本地 SKU「{target_sku_id}」尚未在 B2B 标准库中找到对应记录，无法运行风险召回。",
                    "target": {"sku_id": target_sku_id, "brand": "", "product_name": target_sku_id},
                }
            raise
        target = result.get("target_sku") or {}
        candidates = result.get("product_search_result") or []
        shown = candidates[:2]
        matrix_products = [target, *shown]
        feedback = b2b.fetch_feedback_for_skus(conn, [row.get("sku_id") for row in matrix_products if row.get("sku_id")])
        feedback_map = {row.get("sku_id"): row for row in feedback}
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT sku_id FROM {b2b.B2B_TAG_TABLE}")
            all_sku_ids = [row["sku_id"] for row in cursor.fetchall()]
        market_clusters = b2b.cluster_feedback_problems(b2b.fetch_feedback_for_skus(conn, all_sku_ids))
        conn.commit()

    field_map = {
        "黑下巴": "black_chin_rate", "不吃/适口性差": "palatability_negative_rate",
        "软便": "soft_stool_rate", "粉多/碎渣": "dust_feedback_rate",
        "呕吐": "vomiting_rate", "油腻/碗油": "oily_feedback_rate",
        "换批不稳定": "batch_inconsistency_rate", "泪痕": "tear_stain_rate",
    }
    clusters = [row for row in (result.get("similar_product_problem_clusters") or []) if row.get("problem") != "客诉"][:5]
    baseline_map = {row.get("problem"): row.get("avg_rate") for row in market_clusters}
    risk_names = [row.get("problem") for row in clusters]
    matrix = []
    for index, product in enumerate(matrix_products):
        values = feedback_map.get(product.get("sku_id"), {})
        matrix.append({
            "type": "目标 SKU" if index == 0 else "相似召回",
            "sku_id": product.get("sku_id"), "brand": _text(product.get("brand"), "未知品牌"),
            "product_name": _text(product.get("product_name"), product.get("sku_id") or "未知产品"),
            "risks": {name: values.get(field_map.get(name, ""), 0) for name in risk_names},
        })

    summary = []
    for row in clusters:
        rate = float(row.get("avg_rate") or 0)
        baseline = float(baseline_map.get(row.get("problem")) or 0)
        if rate >= .65: level = "高风险"
        elif rate >= .45: level = "中高"
        elif rate >= .25: level = "中"
        else: level = "低"
        summary.append({
            "problem": row.get("problem"), "weighted_rate": rate, "market_baseline": baseline,
            "risk_level": level, "affected_sku_count": row.get("affected_sku_count"),
            "total_sku_count": row.get("total_sku_count"),
            "description": f"相似产品中 {row.get('affected_sku_count', 0)} / {row.get('total_sku_count', 0)} 款出现该风险信号",
        })

    cards = []
    for row in shown:
        shared = _tags(row.get("shared_ingredient_category_tags"), 2)
        differences = row.get("key_differences") or []
        difference = differences[0] if differences else "暂无显著差异"
        if isinstance(difference, dict):
            difference = difference.get("description") or difference.get("label") or str(difference)
        cards.append({
            "sku_id": row.get("sku_id"), "brand": row.get("brand"), "product_name": row.get("product_name"),
            "overall_similarity": row.get("overall_similarity"), "nutrition_similarity": row.get("nutrition_similarity"),
            "shared_tags": shared, "difference": _text(difference),
        })
    return {
        "ok": True, "analysis_id": result.get("analysis_id"), "target": target,
        "recall_cards": cards, "remaining_count": max(0, len(candidates) - len(cards)),
        "risk_names": risk_names, "risk_matrix": matrix, "problem_summary": summary,
        "scope_note": "风险来自相似产品的风险宽表与问题聚类，是结构化风险信号，不代表目标 SKU 已发生真实问题。",
    }


def _classify_portrait(structs: list[dict[str, Any]]) -> str:
    """根据结构标签判断产品主画像类型"""
    protein_tags = [s.get("level2", "") for s in structs if s.get("level1") == "蛋白结构"]
    carb_tags = [s.get("level2", "") for s in structs if s.get("level1") == "碳水/淀粉结构"]

    if "多肉源结构" in protein_tags:
        if "豆类淀粉结构" in carb_tags:
            return "多肉源+豆类淀粉型"
        if any("谷物" in t for t in carb_tags):
            return "多肉源+谷物淀粉型"
        return "多肉源复合蛋白型"
    if any("鲜肉" in t or "肉粉" in t for t in protein_tags):
        return "鲜肉/肉粉复合蛋白型"
    return "其他蛋白结构型"


def _build_brand_portraits(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把产品画像聚合为品牌画像"""
    from collections import defaultdict

    brand_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in products:
        brand_groups[p.get("brand", "")].append(p)

    # 计算全局 P75 / P25 阈值，用于判断品牌级优势/短板
    all_rankings: dict[str, list[float]] = defaultdict(list)
    for p in products:
        for k, v in p.get("rankings", {}).items():
            if isinstance(v, (int, float)):
                all_rankings[k].append(float(v))
    p75 = {k: sorted(v)[int(len(v) * 0.75)] if v else 75.0 for k, v in all_rankings.items()}
    p25 = {k: sorted(v)[int(len(v) * 0.25)] if v else 25.0 for k, v in all_rankings.items()}

    brand_records = []
    for brand, items in sorted(brand_groups.items(), key=lambda x: len(x[1]), reverse=True):
        if not brand:
            continue

        # 品牌下主画像分布
        portrait_counts: dict[str, int] = defaultdict(int)
        for p in items:
            portrait_counts[p.get("portrait_name", "其他")] += 1
        top_portrait = max(portrait_counts.items(), key=lambda x: x[1])[0]

        # 核心结构标签：取出现频次最高的 level1→level2 组合（最多3个）
        struct_counts: dict[str, int] = defaultdict(int)
        for p in items:
            for s in p.get("structures", []):
                key = f"{s.get('level1', '')}→{s.get('level2', '')}"
                if key != "→":
                    struct_counts[key] += 1
        top_structs = sorted(struct_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        core_structures = [k for k, _ in top_structs]

        # 品牌平均模块分位
        module_vals: dict[str, list[float]] = defaultdict(list)
        for p in items:
            for k, v in p.get("rankings", {}).items():
                if isinstance(v, (int, float)):
                    module_vals[k].append(float(v))
        avg_rankings = {k: round(sum(v) / len(v), 1) for k, v in module_vals.items() if v}
        composite = round(sum(avg_rankings.values()) / len(avg_rankings), 1) if avg_rankings else 0.0

        # 品牌级优势/短板：平均分位对比全局 P75/P25
        brand_advantages = [k for k, v in avg_rankings.items() if v >= p75.get(k, 75.0)]
        brand_weaknesses = [k for k, v in avg_rankings.items() if v <= p25.get(k, 25.0)]

        brand_records.append({
            "brand": brand,
            "product_count": len(items),
            "main_portrait": top_portrait,
            "core_structures": core_structures,
            "avg_rankings": avg_rankings,
            "composite_percentile": composite,
            "advantages": brand_advantages,
            "weaknesses": brand_weaknesses,
        })

    return brand_records


def build_product_portrait(*, brand_type: str = "", has_portrait: str = "") -> dict[str, Any]:
    """基于 catfood_formula_profile 生成全网产品画像与品牌画像"""
    # 从 catfood_formula_profile 取数据
    sql = """
        SELECT formula_id, product_key, brand, product_name,
               structure_labels, market_rankings, advantage_tags, weakness_tags
        FROM protein_feature_platform.catfood_formula_profile
        WHERE structure_labels IS NOT NULL
    """
    with _connection() as conn, conn.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()

    # 解析并构建产品画像
    products = []
    for row in rows:
        structs = _json_parse(row.get("structure_labels")) or []
        rankings = _json_parse(row.get("market_rankings")) or {}
        advantages = _json_parse(row.get("advantage_tags")) or []
        weaknesses = _json_parse(row.get("weakness_tags")) or []

        portrait_type = _classify_portrait(structs)
        primary_tags = " | ".join([f"{s.get('level1','')}→{s.get('level2','')}" for s in structs[:4]])

        products.append({
            "formula_id": row["formula_id"],
            "sku_id": row.get("product_key") or f"formula_{row['formula_id']}",
            "brand": row.get("brand", ""),
            "product_name": row.get("product_name", ""),
            "has_portrait": "是",
            "portrait_name": portrait_type,
            "primary_tags": primary_tags,
            "structures": structs,
            "advantages": advantages,
            "weaknesses": weaknesses,
            "rankings": rankings,
        })

    # 按品牌类型过滤（简化处理：根据品牌名判断）
    if brand_type:
        products = [p for p in products if brand_type in p.get("brand", "")]

    # 统计主画像分布
    type_counts: dict[str, int] = {}
    for p in products:
        type_counts[p["portrait_name"]] = type_counts.get(p["portrait_name"], 0) + 1

    total = len(products) or 1
    type_distribution = [
        {"name": name, "count": count, "share": round(count * 100 / total, 2)}
        for name, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
    ]

    # 统计各模块优势分布（产品维度）
    advantage_counts: dict[str, int] = {}
    for p in products:
        for adv in p.get("advantages", []):
            advantage_counts[adv] = advantage_counts.get(adv, 0) + 1
    profile_distribution = [
        {"name": name, "count": count, "share": round(count * 100 / total, 2)}
        for name, count in sorted(advantage_counts.items(), key=lambda x: x[1], reverse=True)[:8]
    ]

    # 品牌画像聚合
    brand_records = _build_brand_portraits(products)

    brands = sorted({p["brand"] for p in products if p.get("brand")})

    return {
        "ok": True,
        "summary": {
            "product_count": len(products),
            "brand_count": len(brands),
            "profiled_count": len(products),
            "top_profile": type_distribution[0]["name"] if type_distribution else "暂无",
        },
        "filters": {"brand_types": ["进口品牌", "国产品牌"], "portrait_options": ["是"]},
        "distributions": {"types": type_distribution, "profiles": profile_distribution},
        "products": products,
        "brands": brand_records,
        "capabilities": [],
        "scope_note": "数据基于 catfood_formula_profile 表，包含结构标签、市场分位、优势/短板标签。品牌画像按品牌聚合产品数据。",
    }
