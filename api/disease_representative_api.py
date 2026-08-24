"""Disease representative API entrypoints (病症代表产品与代表原料)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.disease_representative_service import (
    OUTPUT_TABLES,
    run_disease_representative,
)

disease_representative_api = Blueprint(
    "disease_representative_api",
    __name__,
    url_prefix="/api/disease-representative",
)


@disease_representative_api.post("/run")
@admin_required
def run_disease_representative_endpoint():
    """Regenerate disease representative product / ingredient tables.

    POST /api/disease-representative/run
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
        result = run_disease_representative(dry_run=dry_run, timeout=timeout)
        status_code = 200 if result.get("ok") else 500
        return jsonify({"output_tables": OUTPUT_TABLES, **result}), status_code
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
