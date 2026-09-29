"""Database sync API -- incremental sync from local MySQL to remote server."""

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.db_sync_service import SYNC_TABLES, sync_tables

db_sync_api = Blueprint("db_sync_api", __name__, url_prefix="/api/sync")
RAW_COMMENT_TABLES = ("douyin_raw_comments", "xiaohongshu_raw_comments")


def _normalize_raw_comment_tables(value):
    """Only allow the two raw comment sources through this public endpoint."""
    if value in (None, []):
        return list(RAW_COMMENT_TABLES)
    if not isinstance(value, list):
        raise ValueError("tables 必须是数组")
    tables = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if not tables:
        return list(RAW_COMMENT_TABLES)
    unsupported = [table for table in tables if table not in RAW_COMMENT_TABLES]
    if unsupported:
        raise ValueError(
            "该接口只允许同步原始评论表: " + ", ".join(RAW_COMMENT_TABLES)
        )
    return tables


@db_sync_api.post("/db")
@admin_required
def sync_db():
    """Incrementally sync raw Douyin/Xiaohongshu comments to the server.

    POST /api/sync/db
    Body (optional):
        { "tables": ["douyin_raw_comments"], "dry_run": false }

    Sync strategy:
        Append-only key-based sync. The service checks remote rows by each
        table's business key and inserts only keys missing from the remote DB.
        This endpoint never runs extraction and never creates brand-health tables.
    """
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))

    try:
        tables = _normalize_raw_comment_tables(payload.get("tables"))
        result = sync_tables(tables=tables, dry_run=dry_run)
        status_code = 200 if result.get("ok") else 500
        return jsonify(result), status_code
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@db_sync_api.get("/db/tables")
@admin_required
def list_sync_tables():
    """List raw comment tables accepted by this endpoint."""
    tables = []
    for name in RAW_COMMENT_TABLES:
        spec = SYNC_TABLES[name]
        tables.append({
            "table": name,
            "watermark_col": spec["watermark_col"],
            "key_cols": spec["key_cols"],
            "columns": spec["select_cols"],
        })
    return jsonify({"ok": True, "tables": tables}), 200
