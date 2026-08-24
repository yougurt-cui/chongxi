"""Comment clean & sync API entrypoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.comment_clean_sync_service import clean_and_sync_comments

comment_clean_sync_api = Blueprint(
    "comment_clean_sync_api", __name__, url_prefix="/api/comment-clean-sync",
)


@comment_clean_sync_api.post("/run")
@admin_required
def run_comment_clean_sync():
    """Run the comment cleaning + remote sync pipeline.

    POST /api/comment-clean-sync/run
    Body (optional):
        dry_run: bool       dry-run both steps (no writes, no remote inserts).
        limit: int          debug limit per source table for cleaning; 0 = all.
        skip_clean: bool    skip cleaning, only sync to remote.
        skip_sync: bool     skip sync, only run cleaning.
        timeout: int        per-step subprocess timeout in seconds.
    """
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    skip_clean = bool(payload.get("skip_clean", False))
    skip_sync = bool(payload.get("skip_sync", False))

    try:
        limit = int(payload.get("limit", 0))
    except (TypeError, ValueError):
        limit = 0

    try:
        timeout = int(payload.get("timeout", 0)) or 1800
    except (TypeError, ValueError):
        timeout = 1800

    try:
        result = clean_and_sync_comments(
            dry_run=dry_run,
            limit=limit,
            skip_clean=skip_clean,
            skip_sync=skip_sync,
            timeout=timeout,
        )
        status_code = 200 if result.get("ok") else 500
        return jsonify(result), status_code
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
