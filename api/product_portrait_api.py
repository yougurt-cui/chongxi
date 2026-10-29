"""Admin API for deterministic five-dimension product portraits."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.product_portrait_summary_service import rebuild_all_product_portraits
from services.unified_product_profile_service import rebuild_unified_product_profiles


product_portrait_api = Blueprint(
    "product_portrait_api", __name__, url_prefix="/api/data-pipeline/product-portraits"
)


@product_portrait_api.post("/rebuild")
@admin_required
def rebuild_product_portraits():
    payload = request.get_json(silent=True) or {}
    for key in ("force", "dry_run"):
        if key in payload and not isinstance(payload[key], bool):
            return jsonify({"ok": False, "message": f"{key} 必须是布尔值"}), 400
    try:
        result = rebuild_all_product_portraits(
            feature_version=payload.get("feature_version") or "soft_v1",
            force=payload.get("force", False),
            dry_run=payload.get("dry_run", False),
        )
        return jsonify(result), 200
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@product_portrait_api.post("/unified/rebuild")
@admin_required
def rebuild_unified_profiles():
    payload = request.get_json(silent=True) or {}
    if "dry_run" in payload and not isinstance(payload["dry_run"], bool):
        return jsonify({"ok": False, "message": "dry_run 必须是布尔值"}), 400
    try:
        return jsonify(rebuild_unified_product_profiles(dry_run=payload.get("dry_run", False))), 200
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500
