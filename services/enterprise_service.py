"""企业试用与付费系统 — 业务逻辑层。

表结构：
  - companies              企业表
  - invitation_codes       邀请码表
  - subscription_orders    订阅订单表
  - corporate_payments     对公付款记录表
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

import pymysql
import pymysql.cursors

from app_config import get_feature_mysql_config

# ── 常量 ──────────────────────────────────────────────
TRIAL_DAYS = 7
PLAN_NAME = "宠析企业版"
PLAN_PRICE_MONTHLY = 1999.0
PLAN_PRICE_FIRST = 999.5  # 首月5折

ACCOUNT_STATUS_TRIAL_PENDING = "trial_pending"
ACCOUNT_STATUS_TRIAL_ACTIVE = "trial_active"
ACCOUNT_STATUS_TRIAL_EXPIRED = "trial_expired"
ACCOUNT_STATUS_PAID_ACTIVE = "paid_active"
ACCOUNT_STATUS_PAID_EXPIRED = "paid_expired"
ACCOUNT_STATUS_DISABLED = "disabled"

ACTIVE_STATUSES = {ACCOUNT_STATUS_TRIAL_ACTIVE, ACCOUNT_STATUS_PAID_ACTIVE}


# ── 工具函数 ──────────────────────────────────────────
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def _calendar_expiry(base: datetime | date, days: int) -> datetime:
    """Return the exclusive midnight boundary after ``days`` calendar days."""
    base_date = base.date() if isinstance(base, datetime) else base
    return datetime.combine(base_date + timedelta(days=days), time.min)


def _remaining_calendar_days(expired_at: datetime | str, now: datetime | None = None) -> int:
    """Calculate whole remaining calendar days without considering the clock time."""
    current = now or datetime.now()
    return max(0, (_as_datetime(expired_at).date() - current.date()).days)


def _is_calendar_expired(expired_at: datetime | str, now: datetime | None = None) -> bool:
    """Treat the expiry date as an exclusive natural-day boundary."""
    current = now or datetime.now()
    return current.date() >= _as_datetime(expired_at).date()


def _normalize_legacy_expiry(expired_at: datetime | str) -> datetime:
    """Normalize a legacy clock-based expiry to its natural-day boundary."""
    value = _as_datetime(expired_at)
    return datetime.combine(value.date(), time.min)


def _connect():
    cfg = get_feature_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _verify_password(password: str, hashed: str) -> bool:
    return _hash_password(password) == hashed


def _gen_invitation_code() -> str:
    return "CX" + secrets.token_hex(4).upper()


def _gen_order_no() -> str:
    return "ORD" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    result = _serialize_local_datetimes(dict(row))
    result.pop("password_hash", None)
    return result


def _rows_to_list(rows) -> list[dict]:
    return [_serialize_local_datetimes(dict(r)) for r in rows] if rows else []


def _serialize_local_datetimes(row: dict) -> dict:
    """Serialize naive MySQL DATETIME values as local wall-clock time.

    Flask otherwise emits them as GMT dates, causing browsers in China to add
    another eight hours when rendering administration tables.
    """
    for key, value in row.items():
        if isinstance(value, datetime):
            row[key] = value.isoformat(timespec="seconds")
    return row


# ── 建表 ──────────────────────────────────────────────
def ensure_tables():
    """创建所有业务表（如果不存在）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS companies (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    company_name VARCHAR(255) NOT NULL,
                    contact_name VARCHAR(128) NOT NULL,
                    contact_phone VARCHAR(32) NOT NULL,
                    contact_email VARCHAR(128) NULL,
                    password_hash VARCHAR(128) NOT NULL,
                    invitation_code VARCHAR(64) NOT NULL,
                    account_status VARCHAR(32) NOT NULL DEFAULT 'trial_pending',
                    trial_started_at DATETIME NULL,
                    trial_expired_at DATETIME NULL,
                    subscription_started_at DATETIME NULL,
                    subscription_expired_at DATETIME NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_phone (contact_phone),
                    KEY idx_status (account_status),
                    KEY idx_invitation (invitation_code)
                ) DEFAULT CHARSET=utf8mb4 COMMENT='企业表'
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS invitation_codes (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    code VARCHAR(64) NOT NULL UNIQUE,
                    status VARCHAR(16) NOT NULL DEFAULT 'unused' COMMENT 'unused/used/disabled',
                    bound_company_id BIGINT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    used_at DATETIME NULL,
                    note VARCHAR(255) NULL,
                    KEY idx_status (status)
                ) DEFAULT CHARSET=utf8mb4 COMMENT='邀请码表'
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscription_orders (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    order_no VARCHAR(64) NOT NULL UNIQUE,
                    company_id BIGINT NOT NULL,
                    plan_name VARCHAR(128) NOT NULL,
                    amount DECIMAL(10,2) NOT NULL,
                    is_first_purchase TINYINT(1) NOT NULL DEFAULT 0,
                    payment_method VARCHAR(32) NOT NULL COMMENT 'online/corporate',
                    payment_status VARCHAR(32) NOT NULL DEFAULT 'pending' COMMENT 'pending/paid/rejected/cancelled',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    paid_at DATETIME NULL,
                    KEY idx_company (company_id),
                    KEY idx_status (payment_status)
                ) DEFAULT CHARSET=utf8mb4 COMMENT='订阅订单表'
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS corporate_payments (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    order_id BIGINT NOT NULL,
                    company_id BIGINT NOT NULL,
                    payer_company VARCHAR(255) NOT NULL,
                    contact_name VARCHAR(128) NOT NULL,
                    transfer_no VARCHAR(128) NULL,
                    proof_url VARCHAR(512) NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending' COMMENT 'pending/confirmed/rejected',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    confirmed_at DATETIME NULL,
                    admin_note VARCHAR(255) NULL,
                    KEY idx_order (order_id),
                    KEY idx_company (company_id)
                ) DEFAULT CHARSET=utf8mb4 COMMENT='对公付款记录表'
            """)
            # 已有表补列（contact_email 字段是后加的）
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'companies' AND column_name = 'contact_email'"
            )
            if cur.fetchone()["n"] == 0:
                cur.execute("ALTER TABLE companies ADD COLUMN contact_email VARCHAR(128) NULL AFTER contact_phone")
        conn.commit()
    finally:
        conn.close()


# ── 邀请码 ────────────────────────────────────────────
def verify_invitation_code(code: str) -> dict[str, Any]:
    """校验邀请码是否可用。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM invitation_codes WHERE code = %s", (code.strip(),))
            row = cur.fetchone()
        if not row:
            return {"valid": False, "message": "邀请码不存在"}
        if row["status"] == "disabled":
            return {"valid": False, "message": "邀请码已被禁用"}
        if row["status"] == "used":
            return {"valid": False, "message": "邀请码已被使用"}
        return {"valid": True, "code": code.strip()}
    finally:
        conn.close()


