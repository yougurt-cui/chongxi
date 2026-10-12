"""Shared, formula-keyed nutrition, recall and evidence for every food flow.

Business scoring is separate from labelled nutrient amounts. Missing data stays
missing; an association or a risk signal is never a diagnosis.
"""
from __future__ import annotations

import json
import logging
import re
from math import isfinite

import pymysql
from app_config import get_mysql_config, get_feature_mysql_config

logger = logging.getLogger(__name__)
FEATURE_VERSION = "soft_v1"
NUTRIENTS = ("粗蛋白", "粗脂肪", "粗纤维", "水分", "钙", "总磷", "牛磺酸", "粗灰分")
SIMILARITY_WEIGHTS = {"protein_score": .20, "fat_score": .20, "carb_score": .20,
                      "fiber_score": .15, "prebiotic_score": .15, "antioxidant_score": .10}
FEATURE_LABELS = {"protein_quality": "蛋白质量", "protein_pressure": "蛋白结构压力",
                  "carb_pressure": "碳水结构负担", "fat_pressure": "脂肪结构负担",
                  "p_buffer": "肠道缓冲", "fiber_buffer": "纤维缓冲", "q_scfa": "菌群代谢支持",
                  "skin_protection": "皮肤支持", "antioxidant_score": "抗氧化支持",
                  "fat_regulation_score": "脂肪调节支持"}


def load_risks(formula_ids):
    ids = sorted({int(value) for value in formula_ids if value})
    if not ids:
        return {}
    try:
        with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cursor:
                marks = ','.join(['%s'] * len(ids))
                cursor.execute(f'''SELECT formula_id,score_model_version,reference_pool_version,current_pool_risk_level,main_reason_tags,calculated_at
                    FROM sku_risk_score_result WHERE formula_id IN ({marks}) AND (score_model_version LIKE 'SOFT_STOOL%%' OR score_model_version LIKE 'BLACK_CHIN%%') ORDER BY calculated_at DESC''', ids)
                rows = list(cursor.fetchall())
    except pymysql.MySQLError as error:
        logger.warning('guardian_risks_unavailable error=%s', type(error).__name__)
        return {}
    result, seen = {}, set()
    for row in rows:
        model = str(row.get('score_model_version') or '')
        kind = 'soft_stool' if model.startswith('SOFT_STOOL') else 'black_chin'
        key = (int(row['formula_id']), kind)
        if key in seen:
            continue
        seen.add(key)
        tags = row.get('main_reason_tags') or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except ValueError:
                tags = [tags]
        if not isinstance(tags, list):
            tags = []
        result.setdefault(key[0], []).append({'kind': kind, 'model_version': model, 'reference_pool_version': row.get('reference_pool_version'),
            'relative_risk_level': row.get('current_pool_risk_level'), 'reason_tags': [str(tag) for tag in tags[:5]],
            'note': '相对于模型参考池的结构风险，不是患病概率或病因。'})
    return result


def number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def compact(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value or "").lower())


def reference_ids(reference, options):
    if reference.get("formula_id"):
        return {int(reference["formula_id"])}
    if reference.get("product_id"):
        return {int(row["formula_id"]) for row in options if row.get("product_id") == reference["product_id"]}
    brand, name = compact(reference.get("brand")), compact(reference.get("product") or reference.get("product_name"))
    if not name:
        return set()
    return {int(row["formula_id"]) for row in options if (not brand or brand == compact(row.get("brand")))
            and name in {compact(row.get("product_name")), compact(row.get("standard_product_name")), compact(row.get("label"))}}


def excluded_history_ids(pet, history, options, confirmed_text=""):
    references = list(history) + [{"formula_id": pet.get("food_formula_id"),
                                  "brand": pet.get("food_brand"), "product": pet.get("food_product")}]
    ids = set().union(*(reference_ids(item, options) for item in references))
    product_ids = {row.get("product_id") for row in options if row["formula_id"] in ids and row.get("product_id")}
    ids.update(row["formula_id"] for row in options if row.get("product_id") in product_ids)
    text = compact(confirmed_text)
    if text:
        for row in options:
            label = compact(str(row.get("brand") or "") + str(row.get("standard_product_name") or row.get("product_name") or ""))
            if label and label in text:
                ids.add(row["formula_id"])
    return ids


