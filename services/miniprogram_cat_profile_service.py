"""Cat profile management for the WeChat mini-program."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import pymysql

from app_config import get_mysql_config


TABLE_NAME = "miniprogram_cat_profile"
MAX_LIST_LIMIT = 100


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect_app(autocommit: bool = False):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        return default


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _clean_user_id(value: Any) -> str:
    user_id = _clean(value, 128)
    if not user_id:
        raise ValueError("user_id 不能为空")
    return user_id


def _clean_profile_id(value: Any) -> str:
    profile_id = _clean(value, 32)
    if not profile_id:
        raise ValueError("profile_id 不能为空")
    return profile_id


def _clean_bool(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return 1
    if text in {"0", "false", "no", "n", "off"}:
        return 0
    raise ValueError("布尔字段格式不正确")


def _clean_int(value: Any, field_name: str, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必须是整数")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} 不能小于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field_name} 不能大于 {maximum}")
    return parsed


def _clean_decimal(value: Any, field_name: str, *, minimum: Decimal, maximum: Decimal) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} 必须是数字")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间")
    return parsed.quantize(Decimal("0.01"))


def _clean_list(value: Any, field_name: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必须是数组")
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean(item, 64)
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return items[:30]


def _clean_date(value: Any, field_name: str) -> str:
    text = _clean(value, 10)
    if not text:
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD")
    return text


def _normalize_payload(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    normalized: dict[str, Any] = {}
    if "user_id" in payload or not partial:
        normalized["user_id"] = _clean_user_id(payload.get("user_id"))
    if "name" in payload or not partial:
        name = _clean(payload.get("name"), 64)
        if not name:
            raise ValueError("name 不能为空")
        normalized["name"] = name
    if "breed" in payload or not partial:
        normalized["breed"] = _clean(payload.get("breed"), 64)
    if "animal_type" in payload or not partial:
        animal_type = _clean(payload.get("animal_type"), 16).lower()
        if animal_type and animal_type not in {"cat", "dog", "unknown"}:
            raise ValueError("animal_type 仅支持 cat/dog/unknown")
        normalized["animal_type"] = animal_type or "cat"
    if "sex" in payload or not partial:
        sex = _clean(payload.get("sex"), 16)
        if sex and sex not in {"male", "female", "unknown", "公", "母", "未知"}:
            raise ValueError("sex 仅支持 male/female/unknown")
        normalized["sex"] = sex
    if "neutered" in payload or not partial:
        normalized["neutered"] = _clean_bool(payload.get("neutered"))
    if "birthday" in payload or not partial:
        normalized["birthday"] = _clean_date(payload.get("birthday"), "birthday") or None
    if "age_text" in payload or "age" in payload or not partial:
        normalized["age_text"] = _clean(payload.get("age_text") or payload.get("age"), 64)
    if "age_months" in payload or not partial:
        normalized["age_months"] = _clean_int(payload.get("age_months"), "age_months", minimum=0, maximum=360)
    if "weight_kg" in payload or "weight" in payload or not partial:
        normalized["weight_kg"] = _clean_decimal(
            payload.get("weight_kg") if "weight_kg" in payload else payload.get("weight"),
            "weight_kg",
            minimum=Decimal("0.1"),
            maximum=Decimal("30"),
        )
    if "avatar_url" in payload or not partial:
        normalized["avatar_url"] = _clean(payload.get("avatar_url"), 1024)
    if "allergies" in payload or not partial:
        normalized["allergies"] = _clean_list(payload.get("allergies"), "allergies")
    if "diseases" in payload or not partial:
        normalized["diseases"] = _clean_list(payload.get("diseases"), "diseases")
    if "symptoms" in payload or not partial:
        normalized["symptoms"] = _clean_list(payload.get("symptoms"), "symptoms")
    if "health_status" in payload or not partial:
        normalized["health_status"] = _clean_list(payload.get("health_status"), "health_status")
    if "avatar_image_id" in payload or not partial:
        normalized["avatar_image_id"] = _clean(payload.get("avatar_image_id"), 32)
    diet = payload.get("diet")
    if diet is not None and not isinstance(diet, dict):
        raise ValueError("diet 必须是对象")
    diet = diet or {}
    if "food_brand" in payload or "brand" in diet or not partial:
        normalized["food_brand"] = _clean(
            payload.get("food_brand") if "food_brand" in payload else diet.get("brand"),
            128,
        )
    if "food_product" in payload or "product" in diet or "product_name" in diet or not partial:
        normalized["food_product"] = _clean(
            payload.get("food_product")
            if "food_product" in payload
            else diet.get("product", diet.get("product_name")),
            512,
        )
    if "food_product_id" in payload or "product_id" in diet or not partial:
        normalized["food_product_id"] = _clean_int(
            payload.get("food_product_id") if "food_product_id" in payload else diet.get("product_id"),
            "food_product_id", minimum=1,
        )
    if "food_formula_id" in payload or "formula_id" in diet or not partial:
        normalized["food_formula_id"] = _clean_int(
            payload.get("food_formula_id") if "food_formula_id" in payload else diet.get("formula_id"),
            "food_formula_id", minimum=1,
        )
    if ("food_product_id" in normalized) != ("food_formula_id" in normalized):
        raise ValueError("food_product_id \u548c food_formula_id \u5fc5\u987b\u540c\u65f6\u63d0\u4f9b")
    if "notes" in payload or not partial:
        normalized["notes"] = _clean(payload.get("notes"), 1000)
    if "is_default" in payload or not partial:
        normalized["is_default"] = _clean_bool(payload.get("is_default")) or 0
    return normalized


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    food_brand = row.get("food_brand") or ""
    food_product = row.get("food_product") or ""
    food_product_id = row.get("food_product_id")
    food_formula_id = row.get("food_formula_id")
    avatar_image_id = row.get("avatar_image_id") or ""
    avatar_url = row.get("avatar_url") or ""
    if avatar_image_id:
        avatar_url = f"/api/miniprogram/pet-images/{avatar_image_id}"
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "name": row.get("name") or "",
        "animal_type": row.get("animal_type") or "cat",
        "breed": row.get("breed") or "",
        "sex": row.get("sex") or "",
        "neutered": None if row.get("neutered") is None else bool(row.get("neutered")),
        "birthday": str(row.get("birthday") or ""),
        "age_text": row.get("age_text") or "",
        "age_months": row.get("age_months"),
        "weight_kg": None if row.get("weight_kg") is None else float(row.get("weight_kg")),
        "avatar_url": avatar_url,
        "allergies": _json_loads(row.get("allergies_json"), []),
        "diseases": _json_loads(row.get("diseases_json"), []),
        "symptoms": _json_loads(row.get("symptoms_json"), []),
        "health_status": _json_loads(row.get("health_status_json"), []),
        "avatar_image_id": avatar_image_id,
        "food_brand": food_brand,
        "food_product": food_product,
        "food_product_id": food_product_id,
        "food_formula_id": food_formula_id,
        "diet": {
            "brand": food_brand, "product": food_product,
            "product_id": food_product_id, "formula_id": food_formula_id,
        },
        "notes": row.get("notes") or "",
        "is_default": bool(row.get("is_default")),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def init_miniprogram_cat_profile_tables() -> None:
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    name VARCHAR(64) NOT NULL,
                    animal_type VARCHAR(16) NOT NULL DEFAULT 'cat',
                    breed VARCHAR(64) NULL,
                    sex VARCHAR(16) NULL,
                    neutered TINYINT NULL,
                    birthday DATE NULL,
                    age_text VARCHAR(64) NULL,
                    age_months INT NULL,
                    weight_kg DECIMAL(5,2) NULL,
                    avatar_url VARCHAR(1024) NULL,
                    allergies_json LONGTEXT NULL,
                    diseases_json LONGTEXT NULL,
                    symptoms_json LONGTEXT NULL,
                    health_status_json LONGTEXT NULL,
                    avatar_image_id CHAR(32) NULL,
                    food_brand VARCHAR(128) NULL,
                    food_product VARCHAR(512) NULL,
                    food_product_id BIGINT UNSIGNED NULL,
                    food_formula_id BIGINT UNSIGNED NULL,
                    notes TEXT NULL,
                    is_default TINYINT NOT NULL DEFAULT 0,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    deleted_at DATETIME NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_mini_cat_user_status (user_id, status, updated_at),
                    KEY idx_mini_cat_user_default (user_id, is_default, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(f"SHOW COLUMNS FROM {TABLE_NAME}")
            existing_columns = {str(row["Field"]) for row in cursor.fetchall() or []}
            if "food_brand" not in existing_columns:
                cursor.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN food_brand VARCHAR(128) NULL AFTER symptoms_json"
                )
            if "food_product" not in existing_columns:
                cursor.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN food_product VARCHAR(512) NULL AFTER food_brand"
                )
            if "animal_type" not in existing_columns:
                cursor.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN animal_type VARCHAR(16) NOT NULL DEFAULT 'cat' AFTER name"
                )
            if "health_status_json" not in existing_columns:
                cursor.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN health_status_json LONGTEXT NULL AFTER symptoms_json"
                )
            if "avatar_image_id" not in existing_columns:
                cursor.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN avatar_image_id CHAR(32) NULL AFTER health_status_json"
                )
            if "deleted_at" not in existing_columns:
                cursor.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN deleted_at DATETIME NULL AFTER status"
                )
            if "food_product_id" not in existing_columns:
                cursor.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN food_product_id BIGINT UNSIGNED NULL AFTER food_product"
                )
            if "food_formula_id" not in existing_columns:
                cursor.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN food_formula_id BIGINT UNSIGNED NULL AFTER food_product_id"
                )
        conn.commit()


def _unset_other_defaults(cursor, user_id: str, profile_id: str | None = None) -> None:
    params: list[Any] = [user_id]
    where = "user_id=%s AND status='active'"
    if profile_id:
        where += " AND id<>%s"
        params.append(profile_id)
    cursor.execute(f"UPDATE {TABLE_NAME} SET is_default=0 WHERE {where}", params)


def _resolve_food_formula(product_id: int, formula_id: int) -> dict[str, Any]:
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.product_id,f.formula_id,b.standard_brand_name,
                       p.standard_product_name,p.display_name
                FROM catfood_standard_formula f
                JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                JOIN catfood_standard_brand b ON b.brand_id=p.brand_id AND b.active=1
                WHERE p.product_id=%s AND f.formula_id=%s
                  AND f.status='active' AND f.is_current=1
                LIMIT 1
                """,
                (product_id, formula_id),
            )
            row = cursor.fetchone()
    if not row:
        raise ValueError("产品或配方不存在、已停用，或二者不匹配")
    return row


