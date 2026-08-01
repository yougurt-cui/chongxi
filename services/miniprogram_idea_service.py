"""Idea crowdfunding feature for the WeChat mini-program."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import pymysql

from app_config import get_mysql_config


IDEA_TABLE = "miniprogram_idea"
SUPPORT_TABLE = "miniprogram_idea_support"
USER_TABLE = "miniprogram_user"

MAX_TITLE_LENGTH = 100
MAX_DESC_LENGTH = 2000
MAX_CATEGORY_LENGTH = 50
MAX_COVER_LENGTH = 1024
DEFAULT_LIST_LIMIT = 50

CATEGORY_PRESETS = {
    "PET_TOY": {"code": "PET_TOY", "name": "宠物玩具", "color": "#4CAF50"},
    "OWNER_TOY": {"code": "OWNER_TOY", "name": "宠物主玩具", "color": "#FF9800"},
    "SMART_DEVICE": {"code": "SMART_DEVICE", "name": "智能用品", "color": "#9C27B0"},
    "DAILY_USE": {"code": "DAILY_USE", "name": "生活用品", "color": "#2196F3"},
    "APPAREL": {"code": "APPAREL", "name": "宠物服饰", "color": "#009688"},
    "FOOD_RELATED": {"code": "FOOD_RELATED", "name": "食品周边", "color": "#E91E63"},
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect_db(autocommit: bool = False):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _gen_id() -> str:
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------

def init_idea_tables() -> None:
    try:
        from services.miniprogram_auth_service import init_miniprogram_auth_tables
        init_miniprogram_auth_tables()
    except Exception:
        pass
    with _connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {IDEA_TABLE} (
                    id CHAR(32) NOT NULL,
                    title VARCHAR({MAX_TITLE_LENGTH}) NOT NULL,
                    description TEXT NULL,
                    category VARCHAR({MAX_CATEGORY_LENGTH}) NOT NULL DEFAULT '',
                    category_color VARCHAR(16) NULL,
                    cover_url VARCHAR({MAX_COVER_LENGTH}) NOT NULL DEFAULT '',
                    target_support_count INT NOT NULL DEFAULT 100,
                    current_support_count INT NOT NULL DEFAULT 0,
                    sort_order INT NOT NULL DEFAULT 0,
                    status VARCHAR(16) NOT NULL DEFAULT 'draft',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_idea_status_sort (status, sort_order DESC, created_at DESC)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {SUPPORT_TABLE} (
                    id CHAR(32) NOT NULL,
                    idea_id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    created_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uniq_idea_user (idea_id, user_id),
                    KEY idx_support_idea (idea_id, created_at DESC)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
        conn.commit()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _idea_row_to_dict(row: dict[str, Any], *, user_id: str = "") -> dict[str, Any]:
    target = int(row.get("target_support_count") or 100)
    current = int(row.get("current_support_count") or 0)
    progress = min(100, round(current / max(target, 1) * 100))
    category_code = row.get("category") or ""
    preset = CATEGORY_PRESETS.get(category_code, {})
    category_color = row.get("category_color") or preset.get("color", "#999")
    category_name = preset.get("name", category_code)
    return {
        "id": row["id"],
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "category": category_code,
        "category_name": category_name,
        "category_color": category_color,
        "cover_url": row.get("cover_url") or "",
        "target_support_count": target,
        "current_support_count": current,
        "progress": progress,
        "sort_order": int(row.get("sort_order") or 0),
        "status": row.get("status") or "draft",
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


# ---------------------------------------------------------------------------
# Public queries
# ---------------------------------------------------------------------------

def list_ideas(*, user_id: str = "", limit: int = DEFAULT_LIST_LIMIT) -> list[dict[str, Any]]:
    init_idea_tables()
    limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), 200))
    with _connect_db(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM {}
                WHERE status='published'
                ORDER BY sort_order DESC, created_at DESC
                LIMIT %s
                """.format(IDEA_TABLE),
                (limit,),
            )
            rows = cur.fetchall()
    ideas = []
    for row in rows:
        idea = _idea_row_to_dict(row, user_id=user_id)
        idea["supporter_avatars"] = _get_supporter_avatars(row["id"], limit=4)
        if user_id:
            idea["is_supported"] = _user_has_supported(row["id"], user_id)
        else:
            idea["is_supported"] = False
        ideas.append(idea)
    return ideas


