"""Reporting and moderation workflow for mini-program cat moments."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pymysql

from app_config import get_mysql_config
from services.miniprogram_moment_service import TABLE_NAME as MOMENT_TABLE_NAME, init_miniprogram_moment_tables


REPORT_TABLE_NAME = "miniprogram_cat_moment_report"
MAX_REASON_TEXT_LENGTH = 64
MAX_DETAIL_LENGTH = 500
MAX_REVIEW_NOTE_LENGTH = 500
MAX_LIST_LIMIT = 100

REPORT_REASONS = {
    "spam": "广告/垃圾信息",
    "abuse": "辱骂/攻击",
    "false_info": "虚假或误导信息",
    "illegal": "违法违规内容",
    "privacy": "侵犯隐私",
    "animal_harm": "伤害动物",
    "other": "其他问题",
}

REPORT_STATUSES = {"pending", "processing", "resolved", "rejected"}
REVIEW_ACTIONS = {"processing", "resolve", "reject", "hide_post"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect_app(autocommit: bool = False):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


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


def _clean_user_id(value: Any, field_name: str = "user_id") -> str:
    user_id = _clean(value, 128)
    if not user_id:
        raise ValueError(f"{field_name} 不能为空")
    return user_id


def _clean_post_id(value: Any) -> str:
    post_id = _clean(value, 32)
    if not post_id:
        raise ValueError("post_id 不能为空")
    return post_id


def _normalize_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    reason_code = _clean(payload.get("reason_code") or payload.get("reason"), 32)
    reason_text = _clean(payload.get("reason_text"), MAX_REASON_TEXT_LENGTH)
    if reason_code and reason_code not in REPORT_REASONS:
        raise ValueError("reason_code 不支持")
    if not reason_text:
        reason_text = REPORT_REASONS.get(reason_code, "")
    if not reason_text:
        raise ValueError("举报原因不能为空")
    detail = _clean(payload.get("detail") or payload.get("description"), MAX_DETAIL_LENGTH)
    evidence = payload.get("evidence") or {}
    if not isinstance(evidence, (dict, list)):
        evidence = {"raw": _clean(evidence, 1024)}
    return {
        "reporter_user_id": _clean_user_id(payload.get("reporter_user_id") or payload.get("user_id"), "reporter_user_id"),
        "reason_code": reason_code or "other",
        "reason_text": reason_text,
        "detail": detail,
        "evidence": evidence,
    }


def _normalize_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    action = _clean(payload.get("action"), 32)
    if action not in REVIEW_ACTIONS:
        raise ValueError("action 仅支持 processing/resolve/reject/hide_post")
    return {
        "action": action,
        "operator_id": _clean(payload.get("operator_id") or payload.get("admin_id"), 128),
        "review_note": _clean(payload.get("review_note") or payload.get("note"), MAX_REVIEW_NOTE_LENGTH),
    }


def init_miniprogram_moment_report_tables() -> None:
    init_miniprogram_moment_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {REPORT_TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    post_id CHAR(32) NOT NULL,
                    reporter_user_id VARCHAR(128) NOT NULL,
                    reported_user_id VARCHAR(128) NOT NULL,
                    reason_code VARCHAR(32) NOT NULL,
                    reason_text VARCHAR(64) NOT NULL,
                    detail TEXT NULL,
                    evidence_json LONGTEXT NULL,
                    post_title VARCHAR(80) NULL,
                    post_content_preview VARCHAR(255) NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    review_note TEXT NULL,
                    operator_id VARCHAR(128) NULL,
                    handled_at DATETIME NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uniq_mini_moment_report_user_post (post_id, reporter_user_id),
                    KEY idx_mini_moment_report_status (status, created_at),
                    KEY idx_mini_moment_report_post (post_id, status),
                    KEY idx_mini_moment_report_reporter (reporter_user_id, created_at),
                    KEY idx_mini_moment_report_reported (reported_user_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "post_id": row.get("post_id") or "",
        "reporter_user_id": row.get("reporter_user_id") or "",
        "reported_user_id": row.get("reported_user_id") or "",
        "reason_code": row.get("reason_code") or "",
        "reason_text": row.get("reason_text") or "",
        "detail": row.get("detail") or "",
        "evidence": _json_loads(row.get("evidence_json"), {}),
        "post_title": row.get("post_title") or "",
        "post_content_preview": row.get("post_content_preview") or "",
        "status": row.get("status") or "",
        "review_note": row.get("review_note") or "",
        "operator_id": row.get("operator_id") or "",
        "handled_at": str(row.get("handled_at") or ""),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def list_report_reasons() -> dict[str, Any]:
    return {
        "ok": True,
        "items": [{"code": code, "label": label} for code, label in REPORT_REASONS.items()],
    }


def create_moment_report(post_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    cleaned_post_id = _clean_post_id(post_id)
    data = _normalize_report_payload(payload)
    now = _now()
    init_miniprogram_moment_report_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id,user_id,title,content
                FROM {MOMENT_TABLE_NAME}
                WHERE id=%s AND status='active'
                LIMIT 1
                """,
                (cleaned_post_id,),
            )
            post = cursor.fetchone()
            if not post:
                raise LookupError("瞬间不存在")
            reported_user_id = _clean(post.get("user_id"), 128)
            if reported_user_id == data["reporter_user_id"]:
                raise ValueError("不能举报自己的瞬间")
            report_id = uuid.uuid4().hex
            cursor.execute(
                f"""
                INSERT INTO {REPORT_TABLE_NAME} (
                    id,post_id,reporter_user_id,reported_user_id,reason_code,reason_text,detail,evidence_json,
                    post_title,post_content_preview,status,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)
                ON DUPLICATE KEY UPDATE
                    reason_code=VALUES(reason_code),
                    reason_text=VALUES(reason_text),
                    detail=VALUES(detail),
                    evidence_json=VALUES(evidence_json),
                    post_title=VALUES(post_title),
                    post_content_preview=VALUES(post_content_preview),
                    status='pending',
                    review_note=NULL,
                    operator_id=NULL,
                    handled_at=NULL,
                    updated_at=VALUES(updated_at)
                """,
                (
                    report_id,
                    cleaned_post_id,
                    data["reporter_user_id"],
                    reported_user_id,
                    data["reason_code"],
                    data["reason_text"],
                    data["detail"] or None,
                    _json_dumps(data["evidence"]),
                    _clean(post.get("title"), 80) or None,
                    _clean(post.get("content"), 255) or None,
                    now,
                    now,
                ),
            )
            cursor.execute(
                f"""
                SELECT *
                FROM {REPORT_TABLE_NAME}
                WHERE post_id=%s AND reporter_user_id=%s
                LIMIT 1
                """,
                (cleaned_post_id, data["reporter_user_id"]),
            )
            report = _serialize(cursor.fetchone() or {})
        conn.commit()
    return {"ok": True, "item": report}


