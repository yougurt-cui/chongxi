"""Read-only aggregates for the brand growth engine demand dashboard."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

import pymysql

from app_config import get_mysql_config


def _split(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor)


def _experience_connect():
    return pymysql.connect(
        **get_mysql_config(database="csv_labeling"),
        cursorclass=pymysql.cursors.DictCursor,
    )


def _rows(conn, sql: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def _top(counter: Counter, limit: int = 10) -> list[dict[str, Any]]:
    total = sum(counter.values()) or 1
    return [
        {"name": name, "count": count, "share": round(count * 100 / total, 1)}
        for name, count in counter.most_common(limit)
    ]


def build_demand_dashboard() -> dict[str, Any]:
    # Need / Decision / Switch are comment-label tables in csv_labeling.
    # Other brand-growth aggregates below remain in the feature database.
    conn = _experience_connect()
    try:
        need_rows = _rows(conn, """
            SELECT need_life_stage, need_breed, health_primary, health_secondary,
                   need_detail_labeled, labeled_at, source_comment_time
            FROM catfood_need_comment_labels
        """)
        decision_rows = _rows(conn, """
            SELECT decision_factor_primary, decision_factor_secondary, decision_result, labeled_at
            FROM catfood_decision_comment_labels
        """)
        switch_rows = _rows(conn, """
            SELECT switch_from_brand, switch_to_brand, switch_reason_primary,
                   switch_reason_secondary, switch_status, labeled_at
            FROM catfood_switch_comment_labels
        """)
    finally:
        conn.close()

    health = Counter()
    stages = Counter()
    breeds = Counter()
    stage_health: dict[str, Counter] = {}
    breed_health: dict[str, Counter] = {}
    breed_stage: dict[str, Counter] = {}
    health_linked = Counter()
    stage_linked = Counter()
    breed_linked = Counter()
    today = date.today()
    recent_start = today - timedelta(days=365)
    prev_start = today - timedelta(days=730)
    health_recent = Counter()
    health_prev = Counter()
    stage_recent = Counter()
    stage_prev = Counter()
    breed_recent = Counter()
    breed_prev = Counter()
    detailed = 0
    latest = []
    for row in need_rows:
        detailed += int(row.get("need_detail_labeled") or 0)
        latest.append(row.get("labeled_at"))
        secondary = [x.split(">", 1)[-1] for x in _split(row.get("health_secondary"))]
        life = _split(row.get("need_life_stage"))
        breed = _split(row.get("need_breed"))
        health.update(secondary)
        stages.update(life)
        breeds.update(breed)
        t = row.get("source_comment_time")
        if t is not None:
            if recent_start <= t < today:
                health_recent.update(secondary)
                stage_recent.update(life)
                breed_recent.update(breed)
            elif prev_start <= t < recent_start:
                health_prev.update(secondary)
                stage_prev.update(life)
                breed_prev.update(breed)
        if life or breed:
            health_linked.update(secondary)
        if secondary or breed:
            stage_linked.update(life)
        if secondary or life:
            breed_linked.update(breed)
        for label in life:
            stage_health.setdefault(label, Counter()).update(secondary)
        for label in breed:
            breed_health.setdefault(label, Counter()).update(secondary)
            breed_stage.setdefault(label, Counter()).update(life)

    factor_primary = Counter()
    factor_secondary = Counter()
    results = Counter()
    factor_result = Counter()
    for row in decision_rows:
        latest.append(row.get("labeled_at"))
        primary_labels = _split(row.get("decision_factor_primary"))
        result = str(row.get("decision_result") or "未决")
        factor_primary.update(primary_labels)
        factor_secondary.update(x.split(">", 1)[-1] for x in _split(row.get("decision_factor_secondary")))
        results.update([result])
        factor_result.update((label, result) for label in primary_labels)

    paths = Counter()
    reasons = Counter()
    statuses = Counter()
    complete_paths = 0
    path_coverage = Counter()
    path_reason_map: dict[tuple[str, str], Counter] = {}
    for row in switch_rows:
        latest.append(row.get("labeled_at"))
        source = str(row.get("switch_from_brand") or "").strip()
        target = str(row.get("switch_to_brand") or "").strip()
        if source and target and source != target:
            paths[(source, target)] += 1
            complete_paths += 1
            path_coverage["完整路径"] += 1
            row_reasons = _split(row.get("switch_reason_primary"))
            path_reason_map.setdefault((source, target), Counter()).update(row_reasons)
        elif source and target:
            path_coverage["同品牌提及"] += 1
        elif source:
            path_coverage["仅识别原品牌"] += 1
        elif target:
            path_coverage["仅识别目标品牌"] += 1
        else:
            path_coverage["未识别品牌路径"] += 1
        reasons.update(_split(row.get("switch_reason_primary")))
        statuses.update([str(row.get("switch_status") or "准备迁移")])

    top_health_names = [x[0] for x in health.most_common(6)]
    def matrix(source: dict[str, Counter], labels: Counter, row_limit: int = 5):
        output = []
        for label, total in labels.most_common(row_limit):
            output.append({
                "name": label,
                "total": total,
                "values": [source.get(label, Counter()).get(item, 0) for item in top_health_names],
            })
        return output

    path_total = sum(paths.values()) or 1
    def bubble_data(counter: Counter, recent: Counter, prev: Counter, limit: int = 10):
        result = []
        for name, count in counter.most_common(limit):
            rc = recent.get(name, 0)
            pc = prev.get(name, 0)
            if pc > 0:
                growth = round((rc - pc) * 100 / pc, 1)
            elif rc > 0:
                growth = None
            else:
                growth = 0
            result.append({
                "name": name,
                "count": count,
                "share": round(count * 100 / (sum(counter.values()) or 1), 1),
                "growth": growth,
            })
        return result

    return {
        "updated_at": max((x for x in latest if x), default=None),
        "counts": {"need": len(need_rows), "decision": len(decision_rows), "switch": len(switch_rows)},
        "needs": {
            "detail_rate": round(detailed * 100 / (len(need_rows) or 1), 1),
            "health": _top(health, 12), "life_stages": _top(stages, 8), "breeds": _top(breeds, 8),
            "bubble_sets": {
                "health": bubble_data(health, health_recent, health_prev, 10),
                "stage": bubble_data(stages, stage_recent, stage_prev, 10),
                "breed": bubble_data(breeds, breed_recent, breed_prev, 10),
            },
            "matrix_columns": top_health_names,
            "stage_matrix": matrix(stage_health, stages), "breed_matrix": matrix(breed_health, breeds),
            "breed_stage_columns": [x[0] for x in stages.most_common(5)],
            "breed_stage_matrix": [
                {"name": label, "total": total,
                 "values": [breed_stage.get(label, Counter()).get(stage, 0)
                            for stage, _ in stages.most_common(5)]}
                for label, total in breeds.most_common(5)
            ],
        },
        "decisions": {
            "factors": _top(factor_primary, 8), "factor_details": _top(factor_secondary, 10),
            "results": _top(results, 8),
            "flows": [
                {"from": source, "to": target, "count": count}
                for (source, target), count in factor_result.most_common()
            ],
        },
        "switches": {
            "complete_paths": complete_paths,
            "complete_path_rate": round(complete_paths * 100 / (len(switch_rows) or 1), 1),
            "path_coverage": _top(path_coverage, 5),
            "paths": [
                {"from": a, "to": b, "count": n, "share": round(n * 100 / path_total, 1)}
                for (a, b), n in paths.most_common(12)
            ],
            "path_reasons": [
                {
                    "from": a, "to": b,
                    "reasons": [
                        {"name": r, "count": c, "share": round(c * 100 / (sum(path_reason_map.get((a, b), Counter()).values()) or 1), 1)}
                        for r, c in path_reason_map.get((a, b), Counter()).most_common(3)
                    ],
                }
                for (a, b), n in paths.most_common(8)
            ],
            "reasons": _top(reasons, 8), "statuses": _top(statuses, 5),
        },
    }


def build_experience_demand_insight(selected_symptom: str | None = None) -> dict[str, Any]:
    """Build a disease insight only from catfood_experience_comment_labels.

    Product coverage is intentionally not returned: this table has no SKU or
    formula key.  ``detail_coverage_rate`` is the honest replacement metric.
    """
    conn = _experience_connect()
    try:
        symptom_rows = _rows(conn, """
            SELECT primary_symptom AS symptom, COUNT(*) AS comment_count
            FROM catfood_experience_comment_labels
            WHERE primary_symptom <> ''
            GROUP BY primary_symptom
            ORDER BY comment_count DESC, primary_symptom
        """)
        symptoms = [str(row["symptom"]) for row in symptom_rows]
        symptom = selected_symptom if selected_symptom in symptoms else (
            "软便/拉稀" if "软便/拉稀" in symptoms else (symptoms[0] if symptoms else "")
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source_comment_id, comment_text, primary_symptom_primary,
                       secondary_symptom, secondary_onset, secondary_outcome,
                       exp_matched_keywords, exp_detail_labeled, labeled_at
                FROM catfood_experience_comment_labels
                WHERE primary_symptom = %s
            """, (symptom,))
            rows = list(cur.fetchall())
            cur.execute("""
                SELECT YEAR(STR_TO_DATE(s.source_comment_time, '%%Y-%%m-%%d')) AS year_value,
                       COUNT(*) AS comment_count
                FROM catfood_experience_comment_labels e
                JOIN catfood_choice_comments_filtered_v2 s ON s.id = e.source_comment_id
                WHERE e.primary_symptom = %s
                  AND s.source_comment_time REGEXP '^20[0-9]{2}-[0-9]{2}-[0-9]{2}'
                GROUP BY year_value ORDER BY year_value
            """, (symptom,))
            yearly = list(cur.fetchall())
    finally:
        conn.close()

    total = len(rows) or 1
    detailed = sum(int(row.get("exp_detail_labeled") or 0) for row in rows)
    secondary = Counter()
    outcomes: dict[str, Counter] = {}
    onset: dict[str, Counter] = {}
    improvement_signal = 0
    unresolved_signal = 0
    scenario_counts = Counter()
    examples: dict[str, list[str]] = {}
    for row in rows:
        text = str(row.get("comment_text") or "")
        matched = str(row.get("exp_matched_keywords") or "")
        if "效果:改善:" in matched:
            improvement_signal += 1
        if "效果:持续:" in matched or "效果:加重:" in matched:
            unresolved_signal += 1
        if any(word in text for word in ("换粮", "换了", "换成", "过渡粮", "过渡换")):
            scenario_counts["换粮适应"] += 1
            examples.setdefault("换粮适应", []).append(text[:90])
        if any(word in text for word in ("长期", "一直", "经常", "反复", "平时")):
            scenario_counts["长期敏感"] += 1
            examples.setdefault("长期敏感", []).append(text[:90])
        for label in _split(row.get("secondary_symptom")):
            secondary[label] += 1
            examples.setdefault(label, []).append(text[:90])
        for item in _split(row.get("secondary_outcome")):
            if ":" in item:
                label, value = item.split(":", 1)
                outcomes.setdefault(label, Counter())[value] += 1
        for item in _split(row.get("secondary_onset")):
            if ":" in item:
                label, value = item.split(":", 1)
                onset.setdefault(label, Counter())[value] += 1

    evidence_total = improvement_signal + unresolved_signal
    improvement_rate = improvement_signal / evidence_total if evidence_total else None
    years = {int(row["year_value"]): int(row["comment_count"]) for row in yearly if row.get("year_value")}
    complete_years = sorted(year for year in years if year < 2026)
    yoy = None
    yoy_years = []
    if len(complete_years) >= 2:
        previous, current = complete_years[-2:]
        yoy_years = [previous, current]
        if years[previous]:
            yoy = round((years[current] - years[previous]) * 100 / years[previous], 1)

    pain_points = []
    for rank, (label, count) in enumerate(secondary.most_common(4), 1):
        outcome_counter = outcomes.get(label, Counter())
        resolved = outcome_counter.get("改善", 0)
        unresolved = outcome_counter.get("未改善", 0) + outcome_counter.get("持续", 0) + outcome_counter.get("加重", 0)
        outcome_total = resolved + unresolved
        pain_points.append({
            "rank": rank, "name": label, "count": count,
            "cooccurrence_rate": round(count * 100 / total, 1),
            "improvement_rate": round(resolved * 100 / outcome_total, 1) if outcome_total else None,
            "unresolved_count": unresolved,
            "top_onset": onset.get(label, Counter()).most_common(1)[0][0] if onset.get(label) else "未知",
        })

    first_pain = pain_points[0]["name"] if pain_points else "伴随问题"
    second_pain = pain_points[1]["name"] if len(pain_points) > 1 else first_pain
    scene_defs = [
        ("换粮适应失败", "新粮切换场景", "换粮后问题反复，用户需要更明确的过渡期承接。", scenario_counts["换粮适应"]),
        ("长期敏感状态", "日常长期管理", f"主需求长期存在，并常与{first_pain}共同出现。", scenario_counts["长期敏感"]),
        (f"{symptom} + {first_pain}", "复合问题", "单一功能表达难以覆盖用户的完整体验。", secondary.get(first_pain, 0)),
    ]
    scenes = [{"title": a, "tag": b, "description": c, "count": n} for a, b, c, n in scene_defs]
    opportunities = [
        {"code": "机会A", "title": f"换粮适应型{symptom}方案", "type": "新品机会", "audience": "换粮期敏感猫", "problem": f"换粮后{symptom}反复", "proposal": "围绕过渡期稳定与简化适应路径定义产品。"},
        {"code": "机会B", "title": f"{symptom} + {first_pain}联合管理", "type": "表达优化", "audience": "长期复合问题用户", "problem": f"{symptom}与{first_pain}共同出现", "proposal": "从泛功能词改写为用户可感知的联合结果。"},
        {"code": "机会C", "title": f"低负担体验型{symptom}产品", "type": "产品优化", "audience": f"{symptom}且伴随{second_pain}用户", "problem": f"改善主需求的同时仍出现{second_pain}", "proposal": "降低额外体验负担，并验证长期稳定性。"},
    ]
    return {
        "selected_symptom": symptom,
        "symptoms": symptom_rows,
        "updated_at": max((row.get("labeled_at") for row in rows), default=None),
        "main_need": {
            "category": rows[0].get("primary_symptom_primary") if rows else "",
            "comment_count": len(rows), "demand_level": "高" if len(rows) >= 1000 else "中",
            "trend": "持续上升" if yoy is not None and yoy > 0 else "波动 / 待观察",
            "yoy_change_pct": yoy, "yoy_years": yoy_years,
            "improvement_rate": round(improvement_rate * 100, 1) if improvement_rate is not None else None,
            "improvement_sample": evidence_total,
            "detail_coverage_rate": round(detailed * 100 / total, 1),
        },
        "pain_points": pain_points, "scenes": scenes, "opportunities": opportunities,
        "definitions": {
            "cooccurrence": "次病症出现次数 / 当前主病症评论数",
            "improvement": "含改善信号评论 /（含改善信号 + 持续或加重信号评论）",
            "coverage": "完成细粒度 Experience 标签的评论 / 当前主病症评论",
            "scope": "所有指标仅使用 csv_labeling.catfood_experience_comment_labels；场景和机会是基于标签共现的业务归纳，不作医疗或产品因果声明。",
        },
    }