def _apply_food_formula_snapshot(data: dict[str, Any]) -> None:
    product_id = data.get("food_product_id")
    formula_id = data.get("food_formula_id")
    if product_id is None and formula_id is None:
        return
    resolved = _resolve_food_formula(product_id, formula_id)
    data["food_brand"] = resolved["standard_brand_name"]
    data["food_product"] = resolved.get("display_name") or resolved["standard_product_name"]


def create_cat_profile(payload: dict[str, Any]) -> dict[str, Any]:
    data = _normalize_payload(payload)
    _apply_food_formula_snapshot(data)
    if data["avatar_image_id"]:
        from services.miniprogram_pet_image_service import validate_pet_image_owner

        validate_pet_image_owner(data["avatar_image_id"], data["user_id"])
    init_miniprogram_cat_profile_tables()
    profile_id = uuid.uuid4().hex
    now = _now()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            if data["is_default"]:
                _unset_other_defaults(cursor, data["user_id"])
            cursor.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    id,user_id,name,animal_type,breed,sex,neutered,birthday,age_text,age_months,weight_kg,
                    avatar_url,allergies_json,diseases_json,symptoms_json,health_status_json,avatar_image_id,
                    food_brand,food_product,food_product_id,food_formula_id,notes,is_default,status,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                """,
                (
                    profile_id, data["user_id"], data["name"], data["animal_type"],
                    data["breed"] or None, data["sex"] or None,
                    data["neutered"], data["birthday"], data["age_text"] or None, data["age_months"],
                    data["weight_kg"], data["avatar_url"] or None, _json_dumps(data["allergies"]),
                    _json_dumps(data["diseases"]), _json_dumps(data["symptoms"]),
                    _json_dumps(data["health_status"]), data["avatar_image_id"] or None,
                    data["food_brand"] or None, data["food_product"] or None,
                    data["food_product_id"], data["food_formula_id"],
                    data["notes"] or None, data["is_default"], now, now,
                ),
            )
        conn.commit()
    if data["avatar_image_id"]:
        from services.miniprogram_pet_image_service import bind_pet_image

        bind_pet_image(data["avatar_image_id"], user_id=data["user_id"], profile_id=profile_id)
    item = get_cat_profile(data["user_id"], profile_id)
    return {"ok": True, "item": item}


def list_cat_profiles(user_id: Any, *, limit: Any = 50) -> dict[str, Any]:
    cleaned_user_id = _clean_user_id(user_id)
    try:
        cleaned_limit = max(1, min(int(limit or 50), MAX_LIST_LIMIT))
    except (TypeError, ValueError):
        cleaned_limit = 50
    init_miniprogram_cat_profile_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM {TABLE_NAME}
                WHERE user_id=%s AND status='active'
                ORDER BY is_default DESC, updated_at DESC
                LIMIT %s
                """,
                (cleaned_user_id, cleaned_limit),
            )
            rows = list(cursor.fetchall() or [])
    return {"ok": True, "count": len(rows), "items": [_serialize(row) for row in rows]}