def load_nutrition(formula_ids):
    """One bounded read, with no table initialisation or side effects."""
    ids = sorted({int(value) for value in formula_ids if value})
    if not ids:
        return {}
    try:
        with pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cursor:
                marks = ",".join(["%s"] * len(ids))
                cursor.execute(f"SELECT formula_id,metric_name,metric_value,metric_unit,basis,operator_symbol FROM product_guarantee WHERE formula_id IN ({marks})", ids)
                rows = list(cursor.fetchall())
                cursor.execute(f"SELECT formula_id,nutrition_json FROM catfood_standard_formula WHERE status='active' AND formula_id IN ({marks})", ids)
                for formula in cursor.fetchall():
                    raw = formula.get("nutrition_json") or {}
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    for item in raw.values():
                        if isinstance(item, dict):
                            rows.append({"formula_id": formula["formula_id"], "metric_name": item.get("metric_name"),
                                         "metric_value": item.get("value"), "metric_unit": item.get("unit"),
                                         "basis": item.get("basis"), "operator_symbol": item.get("operator")})
    except (pymysql.MySQLError, ValueError, TypeError) as error:
        logger.warning("guardian_nutrition_unavailable error=%s", type(error).__name__)
        return {}
    grouped = {}
    for row in rows:
        name = row.get("metric_name")
        if name not in NUTRIENTS or number(row.get("metric_value")) is None:
            continue
        if number(row['metric_value']) < 0 or (str(row.get('metric_unit') or '').replace('％', '%') == '%' and number(row['metric_value']) > 100):
            continue
        value = {"name": name, "value": number(row["metric_value"]), "unit": row.get("metric_unit") or None,
                 "basis": row.get("basis") or None, "operator": row.get("operator_symbol") or None,
                 "source": "product_guarantee/formula.nutrition_json"}
        value['unit'] = str(value['unit']).replace('％', '%').strip().lower() if value['unit'] else None
        value['operator'] = {'≥': '>=', '≤': '<='}.get(value['operator'], value['operator'])
        bucket = grouped.setdefault(int(row["formula_id"]), {}).setdefault(name, [])
        if value not in bucket:
            bucket.append(value)
    result = {}
    for formula_id, metrics in grouped.items():
        result[formula_id] = {name: values[0] if len(values) == 1 else
                             {"name": name, "value": None, "status": "conflicting", "note": "来源数值存在冲突，暂不用于比较"}
                             for name, values in metrics.items()}
    return result


def nutrient_comparison(old, new):
    items = []
    for name in NUTRIENTS:
        before, after = old.get(name), new.get(name)
        comparable = bool(before and after and before.get("value") is not None and after.get("value") is not None
                          and before.get("basis") and before.get("unit")
                          and all(before.get(k) == after.get(k) for k in ("basis", "unit", "operator"))
                          and before.get("operator") in {"=", ">=", "<=", "≥", "≤"})
        items.append({"name": name, "current": before, "candidate": after,
                      "delta": round(after["value"] - before["value"], 4) if comparable else None,
                      "status": "comparable_label" if comparable else "not_comparable",
                      "note": ("包装标示值差；上下限不代表实际含量或优劣" if comparable else "缺资料或单位/基准不同，不计算差值")})
    return items


def analyse_food(item, nutrients=None, feature_row=None, cautions=(), current=None, risks=None):
    metrics = (nutrients or {}).get(int(item["formula_id"]), {})
    features = {k: number(feature_row.get(k)) for k in FEATURE_LABELS if feature_row is not None and number(feature_row.get(k)) is not None}
    return {"formula_id": item["formula_id"], "nutrition": [metrics.get(name) or {"name": name, "value": None, "status": "missing"} for name in NUTRIENTS],
            'comparison_current_formula_id': current,
            "nutrition_status": "available" if metrics else "unavailable",
            "structure_features": features, "risk_signals": list(cautions),
            'risk_models': (risks or {}).get(int(item['formula_id']), []),
            "risk_status": "signals_available" if cautions or (risks or {}).get(int(item['formula_id'])) else "insufficient_evidence",
            "risk_note": "仅为现有资料的结构风险信号，不是患病概率；没有信号不等于没有风险。",
            "comparison": nutrient_comparison((nutrients or {}).get(current, {}), metrics) if current else []}


