"""Product function positioning API."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from services.product_function_service import (
    batch_product_function_positioning,
    get_product_function_positioning,
)


product_function_api = Blueprint("product_function_api", __name__, url_prefix="/api/product-function")


@product_function_api.get("/positioning")
def product_function_positioning_get():
    payload = {
        "source_id": request.args.get("source_id"),
        "product_key": request.args.get("product_key") or request.args.get("sku_id"),
        "brand": request.args.get("brand") or request.args.get("brand_name"),
        "product_name": request.args.get("product_name") or request.args.get("sku_name"),
        "include_raw": request.args.get("include_raw", "").strip().lower() in {"1", "true", "yes"},
    }
    try:
        result = get_product_function_positioning(payload)
        return jsonify(result), 200 if result.get("ok") else 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_function_api.post("/positioning")
def product_function_positioning_post():
    payload = request.get_json(silent=True) or {}
    try:
        result = get_product_function_positioning(payload)
        return jsonify(result), 200 if result.get("ok") else 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_function_api.post("/positioning/batch")
def product_function_positioning_batch():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(batch_product_function_positioning(payload)), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
