"""Pipeline API entrypoints."""


from flask import Blueprint, jsonify, request

from services.miniprogram_content_review_service import list_content_reviews, review_content
from services.miniprogram_moment_report_service import list_moment_reports, review_moment_report
from services.pipeline_service import ingest_catfood_ingredients


pipeline_api = Blueprint("pipeline_api", __name__, url_prefix="/api")


@pipeline_api.post("/catfood/ingredients/ingest")
def catfood_ingredients_ingest():
    payload = request.get_json(silent=True) or {}
    try:
        result = ingest_catfood_ingredients(payload)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@pipeline_api.get("/pipeline/miniprogram-content")
def miniprogram_content_reviews():
    try:
        result = list_content_reviews(
            content_type=request.args.get("type") or "all",
            status=request.args.get("status") or "active",
            limit=request.args.get("limit") or 50,
        )
        return jsonify(result), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@pipeline_api.post("/pipeline/miniprogram-content/<content_type>/<content_id>/review")
def miniprogram_content_review(content_type: str, content_id: str):
    try:
        result = review_content(content_type, content_id, request.get_json(silent=True) or {})
        return jsonify(result), 200
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@pipeline_api.get("/pipeline/miniprogram-reports")
def miniprogram_report_reviews():
    try:
        result = list_moment_reports(
            status=request.args.get("status") or "pending",
            post_id=request.args.get("post_id") or "",
            limit=request.args.get("limit") or 50,
        )
        return jsonify(result), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@pipeline_api.post("/pipeline/miniprogram-reports/<report_id>/review")
def miniprogram_report_review(report_id: str):
    try:
        result = review_moment_report(report_id, request.get_json(silent=True) or {})
        return jsonify(result), 200
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
