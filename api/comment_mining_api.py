"""Comment mining API entrypoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.comment_mining_service import run_comment_mining_pipeline

comment_mining_api = Blueprint(
    "comment_mining_api", __name__, url_prefix="/api/comment-mining",
)


@comment_mining_api.post("/run")
@admin_required
def run_comment_mining():
    """Run the need → decision → switch → experience labeler pipeline.

    POST /api/comment-mining/run
    Body (optional):
        steps: list[str]   subset of step keys to run; defaults to all four.
                           Valid: need / decision / switch / experience.
        limit: int         max source rows per script; 0 = no limit.
        dry_run: bool      dry-run mode — count only, no writes.
        timeout: int       per-step subprocess timeout in seconds.
    """
    payload = request.get_json(silent=True) or {}
    steps = payload.get("steps")
    if isinstance(steps, list):
        steps = [str(s).strip() for s in steps if str(s).strip()] or None
    else:
        steps = None

    try:
        limit = int(payload.get("limit", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit 必须是整数"}), 400
    if limit < 0:
        return jsonify({"ok": False, "error": "limit 不能小于 0"}), 400

    dry_run = bool(payload.get("dry_run", False))

    try:
        timeout = int(payload.get("timeout", 0)) or 3600
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "timeout 必须是整数"}), 400
    if timeout <= 0:
        return jsonify({"ok": False, "error": "timeout 必须大于 0"}), 400

    try:
        result = run_comment_mining_pipeline(
            steps=steps,
            limit=limit,
            dry_run=dry_run,
            timeout=timeout,
        )
        status_code = 200 if result.get("ok") else 500
        return jsonify(result), status_code
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
