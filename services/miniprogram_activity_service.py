"""活动码领取服务（小程序发帖活动）。

流程：用户在小程序发帖成功 → 前端携带登录令牌调用领取接口 → 服务端校验
（1）调用者已登录；（2）post_id 归属该用户且帖子仍有效；（3）活动在有效期内；
（4）再原子地分配一个未被领取的活动码。一人一码（按 activity_id 维度）。

并发安全依赖数据库唯一键 + ``FOR UPDATE SKIP LOCKED``，不依赖应用层加锁：
- ``uq_activity_claim_user (activity_id, user_id)`` 保证一人一码；
- ``uq_activity_claim_code (code_id)`` 保证一码一人；
- 码行加排他锁后再插入领取记录，两个请求不会拿到同一个码。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import pymysql
from pymysql.cursors import DictCursor

from app_config import get_mysql_config

logger = logging.getLogger(__name__)

ACTIVITY_TABLE = "miniprogram_activity"
CODE_TABLE = "miniprogram_activity_code"
CLAIM_TABLE = "miniprogram_activity_claim"
POST_TABLE = "miniprogram_cat_moment_post"
USER_TABLE = "miniprogram_user"

# 业务状态（与小程序端约定，均为 HTTP 200 返回）
STATUS_SUCCESS = "SUCCESS"
STATUS_ALREADY_CLAIMED = "ALREADY_CLAIMED"
STATUS_OUT_OF_STOCK = "OUT_OF_STOCK"
STATUS_NOT_CLAIMED = "NOT_CLAIMED"
STATUS_BUSY = "BUSY"

CLAIM_RETRY_TIMES = 3
# 1213 = 死锁回滚，1205 = 锁等待超时；均为可重试的瞬时冲突
RETRYABLE_MYSQL_ERRORS = {1205, 1213}

MAX_CODE_LENGTH = 255
MAX_BATCH_LENGTH = 64
CODE_INSERT_BATCH = 500


class PostVerificationError(PermissionError):
    """帖子不存在、不属于当前用户或已失效。映射为 HTTP 403。"""


def _connect(autocommit: bool = False):
    """新建连接。

    领取流程使用 READ COMMITTED：判断"还有没有码"时需要看到其他事务刚提交的
    领取记录，可重复读（MySQL 默认）会使用旧快照，容易把 BUSY 误判成 OUT_OF_STOCK。
    """
    conn = pymysql.connect(**get_mysql_config(), cursorclass=DictCursor, autocommit=autocommit)
    with conn.cursor() as cursor:
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    return conn


def _clean_text(value: Any, max_length: int | None = None) -> str:
    text = str(value if value is not None else "").strip()
    return text[:max_length] if max_length else text


def _clean_positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须是正整数")
    return parsed


def _clean_user_id(value: Any) -> str:
    user_id = _clean_text(value, 128)
    if not user_id:
        raise PermissionError("请先登录")
    return user_id


def _clean_post_id(value: Any) -> str:
    post_id = _clean_text(value, 32)
    if not post_id:
        raise ValueError("post_id 不能为空")
    return post_id


def _business_result(status: str, message: str, *, activity_id: int, code: str | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "message": message,
        "activity_id": activity_id,
        "code": code,
    }


def init_miniprogram_activity_tables() -> None:
    """建表（幂等）。首次调用会自动创建三张表，无需手工执行 SQL。"""
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {ACTIVITY_TABLE} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    name VARCHAR(128) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    required_category_code VARCHAR(16) NULL,
                    starts_at DATETIME NULL,
                    ends_at DATETIME NULL,
                    remark VARCHAR(255) NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_mini_activity_status (status, ends_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CODE_TABLE} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    activity_id BIGINT UNSIGNED NOT NULL,
                    code VARCHAR(255) NOT NULL,
                    batch VARCHAR(64) NULL,
                    credits_per_code DECIMAL(10,2) NULL,
                    redeem_ends_at DATETIME NULL,
                    sequence_no INT NULL,
                    source_status VARCHAR(16) NULL,
                    snapshot_at DATETIME NULL,
                    created_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_mini_activity_code (activity_id, code),
                    KEY idx_mini_activity_code_pick (activity_id, id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            # 兼容已建好的旧表：按需补齐后加的列（1060 = 列已存在，并发建表时忽略）
            for column, definition in (
                ("credits_per_code", "DECIMAL(10,2) NULL AFTER batch"),
                ("redeem_ends_at", "DATETIME NULL AFTER credits_per_code"),
                ("sequence_no", "INT NULL AFTER redeem_ends_at"),
                ("source_status", "VARCHAR(16) NULL AFTER sequence_no"),
                ("snapshot_at", "DATETIME NULL AFTER source_status"),
            ):
                cursor.execute(f"SHOW COLUMNS FROM {CODE_TABLE} LIKE %s", (column,))
                if cursor.fetchone():
                    continue
                try:
                    cursor.execute(f"ALTER TABLE {CODE_TABLE} ADD COLUMN {column} {definition}")
                except pymysql.MySQLError as exc:
                    if exc.args[0] != 1060:
                        raise
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CLAIM_TABLE} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    activity_id BIGINT UNSIGNED NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    post_id VARCHAR(32) NOT NULL,
                    code_id BIGINT UNSIGNED NOT NULL,
                    claimed_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_mini_claim_activity_user (activity_id, user_id),
                    UNIQUE KEY uq_mini_claim_code (code_id),
                    KEY idx_mini_claim_user (user_id, activity_id),
                    KEY idx_mini_claim_post (post_id),
                    CONSTRAINT fk_mini_claim_code FOREIGN KEY (code_id) REFERENCES {CODE_TABLE} (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()
    finally:
        conn.close()


def _load_activity(cursor, activity_id: int) -> dict[str, Any] | None:
    """读取活动配置，同时算出当前时间是否落在有效期内（时间统一按 UTC）。"""
    cursor.execute(
        f"""
        SELECT a.*,
               (a.starts_at IS NOT NULL AND UTC_TIMESTAMP() < a.starts_at) AS not_started,
               (a.ends_at IS NOT NULL AND UTC_TIMESTAMP() > a.ends_at) AS ended
        FROM {ACTIVITY_TABLE} a
        WHERE a.id=%s
        LIMIT 1
        """,
        (activity_id,),
    )
    return cursor.fetchone()


def _assert_activity_open(cursor, activity_id: int, post: dict[str, Any]) -> dict[str, Any] | None:
    """校验活动可领取。返回活动配置（未配置活动时返回 None）。

    设计取舍：活动表是本次新增的，允许“只按 activity_id 隔离库存”的轻量用法，
    即活动未登记时不拦截；一旦登记，则严格校验状态、起止时间与限定分类。
    若运营要求强约束，登记活动即可，无需改代码。
    """
    activity = _load_activity(cursor, activity_id)
    if activity is None:
        logger.warning("活动 %s 未在 %s 中登记，仅按 activity_id 隔离库存", activity_id, ACTIVITY_TABLE)
        return None

    status = _clean_text(activity.get("status"), 16).lower() or "active"
    if status != "active":
        raise ValueError("活动未开放或已结束")

    if activity.get("not_started"):
        raise ValueError("活动尚未开始")
    if activity.get("ended"):
        raise ValueError("活动已结束")

    required_category = _clean_text(activity.get("required_category_code"), 16).upper()
    if required_category and required_category != _clean_text(post.get("category_code"), 16).upper():
        raise ValueError("该活动仅限指定话题的帖子领取")
    return activity


def _load_post(cursor, post_id: str) -> dict[str, Any]:
    cursor.execute(
        f"SELECT id, user_id, category_code, status, visibility, created_at FROM {POST_TABLE} WHERE id=%s LIMIT 1",
        (post_id,),
    )
    return cursor.fetchone()


def _assert_post_belongs_to_user(cursor, post_id: str, user_id: str) -> dict[str, Any]:
    """校验帖子真实、已发布且属于当前用户。查不到或不匹配一律拒绝（fail closed）。"""
    post = _load_post(cursor, post_id)
    if not post:
        raise PostVerificationError("帖子不存在或尚未发布")
    if _clean_text(post.get("status"), 16) != "active":
        raise PostVerificationError("帖子已失效，无法领取活动码")
    if _clean_text(post.get("user_id"), 128) != user_id:
        raise PostVerificationError("仅成功发布活动帖子后可以领取")
    return post


def _previous_code(cursor, activity_id: int, user_id: str) -> str | None:
    cursor.execute(
        f"""
        SELECT c.code FROM {CLAIM_TABLE} r
        JOIN {CODE_TABLE} c ON c.id = r.code_id
        WHERE r.activity_id=%s AND r.user_id=%s
        LIMIT 1
        """,
        (activity_id, user_id),
    )
    row = cursor.fetchone()
    return _clean_text(row.get("code")) if row else None


def _previous_code_isolated(activity_id: int, user_id: str) -> str | None:
    """新开连接读取已领码，用于唯一键冲突后的兜底查询。"""
    conn = _connect(autocommit=True)
    try:
        with conn.cursor() as cursor:
            return _previous_code(cursor, activity_id, user_id)
    finally:
        conn.close()


def _remaining_count(cursor, activity_id: int) -> int:
    cursor.execute(
        f"""
        SELECT COUNT(*) AS n FROM {CODE_TABLE} c
        WHERE c.activity_id=%s
          AND NOT EXISTS (SELECT 1 FROM {CLAIM_TABLE} r WHERE r.code_id=c.id)
        """,
        (activity_id,),
    )
    row = cursor.fetchone() or {}
    return int(row.get("n") or 0)


_PICK_CODE_SQL = f"""
    SELECT c.id, c.code FROM {CODE_TABLE} c
    WHERE c.activity_id=%s
      AND NOT EXISTS (SELECT 1 FROM {CLAIM_TABLE} r WHERE r.code_id=c.id)
    ORDER BY c.id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
