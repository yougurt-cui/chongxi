"""Admin content review helpers for mini-program moments."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import pymysql

from app_config import get_mysql_config, get_wechat_miniprogram_config
from services.miniprogram_auth_service import USER_TABLE, init_miniprogram_auth_tables
from services.miniprogram_moment_service import (
    COMMENT_TABLE_NAME,
    TABLE_NAME as MOMENT_TABLE_NAME,
    init_miniprogram_moment_tables,
)


MAX_LIST_LIMIT = 200
CONTENT_TYPES = {"all", "post", "comment"}
CONTENT_ACTIONS = {"check", "hide", "delete"}
_ACCESS_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}
DEFAULT_PUBLIC_BASE_URL = "https://chongxi.cloud"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect_app(autocommit: bool = False):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _clean_limit(value: Any) -> int:
    try:
        return max(1, min(int(value or 50), MAX_LIST_LIMIT))
    except (TypeError, ValueError):
        return 50


def _json_loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        return default


def _public_base_url() -> str:
    return (
        os.getenv("MINIPROGRAM_PUBLIC_BASE_URL")
        or os.getenv("APP_PUBLIC_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or DEFAULT_PUBLIC_BASE_URL
    ).strip().rstrip("/")


def _absolute_media_url(url: Any) -> str:
    value = _clean(url, 2048)
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/"):
        return f"{_public_base_url()}{value}"
    return value


def _wechat_access_token() -> str:
    cached_token = str(_ACCESS_TOKEN_CACHE.get("token") or "")
    if cached_token and float(_ACCESS_TOKEN_CACHE.get("expires_at") or 0) > time.time() + 60:
        return cached_token

    cfg = get_wechat_miniprogram_config()
    if not cfg["appid"]:
        raise ValueError("WECHAT_MINIPROGRAM_APPID 未配置")
    if not cfg["appsecret"]:
        raise ValueError("WECHAT_MINIPROGRAM_APP_SECRET 未配置")

    query = urllib.parse.urlencode({
        "grant_type": "client_credential",
        "appid": cfg["appid"],
        "secret": cfg["appsecret"],
    })
    url = f"https://api.weixin.qq.com/cgi-bin/token?{query}"
    with urllib.request.urlopen(url, timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("errcode"):
        raise ValueError(f"微信 access_token 获取失败：{data.get('errmsg') or data.get('errcode')}")
    token = _clean(data.get("access_token"), 2048)
    if not token:
        raise ValueError("微信 access_token 获取失败：未返回 access_token")
    expires_in = int(data.get("expires_in") or 7200)
    _ACCESS_TOKEN_CACHE.update({"token": token, "expires_at": time.time() + max(60, expires_in - 120)})
    return token


def check_text_with_wechat(*, content: Any, openid: Any, scene: Any = 2) -> dict[str, Any]:
    text = _clean(content, 2500)
    cleaned_openid = _clean(openid, 128)
    if not text:
        raise ValueError("审核内容不能为空")
    if not cleaned_openid:
        raise ValueError("内容作者缺少微信 openid，无法调用微信安全接口")

    token = _wechat_access_token()
    url = f"https://api.weixin.qq.com/wxa/msg_sec_check?access_token={urllib.parse.quote(token)}"
    body = json.dumps({
        "content": text,
        "version": 2,
        "scene": int(scene or 2),
        "openid": cleaned_openid,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))

    errcode = int(data.get("errcode") or 0)
    if errcode not in {0, 87014}:
        raise ValueError(f"微信内容安全接口失败：{data.get('errmsg') or errcode}")
    result = data.get("result") or {}
    suggest = result.get("suggest") or ("risky" if errcode == 87014 else "")
    label = result.get("label") or ""
    return {
        "ok": True,
        "passed": suggest in {"", "pass"},
        "suggest": suggest or "pass",
        "label": label,
        "trace_id": data.get("trace_id") or "",
        "raw": data,
    }


def submit_image_with_wechat(*, media_url: Any, openid: Any, scene: Any = 2) -> dict[str, Any]:
    absolute_url = _absolute_media_url(media_url)
    cleaned_openid = _clean(openid, 128)
    if not absolute_url:
        raise ValueError("图片 URL 不能为空")
    if not absolute_url.startswith(("http://", "https://")):
        raise ValueError("图片安全审核需要公网可访问的 HTTP(S) URL")
    if not cleaned_openid:
        raise ValueError("内容作者缺少微信 openid，无法调用微信图片安全接口")

    token = _wechat_access_token()
    url = f"https://api.weixin.qq.com/wxa/media_check_async?access_token={urllib.parse.quote(token)}"
    body = json.dumps({
        "media_url": absolute_url,
        "media_type": 2,
        "version": 2,
        "scene": int(scene or 2),
        "openid": cleaned_openid,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))

    errcode = int(data.get("errcode") or 0)
    if errcode != 0:
        raise ValueError(f"微信图片安全接口失败：{data.get('errmsg') or errcode}")
    return {
        "ok": True,
        "passed": None,
        "suggest": "async_pending",
        "label": "",
        "trace_id": data.get("trace_id") or "",
        "media_url": absolute_url,
        "raw": data,
    }


def get_user_openid(user_id: Any) -> str:
    cleaned_user_id = _clean(user_id, 128)
    if not cleaned_user_id:
        return ""
    init_miniprogram_auth_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT openid FROM {USER_TABLE} WHERE id=%s AND status='active' LIMIT 1", (cleaned_user_id,))
            row = cursor.fetchone() or {}
    return _clean(row.get("openid"), 128)


def _image_urls(images: Any) -> list[str]:
    if not isinstance(images, list):
        return []
    urls: list[str] = []
    for item in images:
        if isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            url = item.get("url") or item.get("src") or item.get("path")
        else:
            url = ""
        absolute_url = _absolute_media_url(url)
        if absolute_url:
            urls.append(absolute_url)
    return urls


def check_moment_payload_with_wechat(data: dict[str, Any], *, openid: Any = "", scene: Any = 2) -> dict[str, Any]:
    cleaned_openid = _clean(openid, 128) or get_user_openid((data or {}).get("user_id"))
    text_content = "\n".join([
        part for part in [
            _clean((data or {}).get("title")),
            _clean((data or {}).get("content")),
        ] if part
    ])
    text_result = check_text_with_wechat(content=text_content, openid=cleaned_openid, scene=scene)
    if not text_result["passed"]:
        raise ValueError("内容包含微信安全接口判定的风险文本，请修改后再发布")
    image_results = [
        submit_image_with_wechat(media_url=url, openid=cleaned_openid, scene=scene)
        for url in _image_urls((data or {}).get("images"))
    ]
    return {
        "ok": True,
        "text": text_result,
        "images": image_results,
        "image_count": len(image_results),
    }


def check_comment_payload_with_wechat(data: dict[str, Any], *, openid: Any = "", scene: Any = 2) -> dict[str, Any]:
    cleaned_openid = _clean(openid, 128) or get_user_openid((data or {}).get("user_id"))
    text_result = check_text_with_wechat(content=(data or {}).get("content"), openid=cleaned_openid, scene=scene)
    if not text_result["passed"]:
        raise ValueError("评论包含微信安全接口判定的风险文本，请修改后再发布")
    return {"ok": True, "text": text_result, "images": [], "image_count": 0}


def _serialize_post(row: dict[str, Any]) -> dict[str, Any]:
    images = _json_loads(row.get("images_json"), [])
    content = "\n".join([part for part in [_clean(row.get("title")), _clean(row.get("content"))] if part])
    return {
        "id": row.get("id"),
        "type": "post",
        "type_label": "瞬间",
        "post_id": row.get("id"),
        "user_id": row.get("user_id") or "",
        "openid": row.get("openid") or "",
        "title": row.get("title") or "",
        "content": content,
        "images": images if isinstance(images, list) else [],
        "image_count": len(images) if isinstance(images, list) else 0,
        "status": row.get("status") or "",
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _serialize_comment(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "type": "comment",
        "type_label": "评论",
        "post_id": row.get("post_id") or "",
        "user_id": row.get("user_id") or "",
        "openid": row.get("openid") or "",
        "title": row.get("post_title") or "",
        "content": row.get("content") or "",
        "image_count": 0,
        "status": row.get("status") or "",
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def list_content_reviews(*, content_type: Any = "all", status: Any = "active", limit: Any = 50) -> dict[str, Any]:
    cleaned_type = _clean(content_type, 16) or "all"
    if cleaned_type not in CONTENT_TYPES:
        raise ValueError("type 仅支持 all/post/comment")
    cleaned_status = _clean(status, 16) or "active"
    cleaned_limit = _clean_limit(limit)
    init_miniprogram_auth_tables()
    init_miniprogram_moment_tables()

    items: list[dict[str, Any]] = []
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            if cleaned_type in {"all", "post"}:
                cursor.execute(
                    f"""
                    SELECT p.*, u.openid
                    FROM {MOMENT_TABLE_NAME} p
                    LEFT JOIN {USER_TABLE} u ON u.id=p.user_id
                    WHERE p.status=%s
                    ORDER BY p.updated_at DESC
                    LIMIT %s
                    """,
                    (cleaned_status, cleaned_limit),
                )
                items.extend(_serialize_post(row) for row in list(cursor.fetchall() or []))
            if cleaned_type in {"all", "comment"}:
                cursor.execute(
                    f"""
                    SELECT c.*, p.title AS post_title, u.openid
                    FROM {COMMENT_TABLE_NAME} c
                    LEFT JOIN {MOMENT_TABLE_NAME} p ON p.id=c.post_id
                    LEFT JOIN {USER_TABLE} u ON u.id=c.user_id
                    WHERE c.status=%s
                    ORDER BY c.updated_at DESC
                    LIMIT %s
                    """,
                    (cleaned_status, cleaned_limit),
                )
                items.extend(_serialize_comment(row) for row in list(cursor.fetchall() or []))
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return {"ok": True, "count": len(items[:cleaned_limit]), "items": items[:cleaned_limit]}


def _load_content(cursor, content_type: str, content_id: str) -> dict[str, Any]:
    if content_type == "post":
        cursor.execute(
            f"""
            SELECT p.*, u.openid
            FROM {MOMENT_TABLE_NAME} p
            LEFT JOIN {USER_TABLE} u ON u.id=p.user_id
            WHERE p.id=%s
            LIMIT 1
            """,
            (content_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise LookupError("内容不存在")
        return _serialize_post(row)
    if content_type == "comment":
        cursor.execute(
            f"""
            SELECT c.*, p.title AS post_title, u.openid
            FROM {COMMENT_TABLE_NAME} c
            LEFT JOIN {MOMENT_TABLE_NAME} p ON p.id=c.post_id
            LEFT JOIN {USER_TABLE} u ON u.id=c.user_id
            WHERE c.id=%s
            LIMIT 1
            """,
            (content_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise LookupError("内容不存在")
        return _serialize_comment(row)
    raise ValueError("type 仅支持 post/comment")


def review_content(content_type: Any, content_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    cleaned_type = _clean(content_type, 16)
    cleaned_id = _clean(content_id, 32)
    action = _clean((payload or {}).get("action") or "check", 16)
    if cleaned_type not in {"post", "comment"}:
        raise ValueError("type 仅支持 post/comment")
    if not cleaned_id:
        raise ValueError("content_id 不能为空")
    if action not in CONTENT_ACTIONS:
        raise ValueError("action 仅支持 check/hide/delete")

    init_miniprogram_auth_tables()
    init_miniprogram_moment_tables()
    now = _now()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            item = _load_content(cursor, cleaned_type, cleaned_id)
            safety_result = None
            if action == "check":
                if cleaned_type == "post":
                    safety_result = check_moment_payload_with_wechat(item, openid=item["openid"], scene=(payload or {}).get("scene") or 2)
                else:
                    safety_result = check_comment_payload_with_wechat(item, openid=item["openid"], scene=(payload or {}).get("scene") or 2)
            elif cleaned_type == "post":
                next_status = "hidden" if action == "hide" else "deleted"
                cursor.execute(
                    f"UPDATE {MOMENT_TABLE_NAME} SET status=%s, updated_at=%s WHERE id=%s",
                    (next_status, now, cleaned_id),
                )
            else:
                cursor.execute(
                    f"UPDATE {COMMENT_TABLE_NAME} SET status='deleted', updated_at=%s WHERE id=%s",
                    (now, cleaned_id),
                )
            updated = _load_content(cursor, cleaned_type, cleaned_id)
        conn.commit()

    return {"ok": True, "item": updated, "safety": safety_result}
