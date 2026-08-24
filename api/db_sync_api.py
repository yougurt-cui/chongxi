"""Database sync API -- incremental sync from local MySQL to remote server."""

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.db_sync_service import SYNC_TABLES, sync_tables

db_sync_api = Blueprint("db_sync_api", __name__, url_prefix="/api/sync")


@db_sync_api.post("/db")
@admin_required
def sync_db():
    """Trigger incremental sync to remote server.

    POST /api/sync/db
    Body (optional):
        { "tables": ["douyin_raw_comments"], "dry_run": false }

    Sync strategy:
        Append-only key-based sync. The service checks remote rows by each
        table's business key and inserts only keys missing from the remote DB.
    """
    payload = request.get_json(silent=True) or {}
    tables = payload.get("tables")
    dry_run = bool(payload.get("dry_run", False))

    try:
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
    """List available tables for sync."""
    tables = []
    for name, spec in SYNC_TABLES.items():
        tables.append({
            "table": name,
            "watermark_col": spec["watermark_col"],
            "key_cols": spec["key_cols"],
            "columns": spec["select_cols"],
        })
    return jsonify({"ok": True, "tables": tables}), 200
