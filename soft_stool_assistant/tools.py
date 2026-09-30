"""本地数据 Tool 层（真实 MySQL 版 · v3 整合版）。

这一版把 v2 的 Mock 数据层整体换成真实数据库，**对外函数签名与 v2 完全一致**，
所以 v2 的 pipeline.py 不用改逻辑就能直接用真实数据跑。

v2 Mock → 真实表
-----------------
| v2 Mock              | 真实来源                                                      |
|----------------------|---------------------------------------------------------------|
| `PET_DB`             | `csv_labeling.miniprogram_cat_profile`                          |
| `DIET_DB`（基础饮食）  | `cat_profile.food_*` + `miniprogram_food_change_intent`         |
| `PRODUCT_DB`         | `protein_feature_platform.catfood_product_catalog`              |
|                      | + `csv_labeling.product_guarantee`（粗蛋白/粗脂肪/粗纤维）        |
|                      | + `protein_feature_platform.protein_source_aggregate`（蛋白来源）|
|                      | + `csv_labeling.catfood_feature_biotic_labels`（益生元）         |
| （v2 没有的健康信号）   | `protein_feature_platform.sku_soft_stool_compound_risk`         |
|                      | + `sku_symptom_probability_wide` + `brand_soft_stool_stats`      |

v2 Pipeline 只依赖 4 个函数：
    build_baseline_diet_profile / get_product_by_name
    compare_diets / build_manual_old_new_compare
其余（get_product_detail / search_products / list_pets / 风险表）供 Demo 与扩展使用。

关联键
------
- `product_key`（形如 `百利||高蛋白`）是 catalog 与所有特征表之间的通用 join 键；
- `formula_id` 在 `protein_feature_platform` 内部互通，且与
  `csv_labeling.catfood_standard_formula.formula_id` 同属一个 ID 空间；
- 营养三指标在 `product_guarantee` 里是行式存储，需按 `metric_name` 透视。
"""

import json
import re
from datetime import datetime

from .db import query_all, query_one

# =========================================================
# 表名常量
# =========================================================
T_CAT_PROFILE = "csv_labeling.miniprogram_cat_profile"
T_FOOD_CHANGE = "csv_labeling.miniprogram_food_change_intent"
T_PRODUCT_CATALOG = "protein_feature_platform.catfood_product_catalog"
T_GUARANTEE = "csv_labeling.product_guarantee"
T_PROTEIN_SOURCE = "protein_feature_platform.protein_source_aggregate"
T_FAT_FEATURE = "protein_feature_platform.catfood_fat_material_features"
T_BIOTIC = "csv_labeling.catfood_feature_biotic_labels"
T_SOFT_STOOL_RISK = "protein_feature_platform.sku_soft_stool_compound_risk"
T_SYMPTOM_PROB = "protein_feature_platform.sku_symptom_probability_wide"
T_BRAND_STOOL_STATS = "protein_feature_platform.brand_soft_stool_stats"

NUTRIENT_METRICS = ("粗蛋白", "粗脂肪", "粗纤维")

CATALOG_FIELDS = """
    catalog_key, product_key, standard_brand, product_name, raw_title,
    origin_type, brand_tier, price, price_bucket, food_taste, net_content,
    main_image_url, function_scores_json, function_tags_json, warning_tags_json,
    function_display_text, quality_flags_json, score_source_id
"""

PROFILE_FIELDS = """
    id, user_id, name, animal_type, breed, sex, neutered, birthday, age_text,
    age_months, weight_kg, allergies_json, diseases_json, symptoms_json,
    health_status_json, food_brand, food_product, food_product_id,
    food_formula_id, notes, created_at, updated_at
"""

# ---------------------------------------------------------
# Demo 别名：v2 README 测试用例里的 "A粮 / B粮 / C粮"
# 映射到库里真实存在的产品，方便沿用 v2 的对话脚本。
# 用 --no-demo-alias（app.py）可关闭。
# ---------------------------------------------------------
DEMO_PRODUCT_ALIAS = {
    "原粮A": "score:77",    # 天衡宝||三文鱼青豆五谷  蛋白30 脂肪12 纤维4.5
    "A粮": "score:77",
    "新粮B": "score:131",   # 百利||高蛋白            蛋白47 脂肪17 纤维3.0
    "B粮": "score:131",
    "低脂粮C": "score:83",  # 天衡宝||鹿肉青豆无谷    蛋白30 脂肪10 纤维5.0
    "C粮": "score:83",
}
DEMO_ALIAS_ENABLED = True


