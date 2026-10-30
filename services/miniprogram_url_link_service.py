"""Create short-lived WeChat URL Links for opening the mini program."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from app_config import get_wechat_miniprogram_config


def _wechat_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("微信接口返回格式错误")
    if value.get("errcode"):
        raise ValueError(f"微信接口失败：{value.get('errmsg') or value['errcode']}")
    return value


def _access_token() -> str:
    config = get_wechat_miniprogram_config()
    appid = str(config.get("appid") or "").strip()
    secret = str(config.get("appsecret") or "").strip()
    if not appid:
        raise ValueError("WECHAT_MINIPROGRAM_APPID 未配置")
    if not secret:
        raise ValueError("WECHAT_MINIPROGRAM_APP_SECRET 未配置")
    query = urllib.parse.urlencode({
        "grant_type": "client_credential", "appid": appid, "secret": secret,
    })
    token = str(_wechat_json(f"https://api.weixin.qq.com/cgi-bin/token?{query}").get("access_token") or "")
    if not token:
        raise ValueError("微信接口未返回 access_token")
    return token


def generate_url_link(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    path = str(payload.get("path") or "").strip().lstrip("/")
    query = str(payload.get("query") or "").strip().lstrip("?")
    try:
        expire_days = int(payload.get("expire_days", 30))
    except (TypeError, ValueError) as error:
        raise ValueError("expire_days 必须是整数") from error
    if not 1 <= expire_days <= 30:
        raise ValueError("expire_days 必须在 1 到 30 之间")
    if len(path) > 1024 or len(query) > 1024:
        raise ValueError("path 或 query 过长")

    token = _access_token()
    response = _wechat_json(
        "https://api.weixin.qq.com/wxa/generate_urllink?" + urllib.parse.urlencode({"access_token": token}),
        {
            "path": path,
            "query": query,
            "env_version": "release",
            "is_expire": True,
            "expire_type": 1,
            "expire_interval": expire_days,
        },
    )
    url_link = str(response.get("url_link") or "").strip()
    if not url_link.startswith("https://"):
        raise ValueError("微信接口未返回有效 url_link")
    return {"ok": True, "url_link": url_link, "path": path, "query": query, "expire_days": expire_days}
