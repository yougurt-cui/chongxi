"""企业试用与付费系统 — API 路由层。"""

from __future__ import annotations

import hmac
import os
from functools import wraps

from flask import Blueprint, jsonify, request

from services import enterprise_service as svc

enterprise_api = Blueprint("enterprise_api", __name__, url_prefix="/api")


def admin_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        expected = (os.getenv("MINIPROGRAM_ADMIN_TOKEN") or "").strip()
        supplied = (request.headers.get("X-Admin-Token") or "").strip()
        if not expected:
            return jsonify({"ok": False, "message": "管理员令牌未配置"}), 503
        if not supplied or not hmac.compare_digest(supplied, expected):
            return jsonify({"ok": False, "message": "管理员权限不足"}), 401
        return handler(*args, **kwargs)
    return wrapped


# ── 邀请码 ────────────────────────────────────────────
@enterprise_api.post("/invitations/verify")
def verify_invitation():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "message": "请输入邀请码"}), 400
    result = svc.verify_invitation_code(code)
    return jsonify(result), 200 if result["valid"] else 400


@enterprise_api.post("/invitations/create")
@admin_required
def create_invitations():
    payload = request.get_json(silent=True) or {}
    count = int(payload.get("count") or 1)
    note = str(payload.get("note") or "").strip()
    if count < 1 or count > 100:
        return jsonify({"ok": False, "message": "生成数量须在1-100"}), 400
    codes = svc.create_invitation_codes(count, note)
    return jsonify({"ok": True, "codes": codes}), 201


@enterprise_api.get("/invitations")
@admin_required
def list_invitations():
    return jsonify({"ok": True, "codes": svc.list_invitation_codes()}), 200


@enterprise_api.post("/invitations/<int:code_id>/disable")
@admin_required
def disable_invitation(code_id: int):
    result = svc.disable_invitation_code(code_id)
    return jsonify(result), 200 if result["ok"] else 400


# ── 企业注册与登录 ────────────────────────────────────
@enterprise_api.post("/auth/register-enterprise")
def register_enterprise():
    payload = request.get_json(silent=True) or {}
    invitation_code = str(payload.get("invitation_code") or "").strip()
    company_name = str(payload.get("company_name") or "").strip()
    contact_name = str(payload.get("contact_name") or "").strip()
    contact_phone = str(payload.get("contact_phone") or "").strip()
    contact_email = str(payload.get("contact_email") or "").strip()
    password = str(payload.get("password") or "").strip()

    if not all([invitation_code, company_name, contact_name, contact_phone, password]):
        return jsonify({"ok": False, "message": "请填写所有必填字段"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "message": "密码至少6位"}), 400

    result = svc.register_enterprise(invitation_code, company_name, contact_name, contact_phone, password, contact_email)
    return jsonify(result), 201 if result["ok"] else 400


@enterprise_api.post("/auth/login")
def login_enterprise():
    payload = request.get_json(silent=True) or {}
    contact_phone = str(payload.get("contact_phone") or "").strip()
    password = str(payload.get("password") or "").strip()
    if not contact_phone or not password:
        return jsonify({"ok": False, "message": "请输入手机号和密码"}), 400
    result = svc.login_enterprise(contact_phone, password)
    return jsonify(result), 200 if result["ok"] else 400


# ── 体验权限 ──────────────────────────────────────────
@enterprise_api.post("/admin/companies/<int:company_id>/activate-trial")
@admin_required
def activate_trial(company_id: int):
    result = svc.activate_trial(company_id)
    return jsonify(result), 200 if result["ok"] else 400


@enterprise_api.post("/admin/companies/<int:company_id>/extend-trial")
@admin_required
def extend_trial(company_id: int):
    payload = request.get_json(silent=True) or {}
    days = int(payload.get("days") or 0)
    result = svc.extend_trial(company_id, days)
    return jsonify(result), 200 if result["ok"] else 400


@enterprise_api.post("/admin/companies/<int:company_id>/disable")
@admin_required
def disable_company(company_id: int):
    result = svc.disable_company(company_id)
    return jsonify(result), 200 if result["ok"] else 400


@enterprise_api.post("/admin/companies/<int:company_id>/manual-activate")
@admin_required
def manual_activate(company_id: int):
    result = svc.manual_activate_paid(company_id)
    return jsonify(result), 200 if result["ok"] else 400


# ── 订阅状态 ──────────────────────────────────────────
@enterprise_api.get("/account/subscription-status")
def subscription_status():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"ok": False, "message": "缺少 company_id"}), 400
    result = svc.get_subscription_status(company_id)
    return jsonify(result), 200 if result["ok"] else 400


