"""Unified cat-food product catalog API."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from services.cat_food_product_catalog_service import list_product_options, rebuild_product_catalog


product_catalog_api = Blueprint("product_catalog_api", __name__, url_prefix="/api/cat-food-compare")


@product_catalog_api.get("/product-options")
def cat_food_compare_product_options():
    try:
        return jsonify(
            list_product_options(
                q=request.args.get("q", "").strip(),
                origin=request.args.get("origin", "").strip(),
                price_bucket=request.args.get("price_bucket", "").strip(),
                function_tag=request.args.get("function_tag", "").strip(),
                brand=request.args.get("brand", "").strip(),
                compare_available=request.args.get("compare_available"),
                limit=int(request.args.get("limit") or 200),
            )
        ), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_catalog_api.post("/product-options/rebuild")
def cat_food_compare_product_options_rebuild():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(
            rebuild_product_catalog(
                brand_excel_path=payload.get("brand_excel_path") or payload.get("brand_excel"),
                taobao_dir=payload.get("taobao_dir"),
                truncate=bool(payload.get("truncate")),
            )
        ), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
