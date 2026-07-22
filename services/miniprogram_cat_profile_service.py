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
    if "notes" in payload or not partial:
        normalized["notes"] = _clean(payload.get("notes"), 1000)
    if "is_default" in payload or not partial:
        normalized["is_default"] = _clean_bool(payload.get("is_default")) or 0
    return normalized


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "name": row.get("name") or "",
        "breed": row.get("breed") or "",
        "sex": row.get("sex") or "",
        "neutered": None if row.get("neutered") is None else bool(row.get("neutered")),
        "birthday": str(row.get("birthday") or ""),
        "age_text": row.get("age_text") or "",
        "age_months": row.get("age_months"),
        "weight_kg": None if row.get("weight_kg") is None else float(row.get("weight_kg")),
        "avatar_url": row.get("avatar_url") or "",
        "allergies": _json_loads(row.get("allergies_json"), []),
        "diseases": _json_loads(row.get("diseases_json"), []),
        "symptoms": _json_loads(row.get("symptoms_json"), []),
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
                    notes TEXT NULL,
                    is_default TINYINT NOT NULL DEFAULT 0,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_mini_cat_user_status (user_id, status, updated_at),
                    KEY idx_mini_cat_user_default (user_id, is_default, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _unset_other_defaults(cursor, user_id: str, profile_id: str | None = None) -> None:
    params: list[Any] = [user_id]
    where = "user_id=%s AND status='active'"
    if profile_id:
        where += " AND id<>%s"
        params.append(profile_id)
    cursor.execute(f"UPDATE {TABLE_NAME} SET is_default=0 WHERE {where}", params)


def create_cat_profile(payload: dict[str, Any]) -> dict[str, Any]:
    data = _normalize_payload(payload)
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
                    id,user_id,name,breed,sex,neutered,birthday,age_text,age_months,weight_kg,
                    avatar_url,allergies_json,diseases_json,symptoms_json,notes,is_default,status,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                """,
                (
                    profile_id, data["user_id"], data["name"], data["breed"] or None, data["sex"] or None,
                    data["neutered"], data["birthday"], data["age_text"] or None, data["age_months"],
                    data["weight_kg"], data["avatar_url"] or None, _json_dumps(data["allergies"]),
                    _json_dumps(data["diseases"]), _json_dumps(data["symptoms"]), data["notes"] or None,
                    data["is_default"], now, now,
                ),
            )
        conn.commit()
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
    init_miniprogram_cat_profile_tables()
    column_map = {
        "name": "name",
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
        "notes": "notes",
        "is_default": "is_default",
    }
    assignments = []
    params: list[Any] = []
    for key, value in data.items():
        column = column_map[key]
        assignments.append(f"{column}=%s")
        if key in {"allergies", "diseases", "symptoms"}:
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
    return {"ok": True, "item": get_cat_profile(cleaned_user_id, cleaned_profile_id)}


def delete_cat_profile(user_id: Any, profile_id: Any) -> dict[str, Any]:
    cleaned_user_id = _clean_user_id(user_id)
    cleaned_profile_id = _clean_profile_id(profile_id)
    init_miniprogram_cat_profile_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE_NAME} SET status='deleted', is_default=0, updated_at=%s "
                "WHERE id=%s AND user_id=%s AND status='active'",
                (_now(), cleaned_profile_id, cleaned_user_id),
            )
            if cursor.rowcount == 0:
                raise LookupError("猫咪档案不存在")
        conn.commit()
    return {"ok": True, "id": cleaned_profile_id}
