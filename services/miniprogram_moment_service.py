"""Cat moment publishing for the WeChat mini-program."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import pymysql

from app_config import get_mysql_config
from services.miniprogram_cat_profile_service import init_miniprogram_cat_profile_tables


TABLE_NAME = "miniprogram_cat_moment_post"
LIKE_TABLE_NAME = "miniprogram_cat_moment_like"
COMMENT_TABLE_NAME = "miniprogram_cat_moment_comment"
CAT_PROFILE_TABLE = "miniprogram_cat_profile"
MAX_CONTENT_LENGTH = 2000
MAX_COMMENT_LENGTH = 500
MAX_TITLE_LENGTH = 80
MAX_IMAGE_COUNT = 9
MAX_LIST_LIMIT = 100
DEFAULT_COMMENT_LIMIT = 50

CATEGORIES = {
    "SLEEP": {"code": "SLEEP", "name": "睡姿大赏", "color": "#E8D5F5"},
    "FUNNY": {"code": "FUNNY", "name": "搞笑日常", "color": "#FFE0B2"},
    "CHIN": {"code": "CHIN", "name": "黑下巴观察团", "color": "#FFCDD2"},
    "VOMIT": {"code": "VOMIT", "name": "呕吐观察团", "color": "#C8E6C9"},
    "STOOL": {"code": "STOOL", "name": "便便观察团", "color": "#B3E5FC"},
}

CATEGORY_NAME_TO_CODE = {item["name"]: code for code, item in CATEGORIES.items()}
VISIBILITIES = {"public", "private", "anonymous"}


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


def _clean_post_id(value: Any) -> str:
    post_id = _clean(value, 32)
    if not post_id:
        raise ValueError("post_id 不能为空")
    return post_id


def _clean_comment_id(value: Any) -> str:
    comment_id = _clean(value, 32)
    if not comment_id:
        raise ValueError("comment_id 不能为空")
    return comment_id


def _clean_category(value: Any) -> dict[str, str]:
    raw = _clean(value, 32)
    if not raw:
        raise ValueError("category_code 不能为空")
    code = CATEGORY_NAME_TO_CODE.get(raw) or raw.upper()
    if code not in CATEGORIES:
        raise ValueError("category_code 仅支持 SLEEP/FUNNY/CHIN/VOMIT/STOOL")
    return CATEGORIES[code]


def _clean_visibility(value: Any) -> str:
    visibility = _clean(value or "public", 16)
    if visibility not in VISIBILITIES:
        raise ValueError("visibility 仅支持 public/private/anonymous")
    return visibility


def _clean_sex(value: Any) -> str:
    sex = _clean(value, 16)
    if sex and sex not in {"male", "female", "unknown", "公", "母", "未知"}:
        raise ValueError("sex 仅支持 male/female/unknown")
    return sex


def _clean_decimal(value: Any, field_name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        import re

        match = re.search(r"\d+(?:\.\d+)?", str(value))
        if not match:
            return None
        parsed = Decimal(match.group(0))
    if parsed < Decimal("0.1") or parsed > Decimal("30"):
        raise ValueError(f"{field_name} 必须在 0.1 到 30 之间")
    return parsed.quantize(Decimal("0.01"))


def _clean_images(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("images 必须是数组")
    if len(value) > MAX_IMAGE_COUNT:
        raise ValueError(f"images 不能超过 {MAX_IMAGE_COUNT} 张")
    images: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            url = _clean(item, 1024)
            image = {"url": url}
        elif isinstance(item, dict):
            url = _clean(item.get("url") or item.get("src") or item.get("path"), 1024)
            image = {
                "url": url,
                "width": item.get("width"),
                "height": item.get("height"),
                "file_id": _clean(item.get("file_id"), 128),
            }
        else:
            raise ValueError("images 项必须是字符串或对象")
        if not image["url"]:
            raise ValueError(f"images[{index}] 缺少 url")
        if (
            image["url"].startswith("http://tmp/")
            or image["url"].startswith("https://tmp/")
            or image["url"].startswith("wxfile://tmp")
            or "**tmp**" in image["url"]
        ):
            raise ValueError("图片需要先上传后再发布")
        images.append({key: value for key, value in image.items() if value not in (None, "")})
    return images


def _derive_title(content: str, supplied_title: Any) -> str:
    title = _clean(supplied_title, MAX_TITLE_LENGTH)
    if title:
        return title
    compact = " ".join(content.split())
    return compact[:20] + ("..." if len(compact) > 20 else "")


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    content = _clean(payload.get("content"), MAX_CONTENT_LENGTH)
    if not content:
        raise ValueError("content 不能为空")
    category = _clean_category(payload.get("category_code") or payload.get("category"))
    images = _clean_images(payload.get("images"))
    if not images:
        raise ValueError("images 不能为空")
    return {
        "user_id": _clean_user_id(payload.get("user_id")),
        "cat_profile_id": _clean(payload.get("cat_profile_id"), 32) or None,
        "category_code": category["code"],
        "category_name": category["name"],
        "category_color": category["color"],
        "title": _derive_title(content, payload.get("title")),
        "content": content,
        "images": images,
        "visibility": _clean_visibility(payload.get("visibility")),
        "breed": _clean(payload.get("breed"), 64),
        "age_text": _clean(payload.get("age_text") or payload.get("age"), 64),
        "weight_kg": _clean_decimal(
            payload.get("weight_kg") if "weight_kg" in payload else payload.get("weight"),
            "weight_kg",
        ),
        "cat_name": _clean(payload.get("cat_name") or payload.get("name"), 64),
        "sex": _clean_sex(payload.get("sex") or payload.get("cat_sex")),
        "author_name": _clean(payload.get("author_name"), 64),
        "author_avatar": _clean(payload.get("author_avatar"), 1024),
    }


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    visibility = row.get("visibility") or "public"
    anonymous = visibility == "anonymous"
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "cat_profile_id": row.get("cat_profile_id") or "",
        "category_code": row.get("category_code") or "",
        "category": row.get("category_name") or "",
        "category_name": row.get("category_name") or "",
        "category_color": row.get("category_color") or "",
        "title": row.get("title") or "",
        "content": row.get("content") or "",
        "images": _json_loads(row.get("images_json"), []),
        "visibility": visibility,
        "breed": row.get("breed") or "",
        "age_text": row.get("age_text") or "",
        "weight_kg": None if row.get("weight_kg") is None else float(row.get("weight_kg")),
        "cat_name": row.get("cat_name") or "",
        "author": {
            "name": "匿名铲屎官" if anonymous else (row.get("author_name") or "我"),
            "avatar": "" if anonymous else (row.get("author_avatar") or ""),
            "date": str(row.get("created_at") or "")[5:16],
        },
        "likes": int(row.get("like_count") or 0),
        "comment_count": int(row.get("comment_count") or 0),
        "commentCount": int(row.get("comment_count") or 0),
        "liked": bool(row.get("liked")),
        "comments": row.get("comments") or [],
        "status": row.get("status") or "",
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _serialize_comment(row: dict[str, Any]) -> dict[str, Any]:
    anonymous = bool(row.get("anonymous"))
    return {
        "id": row.get("id"),
        "post_id": row.get("post_id"),
        "user_id": row.get("user_id"),
        "author": "匿名铲屎官" if anonymous else (row.get("author_name") or "我"),
        "avatar": "" if anonymous else (row.get("author_avatar") or ""),
        "content": row.get("content") or "",
        "likes": int(row.get("like_count") or 0),
        "date": str(row.get("created_at") or "")[5:16],
        "created_at": str(row.get("created_at") or ""),
    }


def init_miniprogram_moment_tables() -> None:
    init_miniprogram_cat_profile_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    cat_profile_id CHAR(32) NULL,
                    category_code VARCHAR(16) NOT NULL,
                    category_name VARCHAR(32) NOT NULL,
                    category_color VARCHAR(16) NULL,
                    title VARCHAR(80) NOT NULL,
                    content TEXT NOT NULL,
                    images_json LONGTEXT NOT NULL,
                    visibility VARCHAR(16) NOT NULL DEFAULT 'public',
                    breed VARCHAR(64) NULL,
                    age_text VARCHAR(64) NULL,
                    weight_kg DECIMAL(5,2) NULL,
                    cat_name VARCHAR(64) NULL,
                    author_name VARCHAR(64) NULL,
                    author_avatar VARCHAR(1024) NULL,
                    like_count INT NOT NULL DEFAULT 0,
                    comment_count INT NOT NULL DEFAULT 0,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_mini_moment_feed (visibility, status, created_at),
                    KEY idx_mini_moment_category (category_code, visibility, status, created_at),
                    KEY idx_mini_moment_user (user_id, status, created_at),
                    KEY idx_mini_moment_cat_profile (cat_profile_id, status, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {LIKE_TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    post_id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uniq_mini_moment_like_user (post_id, user_id),
                    KEY idx_mini_moment_like_user (user_id, status, created_at),
                    KEY idx_mini_moment_like_post (post_id, status, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {COMMENT_TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    post_id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    content TEXT NOT NULL,
                    author_name VARCHAR(64) NULL,
                    author_avatar VARCHAR(1024) NULL,
                    anonymous TINYINT NOT NULL DEFAULT 0,
                    like_count INT NOT NULL DEFAULT 0,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_mini_moment_comment_post (post_id, status, created_at),
                    KEY idx_mini_moment_comment_user (user_id, status, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def list_moment_categories() -> dict[str, Any]:
    return {"ok": True, "items": list(CATEGORIES.values())}


def _load_cat_profile_snapshot(cursor, user_id: str, cat_profile_id: str | None) -> dict[str, Any]:
    if not cat_profile_id:
        return {}
    cursor.execute(
        f"""
        SELECT id,name,breed,sex,age_text,weight_kg
        FROM {CAT_PROFILE_TABLE}
        WHERE id=%s AND user_id=%s AND status='active'
        LIMIT 1
        """,
        (cat_profile_id, user_id),
    )
    profile = cursor.fetchone()
    if not profile:
        raise LookupError("猫咪档案不存在")
    return profile


def _has_cat_profile_payload(data: dict[str, Any]) -> bool:
    return any([
        data.get("cat_name"),
        data.get("sex"),
        data.get("breed"),
        data.get("age_text"),
        data.get("weight_kg") is not None,
    ])


def _default_cat_name(data: dict[str, Any]) -> str:
    return data.get("cat_name") or "我的猫咪"


def _ensure_cat_profile_for_moment(cursor, data: dict[str, Any], now: str) -> dict[str, Any]:
    if data["cat_profile_id"]:
        return _load_cat_profile_snapshot(cursor, data["user_id"], data["cat_profile_id"])
    if not _has_cat_profile_payload(data):
        return {}

    cat_name = _default_cat_name(data)
    cursor.execute(
        f"""
        SELECT id,name,breed,sex,age_text,weight_kg
        FROM {CAT_PROFILE_TABLE}
        WHERE user_id=%s AND name=%s AND status='active'
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (data["user_id"], cat_name),
    )
    profile = cursor.fetchone()
    if profile:
        cursor.execute(
            f"""
            UPDATE {CAT_PROFILE_TABLE}
            SET breed=%s, sex=%s, age_text=%s, weight_kg=%s, updated_at=%s
            WHERE id=%s
            """,
            (
                data["breed"] or profile.get("breed") or None,
                data["sex"] or profile.get("sex") or None,
                data["age_text"] or profile.get("age_text") or None,
                data["weight_kg"] if data["weight_kg"] is not None else profile.get("weight_kg"),
                now,
                profile["id"],
            ),
        )
        profile.update({
            "breed": data["breed"] or profile.get("breed"),
            "sex": data["sex"] or profile.get("sex"),
            "age_text": data["age_text"] or profile.get("age_text"),
            "weight_kg": data["weight_kg"] if data["weight_kg"] is not None else profile.get("weight_kg"),
        })
        data["cat_profile_id"] = profile["id"]
        return profile

    profile_id = uuid.uuid4().hex
    cursor.execute(
        f"""
        INSERT INTO {CAT_PROFILE_TABLE} (
            id,user_id,name,breed,sex,neutered,birthday,age_text,age_months,weight_kg,
            avatar_url,allergies_json,diseases_json,symptoms_json,notes,is_default,status,created_at,updated_at
        ) VALUES (%s,%s,%s,%s,%s,NULL,NULL,%s,NULL,%s,NULL,%s,%s,%s,NULL,0,'active',%s,%s)
        """,
        (
            profile_id, data["user_id"], cat_name, data["breed"] or None, data["sex"] or None, data["age_text"] or None,
            data["weight_kg"], _json_dumps([]), _json_dumps([]), _json_dumps([]), now, now,
        ),
    )
    data["cat_profile_id"] = profile_id
    return {
        "id": profile_id,
        "name": cat_name,
        "breed": data["breed"],
        "sex": data["sex"],
        "age_text": data["age_text"],
        "weight_kg": data["weight_kg"],
    }


