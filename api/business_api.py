"""Business analysis API entrypoints."""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, request, session

from services.business_analysis_service import (
    get_business_product_options,
    get_business_product_positioning,
    get_business_summary,
)
from services.competitor_growth_service import (
    build_competitor_breakdown,
    build_sku_risk_summary,
    build_product_portrait,
    list_competitor_sku_options,
    list_disease_target_options,
)


business_api = Blueprint("business_api", __name__, url_prefix="/api/business")


def business_access_required(handler):
    """Limit business analysis data to its two owning workbench roles."""
    @wraps(handler)
    def wrapped(*args, **kwargs):
        user = session.get("workbench_user") or {}
        if not user:
            return jsonify({"ok": False, "error": "请先登录。"}), 401
        if user.get("role") not in {"data_admin", "brand_growth"}:
            return jsonify({"ok": False, "error": "当前账号无权访问该功能。"}), 403
        return handler(*args, **kwargs)
    return wrapped


@business_api.get("/summary")
@business_access_required
def business_summary():
    try:
        return jsonify(get_business_summary()), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@business_api.get("/product-options")
@business_access_required
def business_product_options():
    try:
        return jsonify(
            get_business_product_options(
                q=request.args.get("q", "").strip(),
                brand=request.args.get("brand", "").strip(),
                origin=request.args.get("origin", "").strip(),
                price_bucket=request.args.get("price_bucket", "").strip(),
                compare_available=request.args.get("compare_available", "true"),
                limit=int(request.args.get("limit") or 120),
            )
        ), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@business_api.post("/product-positioning")
@business_access_required
def business_product_positioning():
    payload = request.get_json(silent=True) or {}
    try:
        result = get_business_product_positioning(payload)
        return jsonify(result), 200 if result.get("ok") else 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@business_api.get("/competitor-breakdown/options")
@business_access_required
def competitor_breakdown_options():
    try:
        symptom = request.args.get("symptom", "").strip()
        if symptom:
            # V2: 按病症从 disease_representative_product 取目标产品
            return jsonify(list_disease_target_options(
                symptom=symptom,
                limit=int(request.args.get("limit") or 50),
            )), 200
        else:
            # 兼容旧版：无病症时返回全量 SKU
            return jsonify(list_competitor_sku_options(
                q=request.args.get("q", ""),
                limit=int(request.args.get("limit") or 80),
            )), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@business_api.post("/competitor-breakdown/analyze")
@business_access_required
def competitor_breakdown_analyze():
    try:
        return jsonify(build_competitor_breakdown(request.get_json(silent=True) or {})), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@business_api.post("/sku-risk-summary")
@business_access_required
def sku_risk_summary():
    try:
        return jsonify(build_sku_risk_summary(request.get_json(silent=True) or {})), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@business_api.get("/product-portrait")
@business_access_required
def product_portrait():
    try:
        return jsonify(build_product_portrait(
            brand_type=request.args.get("brand_type", "").strip(),
            has_portrait=request.args.get("has_portrait", "").strip(),
        )), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