def create_invitation_codes(count: int = 1, note: str = "") -> list[dict]:
    """批量生成邀请码。"""
    codes = []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for _ in range(count):
                code = _gen_invitation_code()
                cur.execute(
                    "INSERT INTO invitation_codes (code, status, note) VALUES (%s, 'unused', %s)",
                    (code, note or None),
                )
                codes.append({"code": code, "status": "unused"})
        conn.commit()
    finally:
        conn.close()
    return codes


def list_invitation_codes() -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ic.*, c.company_name
                FROM invitation_codes ic
                LEFT JOIN companies c ON ic.bound_company_id = c.id
                ORDER BY ic.id DESC
            """)
            return _rows_to_list(cur.fetchall())
    finally:
        conn.close()


def disable_invitation_code(code_id: int) -> dict:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE invitation_codes SET status = 'disabled' WHERE id = %s AND status = 'unused'",
                (code_id,),
            )
        conn.commit()
        affected = cur.rowcount
    finally:
        conn.close()
    if affected:
        return {"ok": True, "message": "邀请码已禁用"}
    return {"ok": False, "message": "邀请码不存在或已使用，无法禁用"}


# ── 企业注册与登录 ────────────────────────────────────
def register_enterprise(
    invitation_code: str,
    company_name: str,
    contact_name: str,
    contact_phone: str,
    password: str,
    contact_email: str = "",
) -> dict[str, Any]:
    """企业注册。"""
    # 校验邀请码
    verification = verify_invitation_code(invitation_code)
    if not verification["valid"]:
        return {"ok": False, "message": verification["message"]}

    conn = _connect()
    try:
        with conn.cursor() as cur:
            # 检查手机号唯一
            cur.execute("SELECT id FROM companies WHERE contact_phone = %s", (contact_phone,))
            if cur.fetchone():
                return {"ok": False, "message": "该手机号已注册"}

            # 创建企业
            cur.execute(
                """INSERT INTO companies (company_name, contact_name, contact_phone, contact_email, password_hash, invitation_code, account_status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'trial_pending')""",
                (company_name, contact_name, contact_phone, contact_email or None, _hash_password(password), invitation_code.strip()),
            )
            company_id = cur.lastrowid

            # 绑定邀请码
            cur.execute(
                "UPDATE invitation_codes SET status = 'used', bound_company_id = %s, used_at = %s WHERE code = %s",
                (company_id, _now(), invitation_code.strip()),
            )
        conn.commit()
        return {"ok": True, "company_id": company_id, "message": "注册成功，请等待管理员开通体验"}
    finally:
        conn.close()


def login_enterprise(contact_phone: str, password: str) -> dict[str, Any]:
    """企业登录。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies WHERE contact_phone = %s", (contact_phone,))
            company = cur.fetchone()
        if not company:
            return {"ok": False, "message": "企业不存在"}
        if not _verify_password(password, company["password_hash"]):
            return {"ok": False, "message": "密码错误"}
        if company["account_status"] == ACCOUNT_STATUS_DISABLED:
            return {"ok": False, "message": "账号已停用"}

        # 同步过期状态
        company = _sync_company_status(company)
        return {
            "ok": True,
            "company": _row_to_dict(company),
            "message": "登录成功",
        }
    finally:
        conn.close()