def recall_similar(db, anchor_id, candidate_ids, *, limit=40):
    """Shared six-dimensional distance, one version, keyed by canonical formula."""
    if not anchor_id:
        return {"status": "no_current_formula", "items": []}
    from sqlalchemy import text
    import pandas as pd
    try:
        frame = pd.read_sql(text("SELECT f.formula_id," + ",".join('f.' + name for name in SIMILARITY_WEIGHTS) +
                                 " FROM sku_feature_input f JOIN csv_labeling.catfood_standard_formula c ON c.formula_id=f.formula_id AND c.status='active' AND c.is_current=1 WHERE f.feature_version=:version AND f.formula_id IS NOT NULL ORDER BY f.created_at DESC,f.id DESC"),
                            db, params={"version": FEATURE_VERSION})
    except Exception as error:
        logger.warning("guardian_recall_unavailable error=%s", type(error).__name__)
        return {"status": "data_unavailable", "items": []}
    frame = frame.drop_duplicates("formula_id")
    anchor = frame[frame["formula_id"] == anchor_id]
    if anchor.empty:
        return {"status": "current_features_missing", "items": []}
    anchor = anchor.iloc[0]
    fields = [key for key in SIMILARITY_WEIGHTS if number(anchor.get(key)) is not None]
    if len(fields) < 4:
        return {"status": "current_features_incomplete", "items": []}
    ranges = {key: max(float(pd.to_numeric(frame[key], errors="coerce").max() - pd.to_numeric(frame[key], errors="coerce").min()), 1.0) for key in fields}
    matches = []
    for _, row in frame[frame["formula_id"].isin(candidate_ids)].iterrows():
        if row["formula_id"] == anchor_id or any(number(row.get(k)) is None for k in fields):
            continue
        distance = sum(SIMILARITY_WEIGHTS[k] * min(1, abs(float(row[k]) - float(anchor[k])) / ranges[k]) for k in fields) / sum(SIMILARITY_WEIGHTS[k] for k in fields)
        matches.append({"formula_id": int(row["formula_id"]), "similarity": round(1 - distance, 4), "fields": fields})
    matches.sort(key=lambda row: (-row["similarity"], row["formula_id"]))
    return {"status": "ready" if matches else "no_candidates", "feature_version": FEATURE_VERSION,
            "current_formula_id": anchor_id, "items": matches[:limit],
            'source_dimensions': {'protein_score': 'protein_structure_score', 'fiber_score': 'p_total_score', 'prebiotic_score': 'q_feed', 'antioxidant_score': 'fat_regulation_score'},
            "note": "结构相似度用于召回，不等于适配度或健康效果；量化表的字段别名不作为同名营养证据。"}


def comparison_reasons(current_row, candidate_row, goal):
    fields = {"soft_stool": ("p_buffer", "fiber_buffer", "carb_pressure"),
              "black_chin": ("skin_protection", "fat_pressure"), "weight_loss": ("fat_pressure", "carb_pressure"),
              "weight_gain": ("protein_quality", "protein_pressure"),
              "coat_health": ("skin_protection", "fat_regulation_score", "antioxidant_score")}.get(goal, ("protein_quality", "p_buffer"))
    result = []
    for field in fields:
        before, after = number(current_row.get(field)), number(candidate_row.get(field))
        if before is None or after is None or before == after:
            continue
        result.append(f"相较当前粮，{FEATURE_LABELS[field]}参考分{before:g}→{after:g}（结构评分，不是营养含量）")
    return result


def nutrition_text(analysis):
    items = [row for row in (analysis or {}).get('nutrition', []) if row.get('value') is not None]
    if not items:
        return '营养标示资料暂缺，不能据此判断营养高低。'
    return '包装营养标示：' + '、'.join(f"{row['name']}{row.get('operator') or ''}{row['value']:g}{row.get('unit') or ''}" for row in items[:3]) + '（标签上下限，非实测含量）。'


def comparison_text(analysis):
    pairs = []
    for item in (analysis or {}).get('comparison') or []:
        before, after = item.get('current') or {}, item.get('candidate') or {}
        if before.get('value') is None or after.get('value') is None:
            continue
        def label(row):
            return f"{row.get('operator') or ''}{row['value']:g}{row.get('unit') or ''}"
        pairs.append(item['name'] + '：原粮' + label(before) + ' → 目标' + label(after) + ('（同基准标签对照）' if item['status'] == 'comparable_label' else '（基准未统一，不能直接计算差值）'))
    return '\n'.join(pairs[:3]) if pairs else '原粮或目标营养资料不足，暂不能做数值对比。'


def recommendation_text(recommendation):
    lines = []
    for index, item in enumerate(recommendation.get('items') or [], 1):
        label = item.get('label') or ' '.join(filter(None, [item.get('brand'), item.get('product_name')]))
        lines.append(f"{index}. {label}")
        lines.append('推荐理由：' + '；'.join((item.get('strengths') or ['按已选诉求匹配现有评分资料'])[:2]))
        lines.append(nutrition_text(item.get('analysis')))
        if (item.get('analysis') or {}).get('comparison'):
            lines.append(comparison_text(item['analysis']))
        if item.get('current_food_reasons'):
            lines.append(item['current_food_reasons'][0])
        if item.get('history_reasons'):
            lines.append(item['history_reasons'][0])
        lines.append('需留意：' + '；'.join((item.get('cautions') or ['风险资料有限，不能视为无风险'])[:2]))
        for risk in (item.get('analysis') or {}).get('risk_models', [])[:2]:
            name = '软便' if risk['kind'] == 'soft_stool' else '黑下巴'
            lines.append(name + '结构风险参考：' + str(risk.get('relative_risk_level') or '等级暂无') + '（非患病概率）。')
    if recommendation.get('missing_assessment_fields'):
        lines.append('本次未评估：' + '、'.join(FEATURE_LABELS.get(name, name) for name in recommendation['missing_assessment_fields']) + '；这些缺项未按默认分计入，也未认定已通过风险筛选。')
    if recommendation.get('recall', {}).get('status') not in ('not_required', 'ready', None):
        lines.append('当前粮缺少可核验的相似度资料，这次改按诉求筛选候选。')
    return '\n'.join(lines)
