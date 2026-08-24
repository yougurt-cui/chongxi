"""Formula profile build API entrypoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.formula_profile_build_service import build_formula_profile_pipeline

formula_profile_api = Blueprint("formula_profile_api", __name__, url_prefix="/api/formula-profile")


@formula_profile_api.post("/build")
@admin_required
def build_formula_profile():
    """Run the module_ranking → structure_labels → formula_profile pipeline.

    Body (optional):
        steps: list[str]  subset of step keys to run; defaults to all three.
        timeout: int      per-step subprocess timeout in seconds.
    """
    payload = request.get_json(silent=True) or {}
    steps = payload.get("steps")
    if isinstance(steps, list):
        steps = [str(s).strip() for s in steps if str(s).strip()] or None
    else:
        steps = None

    raw_timeout = payload.get("timeout")
    try:
        timeout = int(raw_timeout) if raw_timeout else 1200
    except (TypeError, ValueError):
        timeout = 1200

    try:
        result = build_formula_profile_pipeline(steps=steps, timeout=timeout)
        status_code = 200 if result.get("ok") else 500
        return jsonify(result), status_code
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