def create_moment(payload: dict[str, Any]) -> dict[str, Any]:
    data = _normalize_payload(payload)
    from services.miniprogram_content_review_service import check_moment_payload_with_wechat

    safety_result = check_moment_payload_with_wechat(data)
    init_miniprogram_moment_tables()
    post_id = uuid.uuid4().hex
    now = _now()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            profile = _ensure_cat_profile_for_moment(cursor, data, now)
            breed = data["breed"] or _clean(profile.get("breed"), 64)
            age_text = data["age_text"] or _clean(profile.get("age_text"), 64)
            weight_kg = data["weight_kg"] if data["weight_kg"] is not None else profile.get("weight_kg")
            cat_name = data["cat_name"] or _clean(profile.get("name"), 64)
            cursor.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    id,user_id,cat_profile_id,category_code,category_name,category_color,title,content,
                    images_json,visibility,breed,age_text,weight_kg,cat_name,author_name,author_avatar,
                    like_count,comment_count,status,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,'active',%s,%s)
                """,
                (
                    post_id, data["user_id"], data["cat_profile_id"], data["category_code"],
                    data["category_name"], data["category_color"], data["title"], data["content"],
                    _json_dumps(data["images"]), data["visibility"], breed or None, age_text or None,
                    weight_kg, cat_name or None, data["author_name"] or None, data["author_avatar"] or None,
                    now, now,
                ),
            )
        conn.commit()
    return {"ok": True, "item": get_moment(post_id, viewer_user_id=data["user_id"]), "safety": safety_result}


def list_moments(
    *,
    user_id: Any = "",
    category_code: Any = "",
    visibility: Any = "public",
    include_private: bool = False,
    limit: Any = 20,
) -> dict[str, Any]:
    try:
        cleaned_limit = max(1, min(int(limit or 20), MAX_LIST_LIMIT))
    except (TypeError, ValueError):
        cleaned_limit = 20
    filters = ["status='active'"]
    params: list[Any] = []
    if include_private:
        cleaned_user_id = _clean_user_id(user_id)
        filters.append("user_id=%s")
        params.append(cleaned_user_id)
    else:
        cleaned_visibility = _clean_visibility(visibility)
        filters.append("visibility=%s")
        params.append(cleaned_visibility)
    if category_code:
        filters.append("category_code=%s")
        params.append(_clean_category(category_code)["code"])
    params.append(cleaned_limit)
    init_miniprogram_moment_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM {TABLE_NAME}
                WHERE {' AND '.join(filters)}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = list(cursor.fetchall() or [])
    return {"ok": True, "count": len(rows), "items": [_serialize(row) for row in rows]}


def get_moment(post_id: Any, *, viewer_user_id: Any = "") -> dict[str, Any]:
    cleaned_post_id = _clean_post_id(post_id)
    viewer = _clean(viewer_user_id, 128)
    init_miniprogram_moment_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {TABLE_NAME} WHERE id=%s AND status='active' LIMIT 1", (cleaned_post_id,))
            row = cursor.fetchone()
            comments = _list_comments(cursor, cleaned_post_id, DEFAULT_COMMENT_LIMIT)
            liked = _is_liked(cursor, cleaned_post_id, viewer) if viewer else False
    if not row:
        raise LookupError("瞬间不存在")
    if row.get("visibility") == "private" and row.get("user_id") != viewer:
        raise LookupError("瞬间不存在")
    row["comments"] = comments
    row["liked"] = liked
    return _serialize(row)


def _ensure_visible_post(cursor, post_id: str, viewer_user_id: str = "") -> dict[str, Any]:
    cursor.execute(f"SELECT * FROM {TABLE_NAME} WHERE id=%s AND status='active' LIMIT 1", (post_id,))
    row = cursor.fetchone()
    if not row:
        raise LookupError("瞬间不存在")
    if row.get("visibility") == "private" and row.get("user_id") != viewer_user_id:
        raise LookupError("瞬间不存在")
    return row


def _is_liked(cursor, post_id: str, user_id: str) -> bool:
    if not user_id:
        return False
    cursor.execute(
        f"SELECT id FROM {LIKE_TABLE_NAME} WHERE post_id=%s AND user_id=%s AND status='active' LIMIT 1",
        (post_id, user_id),
    )
    return bool(cursor.fetchone())


def _list_comments(cursor, post_id: str, limit: int) -> list[dict[str, Any]]:
    cursor.execute(
        f"""
        SELECT * FROM {COMMENT_TABLE_NAME}
        WHERE post_id=%s AND status='active'
        ORDER BY created_at ASC
        LIMIT %s
        """,
        (post_id, limit),
    )
    return [_serialize_comment(row) for row in list(cursor.fetchall() or [])]


def list_moment_comments(post_id: Any, *, viewer_user_id: Any = "", limit: Any = DEFAULT_COMMENT_LIMIT) -> dict[str, Any]:
    cleaned_post_id = _clean_post_id(post_id)
    viewer = _clean(viewer_user_id, 128)
    try:
        cleaned_limit = max(1, min(int(limit or DEFAULT_COMMENT_LIMIT), MAX_LIST_LIMIT))
    except (TypeError, ValueError):
        cleaned_limit = DEFAULT_COMMENT_LIMIT
    init_miniprogram_moment_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            _ensure_visible_post(cursor, cleaned_post_id, viewer)
            comments = _list_comments(cursor, cleaned_post_id, cleaned_limit)
    return {"ok": True, "count": len(comments), "items": comments}


def set_moment_like(post_id: Any, payload: dict[str, Any], *, liked: bool) -> dict[str, Any]:
    cleaned_post_id = _clean_post_id(post_id)
    cleaned_user_id = _clean_user_id((payload or {}).get("user_id"))
    init_miniprogram_moment_tables()
    now = _now()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            _ensure_visible_post(cursor, cleaned_post_id, cleaned_user_id)
            was_liked = _is_liked(cursor, cleaned_post_id, cleaned_user_id)
            if liked and not was_liked:
                cursor.execute(
                    f"""
                    INSERT INTO {LIKE_TABLE_NAME} (id,post_id,user_id,status,created_at,updated_at)
                    VALUES (%s,%s,%s,'active',%s,%s)
                    ON DUPLICATE KEY UPDATE status='active', updated_at=VALUES(updated_at)
                    """,
                    (uuid.uuid4().hex, cleaned_post_id, cleaned_user_id, now, now),
                )
                cursor.execute(
                    f"UPDATE {TABLE_NAME} SET like_count=like_count+1, updated_at=%s WHERE id=%s",
                    (now, cleaned_post_id),
                )
            elif not liked and was_liked:
                cursor.execute(
                    f"UPDATE {LIKE_TABLE_NAME} SET status='deleted', updated_at=%s WHERE post_id=%s AND user_id=%s AND status='active'",
                    (now, cleaned_post_id, cleaned_user_id),
                )
                cursor.execute(
                    f"UPDATE {TABLE_NAME} SET like_count=GREATEST(like_count-1, 0), updated_at=%s WHERE id=%s",
                    (now, cleaned_post_id),
                )
            cursor.execute(f"SELECT like_count FROM {TABLE_NAME} WHERE id=%s LIMIT 1", (cleaned_post_id,))
            row = cursor.fetchone() or {}
        conn.commit()
    return {"ok": True, "id": cleaned_post_id, "liked": liked, "likes": int(row.get("like_count") or 0)}


def create_moment_comment(post_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload or {}
    cleaned_post_id = _clean_post_id(post_id)
    cleaned_user_id = _clean_user_id(payload.get("user_id"))
    content = _clean(payload.get("content") or payload.get("text") or payload.get("message"), MAX_COMMENT_LENGTH)
    if not content:
        raise ValueError("content 不能为空")
    safety_payload = {"user_id": cleaned_user_id, "content": content}
    from services.miniprogram_content_review_service import check_comment_payload_with_wechat

    safety_result = check_comment_payload_with_wechat(safety_payload)
    anonymous = 1 if str(payload.get("anonymous") or "").lower() in {"1", "true", "yes"} else 0
    author_name = _clean(payload.get("author_name"), 64)
    author_avatar = _clean(payload.get("author_avatar"), 1024)
    comment_id = uuid.uuid4().hex
    now = _now()
    init_miniprogram_moment_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            _ensure_visible_post(cursor, cleaned_post_id, cleaned_user_id)
            cursor.execute(
                f"""
                INSERT INTO {COMMENT_TABLE_NAME} (
                    id,post_id,user_id,content,author_name,author_avatar,anonymous,like_count,status,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,0,'active',%s,%s)
                """,
                (
                    comment_id, cleaned_post_id, cleaned_user_id, content,
                    author_name or None, author_avatar or None, anonymous, now, now,
                ),
            )
            cursor.execute(
                f"UPDATE {TABLE_NAME} SET comment_count=comment_count+1, updated_at=%s WHERE id=%s",
                (now, cleaned_post_id),
            )
            cursor.execute(f"SELECT * FROM {COMMENT_TABLE_NAME} WHERE id=%s LIMIT 1", (comment_id,))
            comment = _serialize_comment(cursor.fetchone() or {})
        conn.commit()
    return {"ok": True, "item": comment, "safety": safety_result}


def delete_moment_comment(user_id: Any, comment_id: Any) -> dict[str, Any]:
    cleaned_user_id = _clean_user_id(user_id)
    cleaned_comment_id = _clean_comment_id(comment_id)
    now = _now()
    init_miniprogram_moment_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT post_id FROM {COMMENT_TABLE_NAME} WHERE id=%s AND user_id=%s AND status='active' LIMIT 1",
                (cleaned_comment_id, cleaned_user_id),
            )
            comment = cursor.fetchone()
            if not comment:
                raise LookupError("留言不存在")
            cursor.execute(
                f"UPDATE {COMMENT_TABLE_NAME} SET status='deleted', updated_at=%s WHERE id=%s",
                (now, cleaned_comment_id),
            )
            cursor.execute(
                f"UPDATE {TABLE_NAME} SET comment_count=GREATEST(comment_count-1, 0), updated_at=%s WHERE id=%s",
                (now, comment["post_id"]),
            )
        conn.commit()
    return {"ok": True, "id": cleaned_comment_id}


def delete_moment(user_id: Any, post_id: Any) -> dict[str, Any]:
    cleaned_user_id = _clean_user_id(user_id)
    cleaned_post_id = _clean_post_id(post_id)
    init_miniprogram_moment_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE_NAME} SET status='deleted', updated_at=%s WHERE id=%s AND user_id=%s AND status='active'",
                (_now(), cleaned_post_id, cleaned_user_id),
            )
            if cursor.rowcount == 0:
                raise LookupError("瞬间不存在")
        conn.commit()
    return {"ok": True, "id": cleaned_post_id}
