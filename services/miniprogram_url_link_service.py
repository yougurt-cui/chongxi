"""On-demand URL Link generation; no startup work, database, or token refresh."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app_config import get_wechat_miniprogram_config


class LinkConfigurationError(RuntimeError):
    pass


class WeChatLinkError(RuntimeError):
    pass


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception:
        # Exception URLs/bodies can contain access_token or credentials.
        raise WeChatLinkError("微信接口请求失败，请检查网络后重试") from None
    if not isinstance(result, dict):
        raise WeChatLinkError("微信接口返回格式异常")
    if result.get("errcode") not in (None, 0):
        code = result.get("errcode")
        code = code if type(code) is int else "unknown"
        raise WeChatLinkError(f"微信接口调用失败，错误码：{code}")
    return result


def generate_url_link(payload: dict) -> dict:
    """Return a reusable release-version link; never auto-retry a generation POST."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    if set(payload) - {"path", "query", "expire_days"}:
        raise ValueError("仅支持 path、query、expire_days 参数")
    path, query = payload.get("path", ""), payload.get("query", "")
    days = payload.get("expire_days", 30)
    if not isinstance(path, str) or len(path) > 1024 or re.search(r"[?#\s]", path):
        raise ValueError("path 必须为不含参数或空白的小程序页面路径，留空打开首页")
    if not isinstance(query, str) or len(query) > 1024 or re.search(r"[^0-9A-Za-z!#$&'()*+,/:;=?@\-._~%]", query):
        raise ValueError("query 最长 1024 字符，中文参数请先 URL 编码")
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("expire_days 必须为 1–30 的整数")
    cfg = get_wechat_miniprogram_config()
    if not cfg.get("appid") or not cfg.get("appsecret"):
        raise LinkConfigurationError("请配置 WECHAT_MINIPROGRAM_APPID 和 WECHAT_MINIPROGRAM_APP_SECRET")
    # Normal-mode stable_token does not force renewal; do not touch the token
    # caches or legacy token endpoints used by other business services.
    token = _post("https://api.weixin.qq.com/cgi-bin/stable_token", {
        "grant_type": "client_credential", "appid": cfg["appid"],
        "secret": cfg["appsecret"], "force_refresh": False,
    }).get("access_token")
    if not isinstance(token, str) or not token:
        raise WeChatLinkError("微信未返回有效 access_token")
    requested_at = datetime.now(timezone.utc)
    result = _post("https://api.weixin.qq.com/wxa/generate_urllink?" +
                   urllib.parse.urlencode({"access_token": token}), {
        "path": path, "query": query, "expire_type": 1,
        "expire_interval": days, "env_version": "release",
    })
    link = result.get("url_link")
    try:
        parsed = urllib.parse.urlsplit(link) if isinstance(link, str) else None
        valid = parsed and parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        raise WeChatLinkError("微信未返回有效的 HTTPS URL Link")
    return {"ok": True, "url_link": link, "appid": cfg["appid"],
            "path": path, "query": query, "expire_days": days,
            "expires_at_estimate": (requested_at + timedelta(days=days)).isoformat()}
