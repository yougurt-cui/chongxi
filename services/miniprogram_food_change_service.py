"""Mini-program food-change intent extraction, product matching and audit storage."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import pymysql
from openai import OpenAI

from app_config import get_feature_mysql_config, get_mysql_config, get_qwen_config


TABLE_NAME = "miniprogram_food_change_intent"
PROMPT_VERSION = "food-change-intent-v1"
MAX_MESSAGE_LENGTH = 4000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _connect_app(autocommit: bool = False):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _connect_feature(autocommit: bool = True):
    cfg = get_feature_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def init_miniprogram_tables() -> None:
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NULL,
                    session_id VARCHAR(128) NULL,
                    user_message TEXT NOT NULL,
                    request_cat_status_json LONGTEXT NULL,
                    is_food_change_intent TINYINT NULL,
                    intent_confidence DECIMAL(6,5) NULL,
                    extracted_brand VARCHAR(255) NULL,
                    extracted_product VARCHAR(512) NULL,
                    cat_status_json LONGTEXT NULL,
                    matched_catalog_key VARCHAR(128) NULL,
                    matched_product_key VARCHAR(1024) NULL,
                    matched_brand VARCHAR(255) NULL,
                    matched_product_name VARCHAR(512) NULL,
                    match_score DECIMAL(6,5) NULL,
                    match_status VARCHAR(32) NULL,
                    model_name VARCHAR(128) NULL,
                    prompt_version VARCHAR(64) NOT NULL,
                    model_raw_result LONGTEXT NULL,
                    model_result_json LONGTEXT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'processing',
                    error_message TEXT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_mini_user_created (user_id, created_at),
                    KEY idx_mini_session_created (session_id, created_at),
                    KEY idx_mini_intent (is_food_change_intent, created_at),
                    KEY idx_mini_match (matched_catalog_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _compact(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _clean(value).lower())


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = _clean(raw)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("通义千问未返回有效 JSON")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("通义千问结果必须是 JSON 对象")
    return data


def _normalize_cat_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = (
        "name", "age", "age_months", "sex", "neutered", "weight_kg",
        "breed", "symptoms", "diseases", "allergies", "stool", "appetite",
        "activity", "current_food_duration", "notes",
    )
    return {key: value[key] for key in allowed if value.get(key) not in (None, "", [])}


def extract_food_change_intent(message: str, supplied_cat_status: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    cfg = get_qwen_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置通义千问 API Key，请设置 DASHSCOPE_API_KEY 或 QWEN_API_KEY")
    schema = {
        "is_food_change_intent": True,
        "confidence": 0.0,
        "brand": "用户提到的品牌，没有则为空字符串",
        "product_name": "用户提到的具体产品/系列，没有则为空字符串",
        "current_food": {"brand": "当前粮品牌", "product_name": "当前粮产品/系列"},
        "target_food": {"brand": "目标粮品牌", "product_name": "目标粮产品/系列"},
        "cat_status": {
            "name": "", "age": "", "age_months": None, "sex": "", "neutered": None,
            "weight_kg": None, "breed": "", "symptoms": [], "diseases": [],
            "allergies": [], "stool": "", "appetite": "", "activity": "",
            "current_food_duration": "", "notes": "",
        },
        "reason": "简短判断依据",
    }
    system_prompt = (
        "你是猫粮换粮意图识别器。判断用户是否在表达换粮、选新粮、从某粮换到另一粮、"
        "咨询当前猫粮是否需要更换。提取用户明确说出的品牌、产品/系列和猫咪状态。"
        "不得补充或猜测用户未提供的信息。current_food 表示正在吃的粮，target_food 表示想换的粮；"
        "顶层 brand 和 product_name 与 current_food 保持一致，兼容只有一个产品的情况。"
        "只返回一个合法 JSON 对象，不要 Markdown。结构示例：" + _json_dumps(schema)
    )
    user_content = _json_dumps({"user_message": message, "supplied_cat_status": supplied_cat_status})
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=45.0)
    completion = client.chat.completions.create(
        model=cfg["model"],
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = completion.choices[0].message.content or ""
    parsed = _parse_json_object(raw)
    parsed["is_food_change_intent"] = bool(parsed.get("is_food_change_intent"))
    try:
        parsed["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence") or 0)))
    except (TypeError, ValueError):
        parsed["confidence"] = 0.0
    parsed["brand"] = _clean(parsed.get("brand"), 255)
    parsed["product_name"] = _clean(parsed.get("product_name"), 512)
    for food_key in ("current_food", "target_food"):
        food = parsed.get(food_key) if isinstance(parsed.get(food_key), dict) else {}
        parsed[food_key] = {
            "brand": _clean(food.get("brand"), 255),
            "product_name": _clean(food.get("product_name"), 512),
        }
    if not parsed["current_food"]["brand"] and parsed["brand"]:
        parsed["current_food"] = {"brand": parsed["brand"], "product_name": parsed["product_name"]}
    parsed["brand"] = parsed["current_food"]["brand"] or parsed["brand"]
    parsed["product_name"] = parsed["current_food"]["product_name"] or parsed["product_name"]
    merged_status = dict(_normalize_cat_status(supplied_cat_status))
    merged_status.update(_normalize_cat_status(parsed.get("cat_status")))
    parsed["cat_status"] = merged_status
    return parsed, raw, cfg["model"]


def _candidate_score(brand: str, product: str, row: dict[str, Any]) -> float:
    wanted_brand, wanted_product = _compact(brand), _compact(product)
    row_brand = _compact(row.get("standard_brand") or row.get("raw_brand"))
    row_product = _compact(row.get("product_name") or row.get("raw_title"))
    brand_score = SequenceMatcher(None, wanted_brand, row_brand).ratio() if wanted_brand else 0.0
    product_score = SequenceMatcher(None, wanted_product, row_product).ratio() if wanted_product else 0.0
    if wanted_brand and (wanted_brand in row_brand or row_brand in wanted_brand):
        brand_score = max(brand_score, 0.95)
    if wanted_product and (wanted_product in row_product or row_product in wanted_product):
        product_score = max(product_score, 0.92)
    if wanted_brand and wanted_product:
        return round(brand_score * 0.4 + product_score * 0.6, 5)
    return round(brand_score or product_score, 5)


def match_catalog_product(brand: str, product: str) -> dict[str, Any] | None:
    # A brand can own many formulas. Never guess a concrete catalog product
    # when the user only supplied the brand name.
    if not product:
        return None
    terms = [term for term in (brand, product) if term]
    where = ["status = 'active'"]
    params: list[Any] = []
    likes = []
    for term in terms:
        likes.append("(standard_brand LIKE %s OR raw_brand LIKE %s OR product_name LIKE %s OR raw_title LIKE %s)")
        params.extend([f"%{term}%"] * 4)
    where.append("(" + " OR ".join(likes) + ")")
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT catalog_key, product_key, standard_brand, raw_brand, product_name, raw_title, score_source_id "
                "FROM catfood_product_catalog WHERE " + " AND ".join(where) + " LIMIT 100",
                params,
            )
            rows = list(cursor.fetchall() or [])
    if not rows:
        return None
    scored = sorted((( _candidate_score(brand, product, row), row) for row in rows), key=lambda item: item[0], reverse=True)
    score, row = scored[0]
    threshold = 0.55 if brand and product else 0.72
    if score < threshold:
        return None
    return {
        "catalog_key": row.get("catalog_key"), "product_key": row.get("product_key"),
        "brand": row.get("standard_brand"), "product_name": row.get("product_name"),
        "score_source_id": row.get("score_source_id"),
        "score": score, "status": "matched" if score >= 0.8 else "possible_match",
    }


INGREDIENT_CATEGORIES = (
    ("animal_protein", "蛋白质来源"),
    ("plant_protein", "植物蛋白 / 豆类"),
    ("fat", "脂肪与脂肪酸"),
    ("carbohydrate", "碳水结构来源"),
    ("fiber", "纤维与肠道缓冲"),
    ("botanical", "天然风味与植物营养素"),
)


def _ingredient_category(item: dict[str, Any]) -> str | None:
    role = _clean(item.get("primary_nutrition_role"))
    family = _clean(item.get("ingredient_family"))
    if bool(item.get("is_plant_protein")):
        return "plant_protein"
    if bool(item.get("is_protein")) and item.get("source_type") == "animal":
        return "animal_protein"
    if "脂肪" in role or "油脂" in family:
        return "fat"
    if "碳水" in role or any(word in family for word in ("淀粉", "谷物", "根茎")):
        return "carbohydrate"
    if "纤维" in role or "益生元" in role or any(word in family for word in ("纤维", "益生元")):
        return "fiber"
    if any(word in family for word in ("果蔬", "草本", "植物功能")) or "抗氧化" in role:
        return "botanical"
    return None


def _find_standard_formula(cursor, matched: dict[str, Any]) -> dict[str, Any] | None:
    source_id = matched.get("score_source_id")
    if source_id is not None:
        cursor.execute(
            """
            SELECT f.formula_id,p.product_id,b.standard_brand_name,p.standard_product_name,p.display_name,
                   f.raw_ingredient_example,f.normalized_ingredient_composition
            FROM catfood_ocr_standard_mapping m
            JOIN catfood_standard_formula f ON f.formula_id=m.formula_id AND f.status='active'
            JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
            JOIN catfood_standard_brand b ON b.brand_id=p.brand_id AND b.active=1
            WHERE m.source_id=%s
            ORDER BY f.is_current DESC,f.formula_version DESC LIMIT 1
            """,
            (source_id,),
        )
        row = cursor.fetchone()
        if row:
            return row

    cursor.execute(
        """
        SELECT f.formula_id,p.product_id,b.standard_brand_name,p.standard_product_name,p.display_name,
               f.raw_ingredient_example,f.normalized_ingredient_composition
        FROM catfood_standard_product p
        JOIN catfood_standard_brand b ON b.brand_id=p.brand_id AND b.active=1
        JOIN catfood_standard_formula f ON f.product_id=p.product_id AND f.status='active'
        WHERE p.active=1 AND b.standard_brand_name=%s
        ORDER BY f.is_current DESC,f.formula_version DESC
        """,
        (matched.get("brand"),),
    )
    candidates = list(cursor.fetchall() or [])
    wanted = _compact(matched.get("product_name"))
    if not candidates or not wanted:
        return None
    scored = []
    for row in candidates:
        names = (row.get("standard_product_name"), row.get("display_name"))
        score = max(SequenceMatcher(None, wanted, _compact(name)).ratio() for name in names if _compact(name))
        if any(_compact(name) and (_compact(name) in wanted or wanted in _compact(name)) for name in names):
            score = max(score, 0.9)
        scored.append((score, row))
    best_score, best = max(scored, key=lambda item: item[0])
    return best if best_score >= 0.45 else None


def get_product_ingredient_analysis(matched: dict[str, Any] | None) -> dict[str, Any] | None:
    if not matched:
        return None
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            formula = _find_standard_formula(cursor, matched)
            if not formula:
                return None
            cursor.execute(
                """
                SELECT position,raw_name,standard_name,ingredient_family,source_type,
                       primary_nutrition_role,is_protein,is_plant_protein
                FROM catfood_formula_ingredient_item
                WHERE formula_id=%s AND COALESCE(is_ignored,0)=0
                ORDER BY position
                """,
                (formula["formula_id"],),
            )
            items = list(cursor.fetchall() or [])
    grouped: dict[str, list[str]] = {key: [] for key, _ in INGREDIENT_CATEGORIES}
    details: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in INGREDIENT_CATEGORIES}
    for item in items:
        category = _ingredient_category(item)
        if not category:
            continue
        name = _clean(item.get("standard_name") or item.get("raw_name"))
        if name and name not in grouped[category]:
            grouped[category].append(name)
            details[category].append({
                "position": item.get("position"), "name": name,
                "raw_name": item.get("raw_name"), "family": item.get("ingredient_family"),
                "nutrition_role": item.get("primary_nutrition_role"),
            })
    return {
        "formula_id": formula["formula_id"],
        "product_id": formula["product_id"],
        "brand": formula["standard_brand_name"],
        "product_name": formula["display_name"] or formula["standard_product_name"],
        "raw_ingredient_text": formula.get("raw_ingredient_example") or formula.get("normalized_ingredient_composition") or "",
        "groups": [
            {"category": key, "title": title, "ingredients": grouped[key], "items": details[key]}
            for key, title in INGREDIENT_CATEGORIES if grouped[key]
        ],
    }


def list_catalog_products_by_brand(brand: str, *, query: str = "", limit: int = 50) -> dict[str, Any]:
    brand = _clean(brand, 255)
    query = _clean(query, 255)
    if not brand:
        raise ValueError("brand 不能为空")
    limit = max(1, min(int(limit or 50), 100))
    where = ["status='active'", "standard_brand=%s"]
    params: list[Any] = [brand]
    if query:
        where.append("(product_name LIKE %s OR raw_title LIKE %s)")
        params.extend([f"%{query}%", f"%{query}%"])
    params.append(limit * 3)
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT catalog_key,product_key,standard_brand,product_name,raw_title,score_source_id,"
                "compare_available,source FROM catfood_product_catalog WHERE " + " AND ".join(where) +
                " ORDER BY compare_available DESC,FIELD(source,'merged','score_db','taobao'),id DESC LIMIT %s",
                params,
            )
            rows = list(cursor.fetchall() or [])
    items = []
    seen = set()
    for row in rows:
        name_key = _compact(row.get("product_name"))
        if not name_key or name_key in seen:
            continue
        seen.add(name_key)
        items.append({
            "catalog_key": row.get("catalog_key"), "product_key": row.get("product_key"),
            "brand": row.get("standard_brand"), "product_name": row.get("product_name"),
            "raw_title": row.get("raw_title"), "score_source_id": row.get("score_source_id"),
            "compare_available": bool(row.get("compare_available")),
        })
        if len(items) >= limit:
            break
    return {"ok": True, "brand": brand, "count": len(items), "items": items}


def get_catalog_product_ingredients(payload: dict[str, Any]) -> dict[str, Any]:
    catalog_key = _clean(payload.get("catalog_key"), 128)
    if not catalog_key:
        raise ValueError("catalog_key 不能为空")
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT catalog_key,product_key,standard_brand AS brand,product_name,score_source_id "
                "FROM catfood_product_catalog WHERE catalog_key=%s AND status='active' LIMIT 1",
                (catalog_key,),
            )
            matched = cursor.fetchone()
    if not matched:
        raise ValueError("产品不存在或已停用")
    analysis = get_product_ingredient_analysis(matched)
    return {"ok": True, "product": matched, "ingredient_analysis": analysis}


def _insert_processing(record_id: str, payload: dict[str, Any]) -> None:
    now = _now()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {TABLE_NAME} (id,user_id,session_id,user_message,request_cat_status_json,"
                "prompt_version,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,'processing',%s,%s)",
                (record_id, _clean(payload.get("user_id"), 128) or None,
                 _clean(payload.get("session_id"), 128) or None, payload["message"],
                 _json_dumps(payload.get("cat_status") or {}), PROMPT_VERSION, now, now),
            )
        conn.commit()


def analyze_and_store(payload: dict[str, Any]) -> dict[str, Any]:
    message = _clean(payload.get("message") or payload.get("user_message"))
    if not message:
        raise ValueError("message 不能为空")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"message 不能超过 {MAX_MESSAGE_LENGTH} 个字符")
    supplied_status = payload.get("cat_status") or {}
    if not isinstance(supplied_status, dict):
        raise ValueError("cat_status 必须是 JSON 对象")
    normalized_payload = {**payload, "message": message, "cat_status": supplied_status}
    init_miniprogram_tables()
    record_id = uuid.uuid4().hex
    _insert_processing(record_id, normalized_payload)
    try:
        result, raw, model = extract_food_change_intent(message, supplied_status)
        matched = match_catalog_product(result["brand"], result["product_name"])
        target_food = result.get("target_food") or {}
        target_matched = match_catalog_product(target_food.get("brand", ""), target_food.get("product_name", ""))
        current_ingredients = get_product_ingredient_analysis(matched)
        target_ingredients = get_product_ingredient_analysis(target_matched)
        if matched:
            match_status = matched["status"]
        elif result["brand"] and not result["product_name"]:
            match_status = "product_not_provided"
        elif not result["brand"] and not result["product_name"]:
            match_status = "not_provided"
        else:
            match_status = "not_found"
        with _connect_app() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {TABLE_NAME} SET is_food_change_intent=%s,intent_confidence=%s,
                    extracted_brand=%s,extracted_product=%s,cat_status_json=%s,matched_catalog_key=%s,
                    matched_product_key=%s,matched_brand=%s,matched_product_name=%s,match_score=%s,
                    match_status=%s,model_name=%s,model_raw_result=%s,model_result_json=%s,
                    status='completed',updated_at=%s WHERE id=%s""",
                    (1 if result["is_food_change_intent"] else 0, result["confidence"], result["brand"] or None,
                     result["product_name"] or None, _json_dumps(result["cat_status"]),
                     matched.get("catalog_key") if matched else None, matched.get("product_key") if matched else None,
                     matched.get("brand") if matched else None, matched.get("product_name") if matched else None,
                     matched.get("score") if matched else None, match_status, model, raw, _json_dumps(result), _now(), record_id),
                )
            conn.commit()
        return {
            "ok": True, "record_id": record_id, "intent": result,
            "product_match": matched, "target_product_match": target_matched,
            "ingredient_analysis": current_ingredients,
            "target_ingredient_analysis": target_ingredients,
            "match_status": match_status,
        }
    except Exception as exc:
        with _connect_app() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {TABLE_NAME} SET status='failed',error_message=%s,updated_at=%s WHERE id=%s",
                    (_clean(exc, 4000), _now(), record_id),
                )
            conn.commit()
        raise