def get_idea(idea_id: str, *, user_id: str = "") -> dict[str, Any]:
    init_idea_tables()
    idea_id = _clean(idea_id, 32)
    if not idea_id:
        raise ValueError("创意ID不能为空")
    with _connect_db(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM {} WHERE id=%s LIMIT 1".format(IDEA_TABLE),
                (idea_id,),
            )
            row = cur.fetchone()
    if not row:
        raise LookupError("创意不存在")
    idea = _idea_row_to_dict(row, user_id=user_id)
    idea["supporter_avatars"] = _get_supporter_avatars(idea_id, limit=4)
    idea["is_supported"] = _user_has_supported(idea_id, user_id) if user_id else False
    return idea


def _get_supporter_avatars(idea_id: str, *, limit: int = 4) -> list[str]:
    try:
        with _connect_db(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.user_id, u.avatar_url
                    FROM {} s
                    LEFT JOIN {} u ON u.id = s.user_id
                    WHERE s.idea_id = %s
                    ORDER BY s.created_at DESC
                    LIMIT %s
                    """.format(SUPPORT_TABLE, USER_TABLE),
                    (idea_id, limit),
                )
                rows = cur.fetchall()
    except Exception:
        return []
    avatars = []
    for r in rows:
        url = (r.get("avatar_url") or "").strip()
        if url:
            avatars.append(url)
    return avatars


def _user_has_supported(idea_id: str, user_id: str) -> bool:
    with _connect_db(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM {} WHERE idea_id=%s AND user_id=%s LIMIT 1".format(SUPPORT_TABLE),
                (idea_id, user_id),
            )
            return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Support action
# ---------------------------------------------------------------------------

def support_idea(idea_id: str, user_id: str) -> dict[str, Any]:
    init_idea_tables()
    idea_id = _clean(idea_id, 32)
    user_id = _clean(user_id, 128)
    if not idea_id:
        raise ValueError("创意ID不能为空")
    if not user_id:
        raise PermissionError("请先登录")

    with _connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM {} WHERE id=%s LIMIT 1".format(IDEA_TABLE),
                (idea_id,),
            )
            idea = cur.fetchone()
            if not idea:
                raise LookupError("创意不存在")
            if idea.get("status") != "published":
                raise ValueError("该创意当前不支持投票")

            cur.execute(
                "SELECT 1 FROM {} WHERE idea_id=%s AND user_id=%s LIMIT 1".format(SUPPORT_TABLE),
                (idea_id, user_id),
            )
            if cur.fetchone():
                raise ValueError("你已经支持过这个创意啦")

            now = _now()
            support_id = _gen_id()
            cur.execute(
                """
                INSERT INTO {} (id, idea_id, user_id, created_at)
                VALUES (%s, %s, %s, %s)
                """.format(SUPPORT_TABLE),
                (support_id, idea_id, user_id, now),
            )
            cur.execute(
                """
                UPDATE {}
                SET current_support_count = current_support_count + 1, updated_at = %s
                WHERE id = %s
                """.format(IDEA_TABLE),
                (now, idea_id),
            )
        conn.commit()

    return get_idea(idea_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Admin CRUD
# ---------------------------------------------------------------------------

def create_idea(
    *,
    title: str,
    description: str = "",
    category: str = "",
    category_color: str = "",
    cover_url: str = "",
    target_support_count: int = 100,
    sort_order: int = 0,
) -> dict[str, Any]:
    init_idea_tables()
    title = _clean(title, MAX_TITLE_LENGTH)
    if not title:
        raise ValueError("创意名称不能为空")
    description = _clean(description, MAX_DESC_LENGTH)
    category = _clean(category, MAX_CATEGORY_LENGTH)
    if not category_color:
        preset = CATEGORY_PRESETS.get(category, {})
        category_color = preset.get("color", "#999")
    category_color = _clean(category_color, 16)
    cover_url = _clean(cover_url, MAX_COVER_LENGTH)
    target_support_count = max(1, int(target_support_count or 100))
    sort_order = int(sort_order or 0)

    idea_id = _gen_id()
    now = _now()
    with _connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO {}
                    (id, title, description, category, category_color, cover_url,
                     target_support_count, current_support_count, sort_order,
                     status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s,'draft',%s,%s)
                """.format(IDEA_TABLE),
                (
                    idea_id, title, description, category, category_color,
                    cover_url, target_support_count, sort_order, now, now,
                ),
            )
        conn.commit()
    return get_idea(idea_id)