def _sync_company_status(company: dict) -> dict:
    """检查并更新过期状态。"""
    now = datetime.now()
    status = company["account_status"]
    changed = False
    expiry_changed = False

    expiry_column = None
    if status == ACCOUNT_STATUS_TRIAL_ACTIVE and company["trial_expired_at"]:
        expiry_column = "trial_expired_at"
    elif status == ACCOUNT_STATUS_PAID_ACTIVE and company["subscription_expired_at"]:
        expiry_column = "subscription_expired_at"
    if expiry_column:
        normalized_expiry = _normalize_legacy_expiry(company[expiry_column])
        if normalized_expiry != _as_datetime(company[expiry_column]):
            company[expiry_column] = normalized_expiry
            expiry_changed = True

    if status == ACCOUNT_STATUS_TRIAL_ACTIVE and company["trial_expired_at"]:
        if _is_calendar_expired(company["trial_expired_at"], now):
            status = ACCOUNT_STATUS_TRIAL_EXPIRED
            changed = True
    elif status == ACCOUNT_STATUS_PAID_ACTIVE and company["subscription_expired_at"]:
        if _is_calendar_expired(company["subscription_expired_at"], now):
            status = ACCOUNT_STATUS_PAID_EXPIRED
            changed = True

    if changed or expiry_changed:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                if expiry_changed and expiry_column:
                    cur.execute(
                        f"UPDATE companies SET account_status = %s, {expiry_column} = %s WHERE id = %s",
                        (status, company[expiry_column], company["id"]),
                    )
                else:
                    cur.execute("UPDATE companies SET account_status = %s WHERE id = %s", (status, company["id"]))
            conn.commit()
        finally:
            conn.close()
        company["account_status"] = status

    return company