# =========================================================
# 小工具
# =========================================================
def _json_load(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed is not None else default


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_sources(text):
    """把「鸡、鸭、鱼」这类顿号/逗号分隔串拆成列表。"""
    if not text:
        return []
    raw = str(text).replace("，", ",").replace("、", ",").replace("／", "/")
    for sep in ("/", "|", ";", "；"):
        raw = raw.replace(sep, ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _top_level_label(value):
    """function_tags_json / warning_tags_json 里的 level 归一化。"""
    m = {"strong": "强", "medium": "中", "weak": "弱"}
    return m.get(str(value).lower(), value)


# =========================================================
# 宠物档案  ←  miniprogram_cat_profile
# =========================================================
def _row_to_pet(row: dict) -> dict:
    allergies = _json_load(row.get("allergies_json"), [])
    diseases = _json_load(row.get("diseases_json"), [])
    symptoms = _json_load(row.get("symptoms_json"), [])

    health_tags = []
    for tag in symptoms + diseases + allergies:
        tag = str(tag).strip() if tag is not None else ""
        if tag and tag not in health_tags:
            health_tags.append(tag)

    return {
        "pet_id": row.get("id"),
        "user_id": row.get("user_id"),
        "name": row.get("name"),
        "species": row.get("animal_type"),
        "breed": row.get("breed"),
        "sex": row.get("sex"),
        "neutered": row.get("neutered"),
        "age_text": row.get("age_text"),
        "age_months": row.get("age_months"),
        "weight_kg": _to_float(row.get("weight_kg")),
        "allergies": allergies,
        "diseases": diseases,
        "symptoms": symptoms,
        "health_tags": health_tags,
        "current_food": {
            "food_brand": row.get("food_brand"),
            "food_product": row.get("food_product"),
            "food_product_id": row.get("food_product_id"),
            "food_formula_id": row.get("food_formula_id"),
        },
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


def get_pet_profile(pet_id: str) -> dict:
    """按 cat_profile.id 读取宠物档案。"""
    row = query_one(
        f"select {PROFILE_FIELDS} from {T_CAT_PROFILE} "
        "where id = %s and status = 'active' and deleted_at is null",
        (pet_id,),
    )
    if not row:
        raise ValueError(f"pet_id not found in {T_CAT_PROFILE}: {pet_id}")
    return _row_to_pet(row)


def list_pets(limit: int = 50) -> list:
    """列出可用宠物（方便 Demo 选 pet_id）。"""
    rows = query_all(
        "select id, name, breed, animal_type, weight_kg, age_months, "
        "food_brand, food_product, food_formula_id "
        f"from {T_CAT_PROFILE} where status = 'active' and deleted_at is null "
        "order by updated_at desc limit %s",
        (limit,),
    )
    return [
        {
            "pet_id": r["id"],
            "name": r["name"],
            "breed": r.get("breed"),
            "species": r.get("animal_type"),
            "weight_kg": _to_float(r.get("weight_kg")),
            "age_months": r.get("age_months"),
            "food_brand": r.get("food_brand"),
            "food_product": r.get("food_product"),
            "has_profile_food": bool(r.get("food_brand") or r.get("food_product") or r.get("food_formula_id")),
        }
        for r in rows
    ]


# =========================================================
# 软便健康信号
# =========================================================
def get_product_soft_stool_risk(product_key: str) -> dict:
    """SKU 级软便复合风险 + 症状概率。"""
    if not product_key:
        return {}

    risk = query_one(
        f"""
        select soft_stool_brand_symptom_case_count, soft_stool_brand_symptom_ratio,
               soft_stool_symptom_probability, soft_stool_soft_stool_risk_index_value,
               soft_stool_soft_stool_risk_level, brand_total_disease_count
        from {T_SOFT_STOOL_RISK}
        where product_key = %s
        limit 1
        """,
        (product_key,),
    ) or {}

    prob = query_one(
        f"""
        select soft_stool_probability, soft_stool_raw_score, brand_soft_stool_ratio,
               fiber_support_norm, prebiotic_support_norm, fat_regulation_support_norm
        from {T_SYMPTOM_PROB}
        where product_key = %s
        limit 1
        """,
        (product_key,),
    ) or {}

    if not risk and not prob:
        return {}

    return {
        "risk_level": risk.get("soft_stool_soft_stool_risk_level"),
        "risk_index": _to_float(risk.get("soft_stool_soft_stool_risk_index_value")),
        "symptom_probability": _to_float(
            risk.get("soft_stool_symptom_probability") or prob.get("soft_stool_probability")
        ),
        "brand_case_count": _to_float(risk.get("soft_stool_brand_symptom_case_count")),
        "brand_symptom_ratio": _to_float(risk.get("soft_stool_brand_symptom_ratio")),
        "fiber_support_norm": _to_float(prob.get("fiber_support_norm")),
        "prebiotic_support_norm": _to_float(prob.get("prebiotic_support_norm")),
    }


def get_brand_soft_stool_stats(brand: str) -> dict:
    """品牌级软便加重比例。"""
    if not brand:
        return {}
    row = query_one(
        f"""
        select brand, total_clue_count, soft_stool_aggravation_count,
               soft_stool_aggravation_ratio
        from {T_BRAND_STOOL_STATS}
        where brand = %s
        limit 1
        """,
        (brand,),
    )
    if not row:
        return {}
    return {
        "brand": row["brand"],
        "total_clue_count": row.get("total_clue_count"),
        "aggravation_count": row.get("soft_stool_aggravation_count"),
        "aggravation_ratio": _to_float(row.get("soft_stool_aggravation_ratio")),
    }


# =========================================================
# 产品  ←  catalog + guarantee + protein_source + biotic
# =========================================================
def _catalog_by_ref(ref: str):
    """按 catalog_key / product_key / formula_id / 模糊名 找一行 catalog。"""
    base = f"select {CATALOG_FIELDS} from {T_PRODUCT_CATALOG}"
    ref = str(ref).strip()
    if DEMO_ALIAS_ENABLED and ref in DEMO_PRODUCT_ALIAS:
        ref = DEMO_PRODUCT_ALIAS[ref]
    if ref.startswith("formula:"):
        ref = ref.split(":", 1)[1]

    # 1) catalog_key 精确
    row = query_one(base + " where catalog_key = %s limit 1", (ref,))
    if row:
        return row

    # 2) product_key 精确
    row = query_one(base + " where product_key = %s limit 1", (ref,))
    if row:
        return row

    # 3) 纯数字：按 formula_id 经蛋白来源表桥接到 catalog
    if ref.isdigit():
        row = query_one(
            f"""
            select c.catalog_key, c.product_key, c.standard_brand, c.product_name,
                   c.raw_title, c.origin_type, c.brand_tier, c.price, c.price_bucket,
                   c.food_taste, c.net_content, c.main_image_url, c.function_scores_json,
                   c.function_tags_json, c.warning_tags_json, c.function_display_text,
                   c.quality_flags_json, c.score_source_id
            from {T_PRODUCT_CATALOG} c
            join {T_PROTEIN_SOURCE} a on c.product_key = a.product_key
            where a.formula_id = %s
            limit 1
            """,
            (int(ref),),
        )
        if row:
            return row

    # 4) 模糊：取最短的 product_key（最接近精确名）
    return query_one(
        base + " where product_key like %s order by char_length(product_key) asc limit 1",
        (f"%{ref}%",),
    )


def _catalog_by_name(product_name: str):
    """按用户口语名找 catalog（v2 get_product_by_name 的底层）。"""
    if not product_name:
        return None
    ref = str(product_name).strip()
    if not ref:
        return None
    if DEMO_ALIAS_ENABLED and ref in DEMO_PRODUCT_ALIAS:
        ref = DEMO_PRODUCT_ALIAS[ref]

    base = f"select {CATALOG_FIELDS} from {T_PRODUCT_CATALOG}"

    row = query_one(base + " where catalog_key = %s limit 1", (ref,))
    if row:
        return row
    row = query_one(base + " where product_key = %s limit 1", (ref,))
    if row:
        return row
    row = query_one(
        base + " where product_name = %s order by char_length(product_key) asc limit 1",
        (ref,),
    )
    if row:
        return row
    row = query_one(
        base + " where product_key like %s order by char_length(product_key) asc limit 1",
        (f"%{ref}%",),
    )
    if row:
        return row

    # 去掉分隔符再比："百利高蛋白" ↔ "百利||高蛋白"
    loose = re.sub(r"[\s|·\-—_/]+", "", ref)
    if len(loose) >= 2:
        row = query_one(
            base + " where replace(replace(product_key,'||',''),' ','') like %s "
            "order by char_length(product_key) asc limit 1",
            (f"%{loose}%",),
        )
        if row:
            return row

    # 品牌 / 产品名包含
    return query_one(
        base + " where product_name like %s or standard_brand like %s "
        "order by char_length(product_key) asc limit 1",
        (f"%{ref}%", f"%{ref}%"),
    )


def _nutrition_by_formula(formula_id) -> dict:
    """从 product_guarantee 透视出 粗蛋白 / 粗脂肪 / 粗纤维。"""
    if not formula_id:
        return {}
    rows = query_all(
        f"""
        select metric_name, metric_value, metric_unit, basis, operator_symbol
        from {T_GUARANTEE}
        where formula_id = %s
        """,
        (formula_id,),
    )
    out = {"metrics": {}, "basis": None}
    for r in rows:
        name = r.get("metric_name")
        out["metrics"][name] = _to_float(r.get("metric_value"))
        if name in NUTRIENT_METRICS and r.get("basis"):
            out["basis"] = r.get("basis")

    for cn, en in {"粗蛋白": "protein", "粗脂肪": "fat", "粗纤维": "fiber"}.items():
        out[en] = out["metrics"].get(cn)
    return out


def _build_product(catalog: dict, with_risk: bool = True) -> dict:
    """由一行 catalog 拼出完整产品画像（营养 + 蛋白来源 + 益生元 + 风险）。"""
    product_key = catalog["product_key"]

    source = query_one(
        f"""
        select formula_id, animal_sources, animal_source_level1_categories,
               animal_source_level2_sources, protein_source_details,
               primary_meat_source_type, secondary_meat_source_type,
               meat_source_complexity, plant_protein_interference,
               guarantee_crude_protein_value, profile_status
        from {T_PROTEIN_SOURCE}
        where product_key = %s
        limit 1
        """,
        (product_key,),
    ) or {}

    fat = query_one(
        f"""
        select fat_sources, fat_source_types, omega3_sources, omega6_sources,
               antioxidant_sources, guarantee_crude_fat_value
        from {T_FAT_FEATURE}
        where product_key = %s
        limit 1
        """,
        (product_key,),
    ) or {}

    biotic = query_one(
        f"""
        select biotic_structure, biotic_type, prebiotic_details, probiotic_details
        from {T_BIOTIC}
        where product_key = %s
        limit 1
        """,
        (product_key,),
    ) or {}

    formula_id = source.get("formula_id")
    nutrition = _nutrition_by_formula(formula_id)

    function_scores = _json_load(catalog.get("function_scores_json"), {})
    warning_tags = [
        {
            "tag": t.get("tag"),
            "level": _top_level_label(t.get("level")),
            "evidence": t.get("evidence") or [],
        }
        for t in _json_load(catalog.get("warning_tags_json"), [])
        if isinstance(t, dict)
    ]

    risk = get_product_soft_stool_risk(product_key) if with_risk else {}

    brand = catalog["standard_brand"]
    product_name = catalog["product_name"]
    display_name = product_name if (brand and brand in str(product_name)) else f"{brand} {product_name}"

    return {
        # ---- v2 PRODUCT_DB 兼容字段（pipeline 直接读这些）----
        "product_id": catalog["catalog_key"],
        "name": product_name,
        "display_name": display_name,
        "protein": nutrition.get("protein"),
        "fat": nutrition.get("fat"),
        "fiber": nutrition.get("fiber"),
        "protein_sources": _split_sources(source.get("animal_sources")),
        "prebiotics": _split_sources(biotic.get("prebiotic_details")),
        # ---- 真实库扩展字段 ----
        "product_key": product_key,
        "catalog_key": catalog["catalog_key"],
        "brand": brand,
        "formula_id": formula_id,
        "nutrition_basis": nutrition.get("basis"),
        "nutrition_metrics": nutrition.get("metrics", {}),
        "protein_source_detail": source.get("protein_source_details"),
        "protein_source_level1": source.get("animal_source_level1_categories"),
        "meat_source_type": source.get("primary_meat_source_type"),
        "meat_source_complexity": source.get("meat_source_complexity"),
        "plant_protein_interference": source.get("plant_protein_interference"),
        "fat_sources": _split_sources(fat.get("fat_sources")),
        "fat_source_types": fat.get("fat_source_types"),
        "omega3_sources": _split_sources(fat.get("omega3_sources")),
        "prebiotic_detail": biotic.get("prebiotic_details"),
        "biotic_structure": biotic.get("biotic_structure"),
        "function_scores": function_scores,
        "gut_friendly_score": function_scores.get("肠胃友好"),
        "function_display_text": catalog.get("function_display_text"),
        "warning_tags": warning_tags,
        "soft_stool_risk": risk,
        "origin_type": catalog.get("origin_type"),
        "brand_tier": catalog.get("brand_tier"),
        "price": _to_float(catalog.get("price")),
        "price_bucket": catalog.get("price_bucket"),
    }


def get_product_detail(product_ref, with_risk: bool = True):
    """
    product_ref 支持三种写法：
      - catalog_key，如 "score:131"
      - product_key，如 "百利||高蛋白"
      - formula_id，如 112 或 "formula:112"
    """
    if product_ref in (None, ""):
        return None
    catalog = _catalog_by_ref(product_ref)
    if not catalog:
        return None
    return _build_product(catalog, with_risk=with_risk)


def get_product_by_name(product_name: str):
    """按用户口语里的产品名解析成产品（v2 接口，Mock 版换真实库）。"""
    catalog = _catalog_by_name(product_name)
    if not catalog:
        return None
    return _build_product(catalog)


def search_products(keyword: str, limit: int = 15) -> list:
    """按品牌或产品名模糊搜索，用于 Demo 里挑产品。"""
    rows = query_all(
        f"""
        select catalog_key, product_key, standard_brand, product_name, function_display_text
        from {T_PRODUCT_CATALOG}
        where status = 'active'
          and (standard_brand like %s or product_name like %s or product_key like %s)
        order by char_length(product_key) asc
        limit %s
        """,
        (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit),
    )
    return [
        {
            "catalog_key": r["catalog_key"],
            "product_key": r["product_key"],
            "brand": r["standard_brand"],
            "name": r["product_name"],
            "display": r.get("function_display_text"),
        }
        for r in rows
    ]


# =========================================================
# 基础饮食（v2 的 DIET_DB 换真实库）
# =========================================================
def normalize_baseline_override(override):
    """
    统一基础饮食的注入口径，支持三种写法：
      {"baseline_food": "score:77", "ratio": 1.0}
      {"refs": ["score:77", "score:83"]}
      {"products": [{"ref": "score:77", "ratio": 1.0}, ...]}
    统一返回 [{"ref": ..., "ratio": ...}, ...]
    """
    if not override:
        return []

    if isinstance(override, (list, tuple)):
        return [{"ref": str(x), "ratio": 1.0} for x in override if x]

    if isinstance(override, dict):
        if override.get("products"):
            out = []
            for item in override["products"]:
                if isinstance(item, dict) and item.get("ref"):
                    out.append({"ref": item["ref"], "ratio": item.get("ratio", 1.0)})
                elif isinstance(item, str):
                    out.append({"ref": item, "ratio": 1.0})
            return out
        if override.get("refs"):
            return [{"ref": str(x), "ratio": 1.0} for x in override["refs"] if x]
        if override.get("baseline_food"):
            return [{"ref": override["baseline_food"], "ratio": override.get("ratio", 1.0)}]
    return []


def _profile_food_ref(pet: dict):
    """档案里配置的口粮 → 产品引用。"""
    food = pet.get("current_food") or {}
    if food.get("food_formula_id"):
        return f"formula:{food['food_formula_id']}", "cat_profile.food_formula_id"
    if food.get("food_product_id"):
        return food["food_product_id"], "cat_profile.food_product_id"
    if food.get("food_product"):
        return food["food_product"], "cat_profile.food_product"
    return None, None


def get_latest_food_change_intent(pet_id: str):
    """该宠物所属用户最近一次「换粮意图」记录（无法关联时返回 None）。"""
    try:
        pet = get_pet_profile(pet_id)
    except ValueError:
        return None

    user_id = pet.get("user_id")
    if not user_id:
        return None

    row = query_one(
        f"""
        select id, session_id, user_message, extracted_brand, extracted_product,
               matched_catalog_key, matched_brand, matched_product_name,
               match_status, model_result_json, created_at
        from {T_FOOD_CHANGE}
        where user_id = %s and is_food_change_intent = 1 and status = 'completed'
        order by created_at desc
        limit 1
        """,
        (user_id,),
    )
    if not row:
        return None

    model = _json_load(row.get("model_result_json"), {})
    current_food = model.get("current_food") or {}
    target_food = model.get("target_food") or {}

    return {
        "intent_id": row["id"],
        "user_message": row.get("user_message"),
        "match_status": row.get("match_status"),
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        # 原粮（换粮前）
        "previous_brand": current_food.get("brand") or row.get("extracted_brand"),
        "previous_product": current_food.get("product_name"),
        # 新粮（换粮后）
        "new_brand": target_food.get("brand") or row.get("matched_brand"),
        "new_product": target_food.get("product_name") or row.get("matched_product_name"),
        "new_product_ref": row.get("matched_catalog_key"),
    }


def get_baseline_diet(pet_id: str, override=None) -> dict:
    """
    读取「基础饮食」= 这只猫平时在吃什么（v2 的 DIET_DB）。

    优先级：override > cat_profile.food_* > 最近换粮意图里的原粮
    """
    pet = get_pet_profile(pet_id)

    if isinstance(override, dict) and override.get("disabled"):
        return {
            "has_diet_data": False,
            "diet_mode": None,
            "products": [],
            "source": "disabled",
            "unresolved_refs": [],
        }

    entries = []          # [{"ref": ..., "ratio": ...}]
    source = None

    ov = normalize_baseline_override(override)
    if ov:
        entries = ov
        source = "override"
    else:
        ref, src = _profile_food_ref(pet)
        if ref:
            entries = [{"ref": ref, "ratio": 1.0}]
            source = src
        else:
            intent = get_latest_food_change_intent(pet_id)
            if intent and intent.get("previous_brand"):
                entries = [{"ref": intent["previous_brand"], "ratio": 1.0}]
                source = "food_change_intent.previous_food"

    products = []
    unresolved = []
    for item in entries:
        detail = get_product_detail(item["ref"])
        if not detail:
            unresolved.append(str(item["ref"]))
            continue
        products.append({**detail, "ratio": item.get("ratio", 1.0)})

    has_data = len(products) > 0
    return {
        "has_diet_data": has_data,
        "diet_mode": ("single" if len(products) == 1 else "mixed") if has_data else None,
        "products": products,
        "source": source,
        "unresolved_refs": unresolved,
    }


def build_baseline_diet_profile(pet_id: str, override=None) -> dict:
    """
    把单粮 / 混喂整理成「基础饮食画像」（v2 接口，Mock 换真实库）。

    返回结构与 v2 一致（`has_diet_data` / `diet_mode` / `products` / `nutrition_summary`），
    另加 `detailed_products` / `source` / `notes` 供 Demo 展示数据来源。
    """
    diet = get_baseline_diet(pet_id, override=override)
    notes = []

    if not diet["has_diet_data"]:
        if diet.get("unresolved_refs"):
            notes.append("基础饮食引用的产品在 catalog 里没有匹配到：" + "、".join(diet["unresolved_refs"]))
        return {
            "has_diet_data": False,
            "diet_mode": None,
            "products": [],
            "detailed_products": [],
            "nutrition_summary": None,
            "source": diet.get("source"),
            "notes": notes,
        }

    products = diet["products"]

    # 只对三指标齐全的产品做加权，避免 None 污染均值
    usable = [p for p in products
              if p.get("protein") is not None and p.get("fat") is not None and p.get("fiber") is not None]
    skipped = [p["name"] for p in products if p not in usable]
    if skipped:
        notes.append("以下产品缺保证值数据，未计入营养汇总：" + "、".join(skipped))

    summary = None
    if usable:
        total_ratio = sum(p.get("ratio", 0) for p in usable) or 1.0
        summary = {
            "protein": round(sum(p["protein"] * p.get("ratio", 0) for p in usable) / total_ratio, 2),
            "fat": round(sum(p["fat"] * p.get("ratio", 0) for p in usable) / total_ratio, 2),
            "fiber": round(sum(p["fiber"] * p.get("ratio", 0) for p in usable) / total_ratio, 2),
            "protein_sources": sorted({
                s for p in usable for s in (p.get("protein_sources") or [])
            }),
            "prebiotics": sorted({
                s for p in usable for s in (p.get("prebiotics") or [])
            }),
            "gut_friendly_score": (
                round(sum(p["gut_friendly_score"] * p.get("ratio", 0) for p in usable
                          if p.get("gut_friendly_score") is not None) /
                      (sum(p.get("ratio", 0) for p in usable if p.get("gut_friendly_score") is not None) or 1), 2)
                if any(p.get("gut_friendly_score") is not None for p in usable) else None
            ),
            "nutrition_basis": next((p.get("nutrition_basis") for p in usable if p.get("nutrition_basis")), None),
        }

    return {
        "has_diet_data": True,
        "diet_mode": diet["diet_mode"],
        "products": [
            {
                "product_id": p["catalog_key"],
                "product_key": p["product_key"],
                "name": p["name"],
                "display_name": p.get("display_name") or p["name"],
                "brand": p.get("brand"),
                "ratio": p.get("ratio", 1.0),
            }
            for p in products
        ],
        "detailed_products": products,
        "nutrition_summary": summary,
        "source": diet.get("source"),
        "notes": notes,
    }


# =========================================================
# 从历史记录预填「饮食变化」槽位
# =========================================================
def get_recent_diet_change(pet_id: str) -> dict:
    """
    从库里还原「最近一次饮食变化」，供 pipeline 首轮预填槽位。

    数据来源：miniprogram_food_change_intent（按 cat_profile.user_id 关联）。
    注意：现有数据里该表 user_id 多为 NULL / 与档案 user_id 无交集，
    所以实际常常拿不到，此时返回 found=False，交给对话补全。
    """
    intent = get_latest_food_change_intent(pet_id)
    if not intent:
        return {"found": False}

    change_days = None
    if intent.get("created_at"):
        try:
            dt = datetime.strptime(intent["created_at"][:10], "%Y-%m-%d").date()
            change_days = max((datetime.now().date() - dt).days, 0)
        except ValueError:
            pass

    return {
        "found": True,
        "old_product": intent.get("previous_product") or None,
        "new_product": intent.get("new_product") or None,
        "new_product_ref": intent.get("new_product_ref"),
        "change_date": (intent.get("created_at") or "")[:10] or None,
        "change_days": change_days,
        "match_status": intent.get("match_status"),
        "user_message": intent.get("user_message"),
    }


# =========================================================
# 领域计算（v2 接口）
# =========================================================
def _delta(new_value, old_value):
    if new_value is None or old_value is None:
        return 0.0
    return round(new_value - old_value, 2)


def compare_diets(baseline_profile: dict, new_product) -> dict:
    """
    基础饮食 vs 新粮（v2 接口）。
    结构与 v2 保持一致，另加真实库的软便风险 / 肠胃友好分。
    """
    if not baseline_profile or not baseline_profile.get("has_diet_data") or not new_product:
        return {}

    old = baseline_profile.get("nutrition_summary") or {}
    old_sources = set(old.get("protein_sources") or [])
    new_sources = set(new_product.get("protein_sources") or [])

    def _num(x):
        return x if isinstance(x, (int, float)) else None

    return {
        "baseline_summary": old,
        "new_product": {
            "product_id": new_product.get("catalog_key"),
            "product_key": new_product.get("product_key"),
            "name": new_product.get("name"),
            "display_name": new_product.get("display_name") or new_product.get("name"),
            "brand": new_product.get("brand"),
            "protein": new_product.get("protein"),
            "fat": new_product.get("fat"),
            "fiber": new_product.get("fiber"),
            "protein_sources": new_product.get("protein_sources", []),
            "prebiotics": new_product.get("prebiotics", []),
        },
        "delta": {
            "protein": _delta(_num(new_product.get("protein")), _num(old.get("protein"))),
            "fat": _delta(_num(new_product.get("fat")), _num(old.get("fat"))),
            "fiber": _delta(_num(new_product.get("fiber")), _num(old.get("fiber"))),
        },
        "protein_source_changed": bool(old_sources) and old_sources != new_sources,
        "prebiotics_changed": set(old.get("prebiotics") or []) != set(new_product.get("prebiotics") or []),
        # 真实库健康信号
        "new_soft_stool_risk": new_product.get("soft_stool_risk") or {},
        "new_gut_friendly_score": new_product.get("gut_friendly_score"),
        "baseline_gut_friendly_score": old.get("gut_friendly_score"),
        "nutrition_basis": new_product.get("nutrition_basis") or old.get("nutrition_basis"),
    }


def build_manual_old_new_compare(old_product_name: str, new_product_name: str) -> dict:
    """无基础饮食时，用户补齐原粮 + 新粮，就临时构造对比（v2 接口）。"""
    old_product = get_product_by_name(old_product_name)
    new_product = get_product_by_name(new_product_name)

    if not old_product or not new_product:
        return {
            "old_product_found": bool(old_product),
            "new_product_found": bool(new_product),
            "compare": {},
        }

    baseline_profile = {
        "has_diet_data": True,
        "nutrition_summary": {
            "protein": old_product.get("protein"),
            "fat": old_product.get("fat"),
            "fiber": old_product.get("fiber"),
            "protein_sources": old_product.get("protein_sources", []),
            "prebiotics": old_product.get("prebiotics", []),
            "gut_friendly_score": old_product.get("gut_friendly_score"),
        },
    }

    return {
        "old_product_found": True,
        "new_product_found": True,
        "compare": compare_diets(baseline_profile, new_product),
    }


# =========================================================
# 宠物自身健康事件
# =========================================================
HEALTH_EVENT_KEYWORDS = {
    "soft_stool": ("软便", "拉稀", "腹泻", "稀便", "便软"),
    "vomit": ("呕吐", "吐"),
    "blood_stool": ("便血", "血便", "带血"),
    "skin": ("皮肤", "瘙痒", "掉毛", "黑下巴"),
    "urinary": ("泌尿", "尿", "结石"),
    "allergy": ("过敏",),
}


def get_recent_health_events(pet_id: str, days: int = 90) -> list:
    """
    宠物自身的健康事件。

    真实库里没有按宠物的 health_event 表，这里用档案上的
    symptoms_json / diseases_json / allergies_json 还原成事件列表。
    `days` 仅为兼容原签名保留（档案无事件时间，用 updated_at 近似）。
    """
    pet = get_pet_profile(pet_id)
    events = []

    def add(tag, kind):
        tag = str(tag).strip()
        if not tag:
            return
        event_type = "other"
        for etype, kws in HEALTH_EVENT_KEYWORDS.items():
            if any(k in tag for k in kws):
                event_type = etype
                break
        events.append({
            "event_type": event_type,
            "raw_label": tag,
            "kind": kind,
            "event_date": pet.get("updated_at"),
            "resolved": None,
        })

    for s in pet.get("symptoms", []):
        add(s, "symptom")
    for d in pet.get("diseases", []):
        add(d, "disease")
    for a in pet.get("allergies", []):
        add(a, "allergy")

    return events