def update_idea(idea_id: str, **fields: Any) -> dict[str, Any]:
    init_idea_tables()
    idea_id = _clean(idea_id, 32)
    if not idea_id:
        raise ValueError("创意ID不能为空")

    allowed = {
        "title", "description", "category", "category_color",
        "cover_url", "target_support_count", "sort_order",
    }
    sets = []
    values: list[Any] = []
    for key, val in fields.items():
        if key not in allowed:
            continue
        if key == "title":
            val = _clean(val, MAX_TITLE_LENGTH)
            if not val:
                raise ValueError("创意名称不能为空")
        elif key == "description":
            val = _clean(val, MAX_DESC_LENGTH)
        elif key == "category":
            val = _clean(val, MAX_CATEGORY_LENGTH)
        elif key == "category_color":
            val = _clean(val, 16)
        elif key == "cover_url":
            val = _clean(val, MAX_COVER_LENGTH)
        elif key == "target_support_count":
            val = max(1, int(val or 100))
        elif key == "sort_order":
            val = int(val or 0)
        sets.append(f"{key} = %s")
        values.append(val)

    if not sets:
        raise ValueError("没有可更新的字段")

    now = _now()
    sets.append("updated_at = %s")
    values.append(now)
    values.append(idea_id)

    with _connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE {} SET {} WHERE id = %s".format(IDEA_TABLE, ", ".join(sets)),
                tuple(values),
            )
        conn.commit()
    return get_idea(idea_id)


def set_idea_status(idea_id: str, status: str) -> dict[str, Any]:
    init_idea_tables()
    idea_id = _clean(idea_id, 32)
    if status not in ("draft", "published"):
        raise ValueError("状态只能是 draft 或 published")
    now = _now()
    with _connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE {} SET status=%s, updated_at=%s WHERE id=%s".format(IDEA_TABLE),
                (status, now, idea_id),
            )
        conn.commit()
    return get_idea(idea_id)


def list_ideas_admin(*, status: str = "", limit: int = 200) -> list[dict[str, Any]]:
    init_idea_tables()
    with _connect_db(autocommit=True) as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT * FROM {} WHERE status=%s ORDER BY sort_order DESC, created_at DESC LIMIT %s".format(IDEA_TABLE),
                    (status, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM {} ORDER BY sort_order DESC, created_at DESC LIMIT %s".format(IDEA_TABLE),
                    (limit,),
                )
            rows = cur.fetchall()
    return [_idea_row_to_dict(r) for r in rows]


def get_idea_support_count(idea_id: str) -> int:
    init_idea_tables()
    idea_id = _clean(idea_id, 32)
    with _connect_db(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_support_count FROM {} WHERE id=%s LIMIT 1".format(IDEA_TABLE),
                (idea_id,),
            )
            row = cur.fetchone()
    if not row:
        raise LookupError("创意不存在")
    return int(row.get("current_support_count") or 0)


def get_category_presets() -> list[dict[str, str]]:
    return [
        {"code": code, "name": item["name"], "color": item["color"]}
        for code, item in CATEGORY_PRESETS.items()
    ]
