"""Standalone Taobao cat-food SKU import API."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from services.taobao_sku_import_service import (
    DEFAULT_TAOBAO_SKU_DIR,
    DEFAULT_TAOBAO_SKU_HISTORY_DIR,
    clean_taobao_sku_brands,
    import_taobao_sku_items,
)


taobao_sku_api = Blueprint("taobao_sku_api", __name__, url_prefix="/api/taobao-catfood-sku")


@taobao_sku_api.post("/import")
def taobao_catfood_sku_import():
    payload = request.get_json(silent=True) or {}
    try:
        result = import_taobao_sku_items(
            data_dir=payload.get("data_dir") or payload.get("taobao_dir") or DEFAULT_TAOBAO_SKU_DIR,
            history_dir=payload.get("history_dir") or DEFAULT_TAOBAO_SKU_HISTORY_DIR,
            truncate=bool(payload.get("truncate")),
            archive=payload.get("archive", True) is not False,
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@taobao_sku_api.post("/clean-brands")
def taobao_catfood_sku_clean_brands():
    try:
        return jsonify(clean_taobao_sku_brands()), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@taobao_sku_api.post("/import-and-clean")
def taobao_catfood_sku_import_and_clean():
    payload = request.get_json(silent=True) or {}
    try:
        import_result = import_taobao_sku_items(
            data_dir=payload.get("data_dir") or payload.get("taobao_dir") or DEFAULT_TAOBAO_SKU_DIR,
            history_dir=payload.get("history_dir") or DEFAULT_TAOBAO_SKU_HISTORY_DIR,
            truncate=bool(payload.get("truncate")),
            archive=payload.get("archive", True) is not False,
        )
        clean_result = clean_taobao_sku_brands()
        return jsonify({"ok": True, "import": import_result, "clean": clean_result}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