def get_cat_profile(user_id: Any, profile_id: Any) -> dict[str, Any]:
    cleaned_user_id = _clean_user_id(user_id)
    cleaned_profile_id = _clean_profile_id(profile_id)
    init_miniprogram_cat_profile_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {TABLE_NAME} WHERE id=%s AND user_id=%s AND status='active' LIMIT 1",
                (cleaned_profile_id, cleaned_user_id),
            )
            row = cursor.fetchone()
    if not row:
        raise LookupError("猫咪档案不存在")
    return _serialize(row)


def update_cat_profile(user_id: Any, profile_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    cleaned_user_id = _clean_user_id(user_id)
    cleaned_profile_id = _clean_profile_id(profile_id)
    data = _normalize_payload({**payload, "user_id": cleaned_user_id}, partial=True)
    data.pop("user_id", None)
    if not data:
        raise ValueError("没有可更新字段")
    _apply_food_formula_snapshot(data)
    init_miniprogram_cat_profile_tables()
    if data.get("avatar_image_id"):
        from services.miniprogram_pet_image_service import validate_pet_image_owner

        validate_pet_image_owner(data["avatar_image_id"], cleaned_user_id)
    column_map = {
        "name": "name",
        "animal_type": "animal_type",
        "breed": "breed",
        "sex": "sex",
        "neutered": "neutered",
        "birthday": "birthday",
        "age_text": "age_text",
        "age_months": "age_months",
        "weight_kg": "weight_kg",
        "avatar_url": "avatar_url",
        "allergies": "allergies_json",
        "diseases": "diseases_json",
        "symptoms": "symptoms_json",
        "health_status": "health_status_json",
        "avatar_image_id": "avatar_image_id",
        "food_brand": "food_brand",
        "food_product": "food_product",
        "food_product_id": "food_product_id",
        "food_formula_id": "food_formula_id",
        "notes": "notes",
        "is_default": "is_default",
    }
    assignments = []
    params: list[Any] = []
    for key, value in data.items():
        column = column_map[key]
        assignments.append(f"{column}=%s")
        if key in {"allergies", "diseases", "symptoms", "health_status"}:
            params.append(_json_dumps(value))
        else:
            params.append(value if value != "" else None)
    assignments.append("updated_at=%s")
    params.append(_now())
    params.extend([cleaned_profile_id, cleaned_user_id])
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            if data.get("is_default"):
                _unset_other_defaults(cursor, cleaned_user_id, cleaned_profile_id)
            cursor.execute(
                f"UPDATE {TABLE_NAME} SET {', '.join(assignments)} WHERE id=%s AND user_id=%s AND status='active'",
                params,
            )
            if cursor.rowcount == 0:
                raise LookupError("猫咪档案不存在")
        conn.commit()
    if data.get("avatar_image_id"):
        from services.miniprogram_pet_image_service import bind_pet_image

        bind_pet_image(data["avatar_image_id"], user_id=cleaned_user_id, profile_id=cleaned_profile_id)
    return {"ok": True, "item": get_cat_profile(cleaned_user_id, cleaned_profile_id)}


def delete_cat_profile(user_id: Any, profile_id: Any) -> dict[str, Any]:
    cleaned_user_id = _clean_user_id(user_id)
    cleaned_profile_id = _clean_profile_id(profile_id)
    init_miniprogram_cat_profile_tables()
    deleted_at = _now()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT is_default FROM {TABLE_NAME} "
                "WHERE id=%s AND user_id=%s AND status='active' LIMIT 1 FOR UPDATE",
                (cleaned_profile_id, cleaned_user_id),
            )
            profile = cursor.fetchone()
            if not profile:
                raise LookupError("猫咪档案不存在")
            cursor.execute(
                f"UPDATE {TABLE_NAME} "
                "SET status='deleted', is_default=0, deleted_at=%s, updated_at=%s "
                "WHERE id=%s AND user_id=%s AND status='active'",
                (deleted_at, deleted_at, cleaned_profile_id, cleaned_user_id),
            )
            if profile.get("is_default"):
                cursor.execute(
                    f"UPDATE {TABLE_NAME} SET is_default=1, updated_at=%s "
                    "WHERE user_id=%s AND status='active' "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (deleted_at, cleaned_user_id),
                )
        conn.commit()
    return {"ok": True, "id": cleaned_profile_id, "deleted_at": deleted_at}