# ── 体验权限 ──────────────────────────────────────────
def activate_trial(company_id: int) -> dict:
    """管理员开通7天体验。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
            company = cur.fetchone()
            if not company:
                return {"ok": False, "message": "企业不存在"}
            if company["account_status"] not in (ACCOUNT_STATUS_TRIAL_PENDING, ACCOUNT_STATUS_TRIAL_EXPIRED):
                return {"ok": False, "message": f"当前状态不允许开通体验：{company['account_status']}"}

            now = datetime.now()
            expired = _calendar_expiry(now, TRIAL_DAYS)
            cur.execute(
                """UPDATE companies
                   SET account_status = 'trial_active',
                       trial_started_at = %s,
                       trial_expired_at = %s
                   WHERE id = %s""",
                (now, expired, company_id),
            )
        conn.commit()
        return {
            "ok": True,
            "message": f"已开通{TRIAL_DAYS}天体验",
            "trial_started_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trial_expired_at": expired.strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        conn.close()


def extend_trial(company_id: int, days: int) -> dict:
    """延长体验。"""
    if days <= 0 or days > 30:
        return {"ok": False, "message": "延长天数须在1-30之间"}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
            company = cur.fetchone()
            if not company:
                return {"ok": False, "message": "企业不存在"}

            base = company["trial_expired_at"] or datetime.now()
            new_expired = _calendar_expiry(_as_datetime(base), days)
            cur.execute(
                "UPDATE companies SET trial_expired_at = %s, account_status = 'trial_active' WHERE id = %s",
                (new_expired, company_id),
            )
        conn.commit()
        return {"ok": True, "message": f"已延长{days}天", "trial_expired_at": new_expired.strftime("%Y-%m-%d %H:%M:%S")}
    finally:
        conn.close()


# ── 订阅状态 ──────────────────────────────────────────
def get_subscription_status(company_id: int) -> dict:
    """获取企业订阅状态。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
            company = cur.fetchone()
        if not company:
            return {"ok": False, "message": "企业不存在"}
        company = _sync_company_status(company)
        company_dict = dict(company)
        company_dict.pop("password_hash", None)
        now = datetime.now()
        remaining_days = None
        if company_dict["account_status"] == ACCOUNT_STATUS_TRIAL_ACTIVE and company_dict["trial_expired_at"]:
            remaining_days = _remaining_calendar_days(company_dict["trial_expired_at"], now)
        elif company_dict["account_status"] == ACCOUNT_STATUS_PAID_ACTIVE and company_dict["subscription_expired_at"]:
            remaining_days = _remaining_calendar_days(company_dict["subscription_expired_at"], now)
        company_dict = _serialize_local_datetimes(company_dict)
        return {
            "ok": True,
            "company": company_dict,
            "remaining_days": remaining_days,
            "can_access": company_dict["account_status"] in ACTIVE_STATUSES,
            "plan_name": PLAN_NAME,
            "plan_price_monthly": PLAN_PRICE_MONTHLY,
            "plan_price_first": PLAN_PRICE_FIRST,
        }
    finally:
        conn.close()


# ── 订单 ──────────────────────────────────────────────
def create_order(company_id: int, payment_method: str) -> dict:
    """创建订单。"""
    if payment_method not in ("online", "corporate"):
        return {"ok": False, "message": "支付方式不合法"}

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
            company = cur.fetchone()
            if not company:
                return {"ok": False, "message": "企业不存在"}

            # 判断是否首次购买
            cur.execute(
                "SELECT COUNT(*) AS n FROM subscription_orders WHERE company_id = %s AND payment_status = 'paid'",
                (company_id,),
            )
            row = cur.fetchone()
            is_first = int(row["n"]) == 0
            amount = PLAN_PRICE_FIRST if is_first else PLAN_PRICE_MONTHLY
            order_no = _gen_order_no()

            cur.execute(
                """INSERT INTO subscription_orders
                   (order_no, company_id, plan_name, amount, is_first_purchase, payment_method, payment_status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'pending')""",
                (order_no, company_id, PLAN_NAME, amount, int(is_first), payment_method),
            )
            order_id = cur.lastrowid
        conn.commit()
        return {
            "ok": True,
            "order": {
                "id": order_id,
                "order_no": order_no,
                "amount": float(amount),
                "is_first_purchase": is_first,
                "payment_method": payment_method,
                "payment_status": "pending",
                "plan_name": PLAN_NAME,
            },
        }
    finally:
        conn.close()


