"""WeChat mini-program authentication."""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pymysql

from app_config import get_mysql_config, get_wechat_miniprogram_config


USER_TABLE = "miniprogram_user"
SESSION_TABLE = "miniprogram_user_session"
TOKEN_TTL_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def _connect_app(autocommit: bool = False):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_token(value: Any) -> str:
    return _clean(value, 256)


def init_miniprogram_auth_tables() -> None:
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {USER_TABLE} (
                    id CHAR(32) NOT NULL,
                    openid VARCHAR(128) NOT NULL,
                    unionid VARCHAR(128) NULL,
                    nickname VARCHAR(128) NULL,
                    avatar_url VARCHAR(1024) NULL,
                    phone_number VARCHAR(32) NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    last_login_at DATETIME NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uniq_mini_user_openid (openid),
                    KEY idx_mini_user_unionid (unionid),
                    KEY idx_mini_user_status (status, updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SESSION_TABLE} (
                    id CHAR(32) NOT NULL,
                    user_id CHAR(32) NOT NULL,
                    token_hash VARCHAR(128) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uniq_mini_session_token_hash (token_hash),
                    KEY idx_mini_session_user (user_id, status, expires_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _wechat_code2session(code: str) -> dict[str, Any]:
    cfg = get_wechat_miniprogram_config()
    if not cfg["appid"]:
        raise ValueError("WECHAT_MINIPROGRAM_APPID 未配置")
    if not cfg["appsecret"]:
        raise ValueError("WECHAT_MINIPROGRAM_APP_SECRET 未配置")
    query = urllib.parse.urlencode({
        "appid": cfg["appid"],
        "secret": cfg["appsecret"],
        "js_code": code,
        "grant_type": "authorization_code",
    })
    url = f"https://api.weixin.qq.com/sns/jscode2session?{query}"
    with urllib.request.urlopen(url, timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("errcode"):
        raise ValueError(f"微信登录失败：{data.get('errmsg') or data.get('errcode')}")
    if not data.get("openid"):
        raise ValueError("微信登录失败：未返回 openid")
    return data


def _upsert_user(cursor, openid: str, unionid: str = "", *, nickname: str = "", avatar_url: str = "") -> dict[str, Any]:
    now = _now()
    cursor.execute(f"SELECT * FROM {USER_TABLE} WHERE openid=%s LIMIT 1", (openid,))
    user = cursor.fetchone()
    if user:
        cursor.execute(
            f"""
            UPDATE {USER_TABLE}
            SET unionid=COALESCE(NULLIF(%s, ''), unionid),
                nickname=COALESCE(NULLIF(%s, ''), nickname),
                avatar_url=COALESCE(NULLIF(%s, ''), avatar_url),
                last_login_at=%s,
                updated_at=%s
            WHERE id=%s
            """,
            (unionid, nickname, avatar_url, now, now, user["id"]),
        )
        cursor.execute(f"SELECT * FROM {USER_TABLE} WHERE id=%s LIMIT 1", (user["id"],))
        return cursor.fetchone()
    user_id = secrets.token_hex(16)
    cursor.execute(
        f"""
        INSERT INTO {USER_TABLE} (id,openid,unionid,nickname,avatar_url,status,created_at,updated_at,last_login_at)
        VALUES (%s,%s,%s,%s,%s,'active',%s,%s,%s)
        """,
        (user_id, openid, unionid or None, nickname or None, avatar_url or None, now, now, now),
    )
    cursor.execute(f"SELECT * FROM {USER_TABLE} WHERE id=%s LIMIT 1", (user_id,))
    return cursor.fetchone()


def _create_session(cursor, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    cursor.execute(
        f"""
        INSERT INTO {SESSION_TABLE} (id,user_id,token_hash,status,expires_at,created_at,updated_at)
        VALUES (%s,%s,%s,'active',%s,%s,%s)
        """,
        (secrets.token_hex(16), user_id, _token_hash(token), _expires_at(), now, now),
    )
    return token


def _serialize_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "openid": user.get("openid") or "",
        "unionid": user.get("unionid") or "",
        "nickName": user.get("nickname") or "猫咪用户",
        "name": user.get("nickname") or "猫咪用户",
        "avatarUrl": user.get("avatar_url") or "",
        "phoneNumber": user.get("phone_number") or "",
    }


def wechat_login(payload: dict[str, Any]) -> dict[str, Any]:
    code = _clean((payload or {}).get("code"), 128)
    if not code:
        raise ValueError("code 不能为空")
    nickname = _clean((payload or {}).get("nickName") or (payload or {}).get("nickname") or (payload or {}).get("name"), 128)
    avatar_url = _clean((payload or {}).get("avatarUrl") or (payload or {}).get("avatar_url"), 1024)
    session = _wechat_code2session(code)
    init_miniprogram_auth_tables()
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            user = _upsert_user(
                cursor,
                _clean(session.get("openid"), 128),
                _clean(session.get("unionid"), 128),
                nickname=nickname,
                avatar_url=avatar_url,
            )
            token = _create_session(cursor, user["id"])
        conn.commit()
    return {
        "ok": True,
        "token": token,
        "user": _serialize_user(user),
        "expires_in": TOKEN_TTL_DAYS * 24 * 60 * 60,
    }


def get_user_by_token(token: str) -> dict[str, Any] | None:
    cleaned_token = _clean_token(token)
    if not cleaned_token:
        return None
    init_miniprogram_auth_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT u.*
                FROM {SESSION_TABLE} s
                JOIN {USER_TABLE} u ON u.id=s.user_id
                WHERE s.token_hash=%s
                  AND s.status='active'
                  AND s.expires_at > UTC_TIMESTAMP()
                  AND u.status='active'
                LIMIT 1
                """,
                (_token_hash(cleaned_token),),
            )
            user = cursor.fetchone()
            if not user:
                return None
            cursor.execute(
                f"UPDATE {SESSION_TABLE} SET updated_at=UTC_TIMESTAMP() WHERE token_hash=%s",
                (_token_hash(cleaned_token),),
            )
            return user


def require_user_by_token(token: str) -> dict[str, Any]:
    user = get_user_by_token(token)
    if not user:
        raise PermissionError("登录已失效，请重新登录")
    return user
