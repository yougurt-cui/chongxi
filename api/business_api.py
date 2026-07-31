"""Business analysis API entrypoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from services.business_analysis_service import (
    get_business_product_options,
    get_business_product_positioning,
    get_business_summary,
)


business_api = Blueprint("business_api", __name__, url_prefix="/api/business")


@business_api.get("/summary")
def business_summary():
    try:
        return jsonify(get_business_summary()), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@business_api.get("/product-options")
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
def business_product_positioning():
    payload = request.get_json(silent=True) or {}
    try:
        result = get_business_product_positioning(payload)
        return jsonify(result), 200 if result.get("ok") else 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