"""


def claim_activity_code(*, user_id: Any, activity_id: Any, post_id: Any) -> dict[str, Any]:
    """领取活动码。

    返回 ``{"ok": True, "status": ..., "message": ..., "code": ...}``。
    业务状态：SUCCESS / ALREADY_CLAIMED / OUT_OF_STOCK / BUSY。
    鉴权与参数问题抛 ``PermissionError`` / ``ValueError``，由接口层映射状态码。
    """
    cleaned_user_id = _clean_user_id(user_id)
    cleaned_activity_id = _clean_positive_int(activity_id, "activity_id")
    cleaned_post_id = _clean_post_id(post_id)

    init_miniprogram_activity_tables()

    last_error: Exception | None = None
    for _ in range(CLAIM_RETRY_TIMES):
        conn = _connect()
        try:
            with conn.cursor() as cursor:
                post = _assert_post_belongs_to_user(cursor, cleaned_post_id, cleaned_user_id)
                _assert_activity_open(cursor, cleaned_activity_id, post)

                existing = _previous_code(cursor, cleaned_activity_id, cleaned_user_id)
                if existing is not None:
                    conn.rollback()
                    return _business_result(
                        STATUS_ALREADY_CLAIMED, "您已领取过活动码", activity_id=cleaned_activity_id, code=existing
                    )

                cursor.execute(_PICK_CODE_SQL, (cleaned_activity_id,))
                available = cursor.fetchone()
                if not available:
                    # SKIP LOCKED 也会跳过"正被其他请求锁定"的码，需区分真正缺货与瞬时繁忙
                    remaining = _remaining_count(cursor, cleaned_activity_id)
                    conn.rollback()
                    if remaining > 0:
                        return _business_result(STATUS_BUSY, "领取人数较多，请稍后重试", activity_id=cleaned_activity_id)
                    return _business_result(STATUS_OUT_OF_STOCK, "活动码已领完", activity_id=cleaned_activity_id)

                cursor.execute(
                    f"""
                    INSERT INTO {CLAIM_TABLE} (activity_id, user_id, post_id, code_id, claimed_at)
                    VALUES (%s,%s,%s,%s,UTC_TIMESTAMP())
                    """,
                    (cleaned_activity_id, cleaned_user_id, cleaned_post_id, available["id"]),
                )
            conn.commit()
            return _business_result(
                STATUS_SUCCESS, "领取成功", activity_id=cleaned_activity_id, code=_clean_text(available["code"])
            )
        except pymysql.err.IntegrityError as exc:
            # 并发下同一用户重复提交 / 同一个码被抢：兜底查一次已领码
            conn.rollback()
            last_error = exc
            existing = _previous_code_isolated(cleaned_activity_id, cleaned_user_id)
            if existing is not None:
                return _business_result(
                    STATUS_ALREADY_CLAIMED, "您已领取过活动码", activity_id=cleaned_activity_id, code=existing
                )
        except pymysql.err.OperationalError as exc:
            conn.rollback()
            last_error = exc
            if exc.args and exc.args[0] in RETRYABLE_MYSQL_ERRORS:
                continue
            raise
        finally:
            conn.close()

    if isinstance(last_error, pymysql.err.OperationalError):
        return _business_result(STATUS_BUSY, "领取人数较多，请稍后重试", activity_id=cleaned_activity_id)
    raise RuntimeError("活动码领取失败，请稍后重试")


def get_my_activity_code(*, user_id: Any, activity_id: Any) -> dict[str, Any]:
    """查询当前用户在某活动下已领取的码，用于弹窗关闭后恢复展示。"""
    cleaned_user_id = _clean_user_id(user_id)
    cleaned_activity_id = _clean_positive_int(activity_id, "activity_id")
    init_miniprogram_activity_tables()
    conn = _connect(autocommit=True)
    try:
        with conn.cursor() as cursor:
            existing = _previous_code(cursor, cleaned_activity_id, cleaned_user_id)
    finally:
        conn.close()
    if existing is None:
        return _business_result(STATUS_NOT_CLAIMED, "尚未领取", activity_id=cleaned_activity_id)
    return _business_result(STATUS_ALREADY_CLAIMED, "已领取", activity_id=cleaned_activity_id, code=existing)


def _serialize_activity(activity: dict[str, Any] | None) -> dict[str, Any] | None:
    """把活动配置整理成接口友好结构（时间统一为字符串，去掉内部计算列）。"""
    if not activity:
        return None
    keys = ("id", "name", "status", "required_category_code", "starts_at", "ends_at", "remark", "created_at", "updated_at")
    serialized: dict[str, Any] = {}
    for key in keys:
        value = activity.get(key)
        if isinstance(value, datetime):
            value = value.strftime("%Y-%m-%d %H:%M:%S")
        serialized[key] = value
    return serialized


def list_user_activity_claims(
    *,
    user_id: Any = "",
    openid: Any = "",
    activity_id: Any = None,
) -> dict[str, Any]:
    """按用户查领取记录（运营/核销对账用）。

    支持三种定位方式：内部 user_id、微信 openid（自动换算）、不指定用户时
    列出某活动的全部领取记录。activity_id 可选，缺省查所有活动。
    """
    cleaned_user_id = _clean_text(user_id, 128)
    cleaned_openid = _clean_text(openid, 128)
    if not cleaned_user_id and not cleaned_openid:
        raise ValueError("user_id 与 openid 至少提供一个")
    cleaned_activity_id = (
        _clean_positive_int(activity_id, "activity_id") if activity_id not in (None, "", 0, "0") else None
    )

    conn = _connect(autocommit=True)
    try:
        with conn.cursor() as cursor:
            # openid 优先换算成内部 user_id；两者都给时以 user_id 为准，openid 仅作校验
            user_row: dict[str, Any] | None = None
            if cleaned_user_id:
                cursor.execute(
                    f"SELECT id, openid, unionid, nickname, phone_number FROM {USER_TABLE} WHERE id=%s LIMIT 1",
                    (cleaned_user_id,),
                )
                user_row = cursor.fetchone()
            else:
                cursor.execute(
                    f"SELECT id, openid, unionid, nickname, phone_number FROM {USER_TABLE} WHERE openid=%s LIMIT 1",
                    (cleaned_openid,),
                )
                user_row = cursor.fetchone()
                if not user_row:
                    raise LookupError("openid 未找到对应用户")
                cleaned_user_id = _clean_text(user_row.get("id"), 128)

            filters = ["r.user_id=%s"]
            params: list[Any] = [cleaned_user_id]
            if cleaned_activity_id is not None:
                filters.append("r.activity_id=%s")
                params.append(cleaned_activity_id)

            cursor.execute(
                f"""
                SELECT r.id, r.activity_id, r.user_id, r.post_id, r.claimed_at,
                       c.code, c.batch, c.credits_per_code, c.redeem_ends_at, c.sequence_no
                FROM {CLAIM_TABLE} r
                JOIN {CODE_TABLE} c ON c.id = r.code_id
                WHERE {' AND '.join(filters)}
                ORDER BY r.claimed_at DESC, r.id DESC
                """,
                params,
            )
            rows = list(cursor.fetchall() or [])
    finally:
        conn.close()

    claims = []
    for row in rows:
        claims.append(
            {
                "claim_id": row.get("id"),
                "activity_id": row.get("activity_id"),
                "post_id": row.get("post_id"),
                "code": _clean_text(row.get("code")),
                "batch": row.get("batch"),
                "credits_per_code": float(row["credits_per_code"])
                if isinstance(row.get("credits_per_code"), Decimal)
                else row.get("credits_per_code"),
                "redeem_ends_at": row["redeem_ends_at"].strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(row.get("redeem_ends_at"), datetime)
                else row.get("redeem_ends_at"),
                "sequence_no": row.get("sequence_no"),
                "claimed_at": row["claimed_at"].strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(row.get("claimed_at"), datetime)
                else row.get("claimed_at"),
            }
        )

    user_info: dict[str, Any] | None = None
    if user_row:
        user_info = {
            "user_id": _clean_text(user_row.get("id"), 128),
            "openid": user_row.get("openid"),
            "unionid": user_row.get("unionid"),
            "nickname": user_row.get("nickname"),
            "phone_number": user_row.get("phone_number"),
        }

    return {
        "ok": True,
        "user": user_info,
        "activity_id": cleaned_activity_id,
        "count": len(claims),
        "claims": claims,
    }


def get_activity_stats(activity_id: Any) -> dict[str, Any]:
    """运营用库存统计。"""
    cleaned_activity_id = _clean_positive_int(activity_id, "activity_id")
    init_miniprogram_activity_tables()
    conn = _connect(autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS n FROM {CODE_TABLE} WHERE activity_id=%s", (cleaned_activity_id,))
            total = int((cursor.fetchone() or {}).get("n") or 0)
            cursor.execute(
                f"""SELECT COUNT(*) AS n FROM {CLAIM_TABLE} r
                    JOIN {CODE_TABLE} c ON c.id=r.code_id WHERE r.activity_id=%s""",
                (cleaned_activity_id,),
            )
            claimed = int((cursor.fetchone() or {}).get("n") or 0)
            activity = _load_activity(cursor, cleaned_activity_id)
    finally:
        conn.close()
    return {
        "ok": True,
        "activity_id": cleaned_activity_id,
        "activity": _serialize_activity(activity),
        "total_codes": total,
        "claimed_codes": claimed,
        "remaining_codes": max(total - claimed, 0),
    }


def upsert_activity(
    *,
    activity_id: int,
    name: str,
    status: str = "active",
    required_category_code: str = "",
    starts_at: str | None = None,
    ends_at: str | None = None,
    remark: str = "",
) -> None:
    """登记/更新活动配置（供脚本与运维使用）。"""
    cleaned_id = _clean_positive_int(activity_id, "activity_id")
    cleaned_name = _clean_text(name, 128)
    if not cleaned_name:
        raise ValueError("name 不能为空")
    init_miniprogram_activity_tables()
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {ACTIVITY_TABLE}
                    (id, name, status, required_category_code, starts_at, ends_at, remark, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name), status=VALUES(status),
                    required_category_code=VALUES(required_category_code),
                    starts_at=VALUES(starts_at), ends_at=VALUES(ends_at),
                    remark=VALUES(remark), updated_at=UTC_TIMESTAMP()
                """,
                (
                    cleaned_id,
                    cleaned_name,
                    _clean_text(status, 16) or "active",
                    _clean_text(required_category_code, 16).upper() or None,
                    starts_at or None,
                    ends_at or None,
                    _clean_text(remark, 255) or None,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _parse_utc_datetime(value: Any, field_name: str = "") -> datetime | None:
    """解析导出数据里的时间（支持 ``2026-11-06T02:48:00Z``），统一转成 UTC 朴素时间。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    raw = str(value).strip().replace("T", " ").removesuffix("Z").split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"{field_name or '时间'} 格式无效：{value}")


def _clean_optional_int(value: Any) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _clean_optional_decimal(value: Any) -> Decimal | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _normalize_code_records(records: Iterable[Any], default_batch: str | None) -> tuple[list[dict[str, Any]], int]:
    """把「纯码列表」或「带字段的记录」统一成入库结构，按码去重。返回 (记录, 空行数)。"""
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    blank = 0
    for item in records:
        payload = {"code": item} if isinstance(item, str) else dict(item or {})
        code = _clean_text(payload.get("code"), MAX_CODE_LENGTH)
        if not code:
            blank += 1
            continue
        if code in seen:
            continue
        seen.add(code)
        normalized.append(
            {
                "code": code,
                "batch": _clean_text(payload.get("batch"), MAX_BATCH_LENGTH) or default_batch,
                "credits_per_code": _clean_optional_decimal(payload.get("credits_per_code")),
                "redeem_ends_at": _parse_utc_datetime(payload.get("redeem_ends_at"), "redeem_ends_at"),
                "sequence_no": _clean_optional_int(payload.get("sequence_no")),
                "source_status": _clean_text(payload.get("source_status") or payload.get("status"), 16) or None,
                "snapshot_at": _parse_utc_datetime(payload.get("snapshot_at"), "snapshot_at"),
            }
        )
    return normalized, blank


def import_activity_code_records(
    *,
    activity_id: Any,
    records: Iterable[Any],
    batch: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """批量导入活动码，支持附带每码的积分数、兑换截止、批次序号等字段。"""
    cleaned_activity_id = _clean_positive_int(activity_id, "activity_id")
    default_batch = _clean_text(batch, MAX_BATCH_LENGTH) or None
    normalized, blank = _normalize_code_records(records, default_batch)
    if not normalized:
        raise ValueError("没有可导入的活动码")

    if dry_run:
        return {
            "dry_run": True,
            "activity_id": cleaned_activity_id,
            "input_unique": len(normalized),
            "blank_lines": blank,
            "would_insert": len(normalized),
        }

    init_miniprogram_activity_tables()
    inserted = 0
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            for start in range(0, len(normalized), CODE_INSERT_BATCH):
                chunk = normalized[start:start + CODE_INSERT_BATCH]
                cursor.executemany(
                    f"""
                    INSERT INTO {CODE_TABLE}
                        (activity_id, code, batch, credits_per_code, redeem_ends_at,
                         sequence_no, source_status, snapshot_at, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())
                    ON DUPLICATE KEY UPDATE id=id
                    """,
                    [
                        (
                            cleaned_activity_id, row["code"], row["batch"], row["credits_per_code"],
                            row["redeem_ends_at"], row["sequence_no"], row["source_status"], row["snapshot_at"],
                        )
                        for row in chunk
                    ],
                )
                inserted += max(cursor.rowcount, 0)
        conn.commit()
    finally:
        conn.close()
    return {
        "dry_run": False,
        "activity_id": cleaned_activity_id,
        "input_unique": len(normalized),
        "blank_lines": blank,
        "inserted": inserted,
        "skipped_existing": len(normalized) - inserted,
    }


def import_activity_codes(
    *,
    activity_id: Any,
    codes: Iterable[str],
    batch: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """批量导入纯码（一行一个），已存在的码跳过。"""
    return import_activity_code_records(
        activity_id=activity_id, records=codes, batch=batch, dry_run=dry_run
    )


__all__ = [
    "ACTIVITY_TABLE",
    "CODE_TABLE",
    "CLAIM_TABLE",
    "STATUS_SUCCESS",
    "STATUS_ALREADY_CLAIMED",
    "STATUS_OUT_OF_STOCK",
    "STATUS_NOT_CLAIMED",
    "STATUS_BUSY",
    "PostVerificationError",
    "init_miniprogram_activity_tables",
    "claim_activity_code",
    "get_my_activity_code",
    "list_user_activity_claims",
    "get_activity_stats",
    "upsert_activity",
    "import_activity_codes",
    "import_activity_code_records",
]