def get_order(order_no: str) -> dict:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.*, c.company_name, c.contact_name, c.contact_phone
                   FROM subscription_orders o
                   JOIN companies c ON o.company_id = c.id
                   WHERE o.order_no = %s""",
                (order_no,),
            )
            order = cur.fetchone()
        if not order:
            return {"ok": False, "message": "订单不存在"}
        result = dict(order)
        result = _serialize_local_datetimes(result)
        result["amount"] = float(result["amount"])
        return {"ok": True, "order": result}
    finally:
        conn.close()


def list_orders() -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.*, c.company_name, c.contact_name, c.contact_phone
                FROM subscription_orders o
                JOIN companies c ON o.company_id = c.id
                ORDER BY o.id DESC
            """)
            rows = cur.fetchall()
        result = []
        for r in rows:
            d = _serialize_local_datetimes(dict(r))
            d["amount"] = float(d["amount"])
            result.append(d)
        return result
    finally:
        conn.close()


# ── 在线支付 ──────────────────────────────────────────
def create_online_payment(order_no: str) -> dict:
    """创建在线支付（第一版模拟支付，直接返回支付链接/二维码信息）。"""
    order_info = get_order(order_no)
    if not order_info["ok"]:
        return order_info
    order = order_info["order"]
    if order["payment_status"] != "pending":
        return {"ok": False, "message": f"订单状态不允许支付：{order['payment_status']}"}
    return {
        "ok": True,
        "order_no": order_no,
        "amount": order["amount"],
        "pay_url": f"/api/payments/mock-pay?order_no={order_no}",
        "message": "请在支付页面完成支付",
    }


def payment_callback(order_no: str, transaction_id: str = "") -> dict:
    """支付回调（模拟）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM subscription_orders WHERE order_no = %s FOR UPDATE", (order_no,))
            order = cur.fetchone()
            if not order:
                return {"ok": False, "message": "订单不存在"}
            if order["payment_status"] == "paid":
                return {"ok": False, "message": "订单已支付"}
            if order["payment_status"] != "pending":
                return {"ok": False, "message": f"订单状态不允许支付：{order['payment_status']}"}

            now = datetime.now()
            expired = now + timedelta(days=30)
            cur.execute(
                "UPDATE subscription_orders SET payment_status = 'paid', paid_at = %s WHERE order_no = %s",
                (now, order_no),
            )
            cur.execute(
                """UPDATE companies
                   SET account_status = 'paid_active',
                       subscription_started_at = %s,
                       subscription_expired_at = %s
                   WHERE id = %s""",
                (now, expired, order["company_id"]),
            )
        conn.commit()
        return {
            "ok": True,
            "message": "支付成功，正式版已开通",
            "subscription_expired_at": expired.strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        conn.close()


# ── 对公付款 ──────────────────────────────────────────
# 收款信息（静态配置）
CORPORATE_BANK_INFO = {
    "收款公司": "杭州宠析科技有限公司",
    "开户银行": "招商银行杭州分行",
    "银行账号": "5719 0856 7710 808",
}


def submit_corporate_payment(
    order_no: str,
    payer_company: str,
    contact_name: str,
    transfer_no: str = "",
    proof_url: str = "",
) -> dict:
    """提交对公付款信息。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM subscription_orders WHERE order_no = %s", (order_no,))
            order = cur.fetchone()
            if not order:
                return {"ok": False, "message": "订单不存在"}
            if order["payment_method"] != "corporate":
                return {"ok": False, "message": "该订单不是对公付款订单"}
            if order["payment_status"] == "paid":
                return {"ok": False, "message": "订单已支付"}

            cur.execute(
                """INSERT INTO corporate_payments
                   (order_id, company_id, payer_company, contact_name, transfer_no, proof_url, status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'pending')""",
                (order["id"], order["company_id"], payer_company, contact_name, transfer_no or None, proof_url or None),
            )
        conn.commit()
        return {"ok": True, "message": "对公付款信息已提交，请等待管理员确认"}
    finally:
        conn.close()