def build_cross_demand_analysis() -> dict[str, Any]:
    """返回增长机会页的交叉需求 TOP10 和病症供需总览。"""
    # 该聚合表由 build_demand_cross_analysis.py 与需求标签一起写入
    # csv_labeling，不能随运行时 MYSQL_DATABASE 指向特征库。
    conn = _experience_connect()
    try:
        cross_rows = _rows(conn, """
            SELECT cross_rank, category_type, category_value, cross_demand,
                   recent_12m_count, prev_12m_count, yoy_change_pct,
                   product_coverage_count, product_total_count,
                   product_coverage_rate, improvement_rate, improvement_sample,
                   metric_scope, analysis_date
            FROM catfood_demand_cross_analysis
            WHERE category_type IN ('life_stage', 'breed')
              AND cross_rank IS NOT NULL
            ORDER BY cross_rank
            LIMIT 10
        """)
        disease_rows = _rows(conn, """
            SELECT health_primary, health_secondary, cross_demand,
                   disease_clue_count, improvement_rate, improvement_sample,
                   uncertain_sample, product_coverage_count, product_total_count,
                   product_coverage_rate, mentioned_brand_count,
                   matched_brand_count, metric_scope, analysis_date
            FROM catfood_demand_cross_analysis
            WHERE category_type = 'all'
              AND health_secondary NOT IN ('其他')
            ORDER BY disease_clue_count DESC, cross_demand
        """)
    finally:
        conn.close()

    def clean(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for field in ("yoy_change_pct", "product_coverage_rate", "improvement_rate"):
            if result.get(field) is not None:
                result[field] = float(result[field])
        if result.get("analysis_date") is not None:
            result["analysis_date"] = result["analysis_date"].isoformat()
        return result

    updated_at = max(
            (row.get("analysis_date") for row in cross_rows + disease_rows if row.get("analysis_date")),
            default=None,
        )
    return {
        "updated_at": updated_at.isoformat() if updated_at else None,
        "cross_top10": [clean(row) for row in cross_rows],
        "disease_overview": [clean(row) for row in disease_rows],
        "definitions": {
            "coverage": "病症提及品牌关联的本地有效产品数 / 本地全部有效产品数",
            "improvement": "远程病症线索中改善 / (改善 + 加重)",
            "scope": "改善率和产品覆盖率为病症级指标；需求量和同比为年龄/品种交叉指标",
        },
    }


def build_disease_representatives(selected_symptom: str | None = None) -> dict[str, Any]:
    """病症需求洞察：需求承接候选产品与代表原料。

    候选产品逻辑与竞品增长下拉框保持一致：
    1. 从正式 cat_disease_clues 取改善品牌（改善>加重、改善>=5、总>=10）
    2. 从 catfood_disease_representative_product 取这些品牌的代表产品
    3. 每个品牌只保留排名最高的 1 个产品
    4. 代表原料仅来自这些候选产品的配方原料
    """
    conn = _experience_connect()
    try:
        # 1. 获取所有可选症状（与候选产品表保持一致）
        symptom_rows = _rows(conn, """
            SELECT secondary_symptom, COUNT(*) product_count,
                   COUNT(DISTINCT brand_id) brand_count,
                   MAX(disease_clue_count) max_brand_clues,
                   MAX(generated_at) updated_at
            FROM catfood_disease_representative_product
            WHERE secondary_symptom <> '其他'
            GROUP BY secondary_symptom
            ORDER BY SUM(disease_clue_count) DESC, secondary_symptom
        """)
        symptoms = [str(row["secondary_symptom"]) for row in symptom_rows]
        symptom = selected_symptom if selected_symptom in symptoms else (
            "软便/拉稀" if "软便/拉稀" in symptoms else (symptoms[0] if symptoms else "")
        )

        # 2. 按竞品增长下拉框逻辑取改善品牌
        with conn.cursor() as cur:
            cur.execute("""
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
                LIMIT 50
            """, (symptom, f"%{symptom}%", f"%{symptom}%"))
            brand_rows = list(cur.fetchall())
        qualified_brands = {row["brand"]: row for row in brand_rows}

        # 3. 取这些品牌的代表产品，并按品牌去重保留排名第一的产品
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.product_rank,p.brand_id,p.brand_name,p.product_id,p.product_name,
                       p.formula_id,p.life_stage,p.product_type,p.disease_clue_count,
                       p.improvement_count,p.worsening_count,p.uncertain_count,
                       p.improvement_rate,p.improvement_sample,p.wilson_lower_bound,
                       p.representative_score,p.evidence_scope
                FROM catfood_disease_representative_product p
                WHERE p.secondary_symptom=%s
                  AND p.brand_name IN %s
                ORDER BY p.product_rank
            """, (symptom, tuple(qualified_brands.keys()) if qualified_brands else ("",)))
            all_products = list(cur.fetchall())

        # 每个品牌只保留一个产品
        seen_brands: set[int] = set()
        products = []
        for row in all_products:
            brand_id = row["brand_id"]
            if brand_id not in seen_brands:
                seen_brands.add(brand_id)
                products.append(row)
        products = products[:12]
        selected_product_ids = [row["product_id"] for row in products]

        # 4. 代表原料仅来自候选产品
        candidate_ingredients = []
        if selected_product_ids:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT standard_ingredient_id, standard_name,
                           ingredient_family, source_type, animal_source, primary_nutrition_role,
                           COUNT(DISTINCT brand_id) AS supporting_brand_count,
                           COUNT(DISTINCT product_id) AS supporting_product_count,
                           COUNT(DISTINCT formula_id) AS supporting_formula_count,
                           SUM(disease_clue_count) AS supporting_clue_count,
                           AVG(improvement_rate) AS improvement_rate,
                           SUM(improvement_count) AS improvement_sample,
                           AVG(position) AS average_position
                    FROM catfood_formula_ingredient_disease
                    WHERE secondary_symptom=%s
                      AND product_id IN %s
                    GROUP BY standard_ingredient_id, standard_name, ingredient_family,
                             source_type, animal_source, primary_nutrition_role
                    ORDER BY supporting_clue_count DESC, supporting_brand_count DESC
                    LIMIT 500
                """, (symptom, tuple(selected_product_ids)))
                candidate_ingredients = list(cur.fetchall())

        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT brand_id) brand_count,
                       COUNT(DISTINCT product_id) product_count,
                       COUNT(DISTINCT formula_id) formula_count,
                       COUNT(DISTINCT standard_ingredient_id) ingredient_count,
                       MAX(generated_at) updated_at
                FROM catfood_formula_ingredient_disease
                WHERE secondary_symptom=%s
            """, (symptom,))
            summary = cur.fetchone() or {}
    finally:
        conn.close()

    def clean(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key, value in list(result.items()):
            if hasattr(value, "as_integer_ratio") and not isinstance(value, (int, float)):
                result[key] = float(value)
            elif hasattr(value, "isoformat"):
                result[key] = value.isoformat(sep=" ")
        return result

    # 按营养角色分组候选原料
    ingredient_groups: dict[str, list[dict[str, Any]]] = {}
    for row in candidate_ingredients:
        role = str(row.get("primary_nutrition_role") or "未分类")
        ingredient_groups.setdefault(role, []).append(row)

    grouped_output = [
        {
            "role": role,
            "ingredient_count": len(rows),
            "max_brand_coverage": max((int(row.get("supporting_brand_count") or 0) for row in rows), default=0),
            "ingredients": [clean(row) for row in rows[:8]],
        }
        for role, rows in ingredient_groups.items()
    ]
    grouped_output.sort(key=lambda group: -len(group["ingredients"]))

    return {
        "selected_symptom": symptom,
        "symptoms": [clean(row) for row in symptom_rows],
        "summary": clean(summary),
        "products": [clean(row) for row in products],
        "ingredient_groups": [],  # 高关联营养角色模块已移除
        "candidate_ingredient_groups": grouped_output,
        "definitions": {
            "product": "候选产品来自该病症改善品牌（改善>加重、改善≥5条、总≥10条），每个品牌保留排名最高的1个代表产品。",
            "ingredient": "代表原料仅来自上述候选产品的配方原料，按营养角色分组，表示共现代表性，不表示因果功效。",
        },
    }