def list_moment_reports(*, status: Any = "", post_id: Any = "", limit: Any = 50) -> dict[str, Any]:
    try:
        cleaned_limit = max(1, min(int(limit or 50), MAX_LIST_LIMIT))
    except (TypeError, ValueError):
        cleaned_limit = 50
    filters = ["1=1"]
    params: list[Any] = []
    cleaned_status = _clean(status, 16)
    if cleaned_status:
        if cleaned_status not in REPORT_STATUSES:
            raise ValueError("status 不支持")
        filters.append("status=%s")
        params.append(cleaned_status)
    cleaned_post_id = _clean(post_id, 32)
    if cleaned_post_id:
        filters.append("post_id=%s")
        params.append(cleaned_post_id)
    params.append(cleaned_limit)
    init_miniprogram_moment_report_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM {REPORT_TABLE_NAME}
                WHERE {' AND '.join(filters)}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = list(cursor.fetchall() or [])
    return {"ok": True, "count": len(rows), "items": [_serialize(row) for row in rows]}


def review_moment_report(report_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    cleaned_report_id = _clean(report_id, 32)
    if not cleaned_report_id:
        raise ValueError("report_id 不能为空")
    data = _normalize_review_payload(payload)
    now = _now()
    next_status = {
        "processing": "processing",
        "resolve": "resolved",
        "reject": "rejected",
        "hide_post": "resolved",
    }[data["action"]]
    init_miniprogram_moment_report_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {REPORT_TABLE_NAME} WHERE id=%s LIMIT 1", (cleaned_report_id,))
            report = cursor.fetchone()
            if not report:
                raise LookupError("举报记录不存在")
            handled_at = now if next_status in {"resolved", "rejected"} else None
            cursor.execute(
                f"""
                UPDATE {REPORT_TABLE_NAME}
                SET status=%s, review_note=%s, operator_id=%s, handled_at=%s, updated_at=%s
                WHERE id=%s
                """,
                (
                    next_status,
                    data["review_note"] or None,
                    data["operator_id"] or None,
                    handled_at,
                    now,
                    cleaned_report_id,
                ),
            )
            if data["action"] == "hide_post":
                cursor.execute(
                    f"UPDATE {MOMENT_TABLE_NAME} SET status='hidden', updated_at=%s WHERE id=%s AND status='active'",
                    (now, report["post_id"]),
                )
            cursor.execute(f"SELECT * FROM {REPORT_TABLE_NAME} WHERE id=%s LIMIT 1", (cleaned_report_id,))
            updated = _serialize(cursor.fetchone() or {})
        conn.commit()
    return {"ok": True, "item": updated}