def confirm_corporate_payment(payment_id: int, admin_note: str = "") -> dict:
    """管理员确认对公到账。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM corporate_payments WHERE id = %s", (payment_id,))
            payment = cur.fetchone()
            if not payment:
                return {"ok": False, "message": "对公记录不存在"}
            if payment["status"] != "pending":
                return {"ok": False, "message": f"当前状态不允许确认：{payment['status']}"}

            now = datetime.now()
            expired = now + timedelta(days=30)
            cur.execute(
                "UPDATE corporate_payments SET status = 'confirmed', confirmed_at = %s, admin_note = %s WHERE id = %s",
                (now, admin_note, payment_id),
            )
            cur.execute(
                "UPDATE subscription_orders SET payment_status = 'paid', paid_at = %s WHERE id = %s",
                (now, payment["order_id"]),
            )
            cur.execute(
                """UPDATE companies
                   SET account_status = 'paid_active',
                       subscription_started_at = %s,
                       subscription_expired_at = %s
                   WHERE id = %s""",
                (now, expired, payment["company_id"]),
            )
        conn.commit()
        return {"ok": True, "message": "已确认到账，正式版已开通"}
    finally:
        conn.close()


def reject_corporate_payment(payment_id: int, admin_note: str = "") -> dict:
    """管理员驳回对公记录。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE corporate_payments SET status = 'rejected', confirmed_at = %s, admin_note = %s WHERE id = %s AND status = 'pending'",
                (_now(), admin_note, payment_id),
            )
        conn.commit()
        if cur.rowcount:
            return {"ok": True, "message": "已驳回"}
        return {"ok": False, "message": "记录不存在或状态不允许驳回"}
    finally:
        conn.close()


def list_corporate_payments() -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cp.*, so.order_no, so.amount, c.company_name
                FROM corporate_payments cp
                JOIN subscription_orders so ON cp.order_id = so.id
                JOIN companies c ON cp.company_id = c.id
                ORDER BY cp.id DESC
            """)
            rows = cur.fetchall()
        result = []
        for r in rows:
            d = _serialize_local_datetimes(dict(r))
            d["amount"] = float(d["amount"])
            result.append(d)
        return result
    finally:
        conn.close()


# ── 企业管理 ──────────────────────────────────────────
def list_companies() -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies ORDER BY id DESC")
            rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # 同步状态
            d = _sync_company_status(d)
            # 格式化时间
            for k in ("trial_started_at", "trial_expired_at", "subscription_started_at",
                      "subscription_expired_at", "created_at", "updated_at"):
                if d.get(k) and isinstance(d[k], datetime):
                    d[k] = d[k].strftime("%Y-%m-%d %H:%M:%S")
            result.append(d)
        return result
    finally:
        conn.close()


def disable_company(company_id: int) -> dict:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE companies SET account_status = 'disabled' WHERE id = %s", (company_id,))
        conn.commit()
        if cur.rowcount:
            return {"ok": True, "message": "账号已停用"}
        return {"ok": False, "message": "企业不存在"}
    finally:
        conn.close()


def manual_activate_paid(company_id: int) -> dict:
    """管理员手动开通正式版。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            now = datetime.now()
            expired = now + timedelta(days=30)
            cur.execute(
                """UPDATE companies
                   SET account_status = 'paid_active',
                       subscription_started_at = %s,
                       subscription_expired_at = %s
                   WHERE id = %s""",
                (now, expired, company_id),
            )
        conn.commit()
        if cur.rowcount:
            return {"ok": True, "message": "已手动开通正式版", "subscription_expired_at": expired.strftime("%Y-%m-%d %H:%M:%S")}
        return {"ok": False, "message": "企业不存在"}
    finally:
        conn.close()
