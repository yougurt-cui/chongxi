"""One subscribed result notice per private submission; never re-send unknown outcomes."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta

from app_config import get_wechat_miniprogram_config
from services import orchestrator_service as jobs

_ready = False
_lock = threading.Lock()
_token_lock = threading.Lock()
_token = ("", 0.0)
TABLE = "miniprogram_submission_notice"


def config():
    template = os.getenv("WECHAT_FOOD_RESULT_TEMPLATE_ID", "").strip()
    try:
        fields = json.loads(os.getenv("WECHAT_FOOD_RESULT_TEMPLATE_FIELDS", "{}"))
    except ValueError:
        fields = {}
    patterns = {"product": r"thing\d+", "status": r"(?:thing|phrase)\d+", "completed_at": r"(?:time|date)\d+",
                "analysis_type": r"thing\d+", "result_summary": r"thing\d+"}
    valid = isinstance(fields, dict) and bool(fields) and all(
        isinstance(k, str) and isinstance(v, str) and v in patterns and re.fullmatch(patterns[v], k)
        for k, v in fields.items())
    return {"enabled": bool(template and valid), "template_id": template if valid else "",
            "fields": fields if valid else {}}


def public_config():
    cfg = config()
    return {"enabled": cfg["enabled"], "template_id": cfg["template_id"],
            "description": "首次有效分析结果生成后通知一次；拒绝订阅不影响处理"}


def init_db():
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        with jobs._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                    submission_id CHAR(32) PRIMARY KEY,user_id VARCHAR(128) NOT NULL,
                    template_id VARCHAR(128) NOT NULL,status VARCHAR(24) NOT NULL,
                    attempt_count INT NOT NULL DEFAULT 0,error_code VARCHAR(64) NULL,
                    created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,
                    KEY idx_submission_notice_status(status,updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            conn.commit()
        _ready = True


def subscribe(user_id, submission_id, decision):
    if decision not in {"accept", "reject", "ban", "filter"}:
        raise ValueError("无效订阅结果")
    cfg = config()
    if not cfg["enabled"]:
        raise ValueError("结果通知模板尚未配置，请在投稿页查看进度")
    init_db()
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM miniprogram_food_submission WHERE id=%s AND user_id=%s AND status='active' FOR UPDATE",
                        (submission_id, user_id))
            if not cur.fetchone():
                raise LookupError("投稿不存在或已撤销")
            status = "waiting" if decision == "accept" else "declined"
            cur.execute(f"""INSERT INTO {TABLE}
                (submission_id,user_id,template_id,status,created_at,updated_at)
                VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE
                  template_id=IF(status IN ('waiting','declined'),VALUES(template_id),template_id),
                  status=IF(status IN ('waiting','declined'),VALUES(status),status),updated_at=UTC_TIMESTAMP()""",
                (submission_id, user_id, cfg["template_id"], status))
        conn.commit()
    jobs.wake_dispatcher()
    return {"ok": True, "subscription": status}


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.loads(response.read().decode())


def _access_token():
    global _token
    with _token_lock:
        if _token[1] > time.monotonic():
            return _token[0]
        cfg = get_wechat_miniprogram_config()
        if not cfg.get("appid") or not cfg.get("appsecret"):
            raise ValueError("微信服务端凭据未配置")
        result = _post("https://api.weixin.qq.com/cgi-bin/stable_token",
                       {"grant_type": "client_credential", "appid": cfg["appid"], "secret": cfg["appsecret"]})
        if not result.get("access_token"):
            raise ValueError("获取微信通知凭据失败")
        _token = (result["access_token"], time.monotonic() + max(0, int(result.get("expires_in", 7200)) - 120))
        return _token[0]


def template_data(row, fields):
    completed = row.get("finished_at")
    if isinstance(completed, str):
        completed = datetime.fromisoformat(completed)
    completed = completed or datetime.now(timezone.utc)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    product = str(row.get("claimed_product_name") or "猫粮投稿")
    values = {"product": product, "status": "解析完成",
              "analysis_type": "食品配料解析", "result_summary": product[:16] + "解析完成",
              "completed_at": completed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")}
    return {key: {"value": values[value][:5] if key.startswith("phrase") else values[value][:20]
                  if key.startswith("thing") else values[value]} for key, value in fields.items()}


def reconcile():
    cfg = config()
    if not cfg["enabled"]:
        return
    init_db()
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            # A crash after platform acceptance is ambiguous: do not silently
            # issue a second message against another subscription credit.
            cur.execute(f"UPDATE {TABLE} SET status='unknown',error_code='send_interrupted' "
                        "WHERE status='sending' AND updated_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL 5 MINUTE)")
            cur.execute(f"""SELECT n.submission_id,n.template_id,s.user_id,s.claimed_product_name,
                u.openid,t.finished_at FROM {TABLE} n
                JOIN miniprogram_food_submission s ON s.id=n.submission_id AND s.user_id=n.user_id
                JOIN pipeline_task t ON t.id=s.orchestrator_task_id
                JOIN miniprogram_user u ON u.id=s.user_id
                WHERE n.status='waiting' AND s.status='active' AND u.status='active'
                  AND t.task_status='done' AND TRIM(COALESCE(s.ingredient_composition,''))<>''
                ORDER BY n.created_at LIMIT 10""")
            rows = list(cur.fetchall())
        conn.commit()
    for row in rows:
        # Acquire credentials before claiming a send: this failure cannot have
        # delivered the notice, so leave it waiting for configuration recovery.
        try:
            token = _access_token()
        except Exception:
            continue
        if row["template_id"] != cfg["template_id"]:
            continue
        with jobs._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {TABLE} n JOIN miniprogram_food_submission s ON s.id=n.submission_id "
                            "JOIN pipeline_task t ON t.id=s.orchestrator_task_id "
                            "SET n.status='sending',n.attempt_count=n.attempt_count+1,n.updated_at=UTC_TIMESTAMP() "
                            "WHERE n.submission_id=%s AND n.status='waiting' AND s.status='active' AND t.task_status='done'",
                            (row["submission_id"],))
                claimed = cur.rowcount == 1
            conn.commit()
        if not claimed:
            continue
        try:
            result = _post("https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token=" + token,
                           {"touser": row["openid"], "template_id": row["template_id"],
                            "page": "pages/pet-space/food-submissions?id=" + row["submission_id"],
                            "miniprogram_state": os.getenv("WECHAT_MINIPROGRAM_STATE", "formal"),
                            "lang": "zh_CN", "data": template_data(row, cfg["fields"])})
            code = str(result.get("errcode", "missing"))
            status = "sent" if code == "0" else "declined" if code == "43101" else "failed"
        except Exception:
            status, code = "unknown", "transport_outcome_unknown"
        with jobs._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {TABLE} SET status=%s,error_code=%s,updated_at=UTC_TIMESTAMP() "
                            "WHERE submission_id=%s AND status='sending'", (status, code, row["submission_id"]))
            conn.commit()
