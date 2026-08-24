"""Demand cross-analysis API entrypoints (病症声量)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.demand_cross_analysis_service import (
    OUTPUT_TABLE,
    run_demand_cross_analysis,
)

demand_cross_analysis_api = Blueprint(
    "demand_cross_analysis_api",
    __name__,
    url_prefix="/api/demand-cross-analysis",
)


@demand_cross_analysis_api.post("/run")
@admin_required
def run_demand_cross_analysis_endpoint():
    """Regenerate ``catfood_demand_cross_analysis``.

    POST /api/demand-cross-analysis/run
    Body (optional):
        dry_run: bool    dry-run mode — print metrics without writing.
        timeout: int     subprocess timeout in seconds.
    """
    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return jsonify({"ok": False, "error": "dry_run 必须是布尔值"}), 400

    try:
        timeout = int(payload.get("timeout", 0)) or 3600
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "timeout 必须是整数"}), 400
    if timeout <= 0:
        return jsonify({"ok": False, "error": "timeout 必须大于 0"}), 400

    try:
        result = run_demand_cross_analysis(dry_run=dry_run, timeout=timeout)
        status_code = 200 if result.get("ok") else 500
        return jsonify({"output_table": OUTPUT_TABLE, **result}), status_code
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