# ── 订单 ──────────────────────────────────────────────
@enterprise_api.post("/orders")
def create_order():
    payload = request.get_json(silent=True) or {}
    company_id = int(payload.get("company_id") or 0)
    payment_method = str(payload.get("payment_method") or "").strip()
    if not company_id or payment_method not in ("online", "corporate"):
        return jsonify({"ok": False, "message": "请提供 company_id 和有效的 payment_method (online/corporate)"}), 400
    result = svc.create_order(company_id, payment_method)
    return jsonify(result), 201 if result["ok"] else 400


@enterprise_api.get("/orders/<order_no>")
def get_order(order_no: str):
    result = svc.get_order(order_no)
    return jsonify(result), 200 if result["ok"] else 404


@enterprise_api.get("/orders")
@admin_required
def list_orders():
    return jsonify({"ok": True, "orders": svc.list_orders()}), 200


# ── 在线支付 ──────────────────────────────────────────
@enterprise_api.post("/payments/create")
def create_payment():
    payload = request.get_json(silent=True) or {}
    order_no = str(payload.get("order_no") or "").strip()
    if not order_no:
        return jsonify({"ok": False, "message": "缺少 order_no"}), 400
    result = svc.create_online_payment(order_no)
    return jsonify(result), 200 if result["ok"] else 400


@enterprise_api.post("/payments/callback")
@admin_required
def payment_callback():
    payload = request.get_json(silent=True) or {}
    order_no = str(payload.get("order_no") or "").strip()
    transaction_id = str(payload.get("transaction_id") or "").strip()
    if not order_no:
        return jsonify({"ok": False, "message": "缺少 order_no"}), 400
    result = svc.payment_callback(order_no, transaction_id)
    return jsonify(result), 200 if result["ok"] else 400


# 模拟支付页面（GET 返回简单 HTML）
@enterprise_api.get("/payments/mock-pay")
@admin_required
def mock_pay_page():
    from flask import request as req
    order_no = req.args.get("order_no", "")
    return f"""
    <!DOCTYPE html><html><head><meta charset="UTF-8"><title>模拟支付</title>
    <style>body{{font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;}}
    button{{padding:14px 36px;font-size:16px;border:0;border-radius:8px;background:#19C37D;color:#fff;cursor:pointer;}}
    h2{{color:#171A1F;}}p{{color:#7B8088;}}</style></head>
    <body><h2>宠析 — 模拟支付</h2>
    <p>订单号：{order_no}</p>
    <p>点击下方按钮模拟支付成功</p>
    <button onclick="pay()">确认支付</button>
    <p id="result" style="margin-top:20px;font-size:14px;"></p>
    <script>
    function pay(){{
        fetch('/api/payments/callback',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{order_no:'{order_no}',transaction_id:'MOCK_'+Date.now()}})}})
        .then(r=>r.json()).then(d=>{{
            document.getElementById('result').innerText=d.message;
            if(d.ok){{setTimeout(()=>window.location.href='/enterprise-portal.html?page=pay-success',2000);}}
        }});
    }}
    </script></body></html>
    """


# ── 对公付款 ──────────────────────────────────────────
@enterprise_api.get("/corporate-payments/bank-info")
def bank_info():
    return jsonify({"ok": True, "bank_info": svc.CORPORATE_BANK_INFO}), 200


@enterprise_api.post("/corporate-payments")
def submit_corporate():
    payload = request.get_json(silent=True) or {}
    order_no = str(payload.get("order_no") or "").strip()
    payer_company = str(payload.get("payer_company") or "").strip()
    contact_name = str(payload.get("contact_name") or "").strip()
    transfer_no = str(payload.get("transfer_no") or "").strip()
    proof_url = str(payload.get("proof_url") or "").strip()
    if not order_no or not payer_company or not contact_name:
        return jsonify({"ok": False, "message": "请填写订单号、付款公司、联系人"}), 400
    result = svc.submit_corporate_payment(order_no, payer_company, contact_name, transfer_no, proof_url)
    return jsonify(result), 200 if result["ok"] else 400


@enterprise_api.post("/admin/corporate-payments/<int:payment_id>/confirm")
@admin_required
def confirm_corporate(payment_id: int):
    payload = request.get_json(silent=True) or {}
    note = str(payload.get("note") or "").strip()
    result = svc.confirm_corporate_payment(payment_id, note)
    return jsonify(result), 200 if result["ok"] else 400


@enterprise_api.post("/admin/corporate-payments/<int:payment_id>/reject")
@admin_required
def reject_corporate(payment_id: int):
    payload = request.get_json(silent=True) or {}
    note = str(payload.get("note") or "").strip()
    result = svc.reject_corporate_payment(payment_id, note)
    return jsonify(result), 200 if result["ok"] else 400


@enterprise_api.get("/admin/corporate-payments")
@admin_required
def list_corporate():
    return jsonify({"ok": True, "payments": svc.list_corporate_payments()}), 200


# ── 后台企业管理 ──────────────────────────────────────
@enterprise_api.get("/admin/companies")
@admin_required
def list_companies():
    return jsonify({"ok": True, "companies": svc.list_companies()}), 200
