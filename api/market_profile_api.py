"""Admin API for full-market profile rebuild tasks."""

from __future__ import annotations

from flask import Blueprint, jsonify

from api.enterprise_api import admin_required
from services.market_profile_rebuild_service import create_rebuild_task, get_rebuild_task


market_profile_api = Blueprint("market_profile_api", __name__, url_prefix="/api/data-pipeline")


@market_profile_api.post("/formulas/<int:formula_id>/rebuild-market-profile")
@admin_required
def rebuild_market_profile(formula_id: int):
    try:
        task = create_rebuild_task(formula_id)
        return jsonify({"ok": True, "task": task}), 202
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    except RuntimeError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@market_profile_api.get("/tasks/<task_id>")
@admin_required
def market_profile_task(task_id: str):
    try:
        task = get_rebuild_task(task_id)
        if not task:
            return jsonify({"ok": False, "message": f"任务不存在: {task_id}"}), 404
        return jsonify({"ok": True, "task": task}), 200
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500
