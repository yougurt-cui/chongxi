"""Pipeline API entrypoints."""


from flask import Blueprint, jsonify, request

from services.miniprogram_content_review_service import list_content_reviews, review_content
from services.miniprogram_moment_report_service import list_moment_reports, review_moment_report
from services.miniprogram_idea_service import (
    create_idea,
    get_category_presets,
    list_ideas_admin,
    set_idea_status,
    update_idea,
)
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


# ---------------------------------------------------------------------------
# Idea crowdfunding management (pipeline admin)
# ---------------------------------------------------------------------------

@pipeline_api.get("/pipeline/ideas")
def pipeline_list_ideas():
    try:
        status = request.args.get("status") or ""
        items = list_ideas_admin(status=status)
        return jsonify({"ok": True, "items": items, "total": len(items)}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@pipeline_api.get("/pipeline/idea-categories")
def pipeline_idea_categories():
    return jsonify({"ok": True, "items": get_category_presets()}), 200


@pipeline_api.post("/pipeline/ideas")
def pipeline_create_idea():
    try:
        payload = request.get_json(silent=True) or {}
        item = create_idea(
            title=payload.get("title", ""),
            description=payload.get("description", ""),
            category=payload.get("category", ""),
            category_color=payload.get("category_color", ""),
            cover_url=payload.get("cover_url", ""),
            target_support_count=payload.get("target_support_count", 100),
            sort_order=payload.get("sort_order", 0),
        )
        return jsonify({"ok": True, "item": item}), 201
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@pipeline_api.put("/pipeline/ideas/<idea_id>")
@pipeline_api.patch("/pipeline/ideas/<idea_id>")
def pipeline_update_idea(idea_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        item = update_idea(idea_id, **payload)
        return jsonify({"ok": True, "item": item}), 200
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@pipeline_api.patch("/pipeline/ideas/<idea_id>/status")
def pipeline_set_idea_status(idea_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        status = payload.get("status", "")
        item = set_idea_status(idea_id, status)
        return jsonify({"ok": True, "item": item}), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@pipeline_api.post("/pipeline/ideas/<idea_id>/cover")
def pipeline_upload_idea_cover(idea_id: str):
    try:
        image_file = request.files.get("file")
        if not image_file:
            raise ValueError("请上传封面图片")
        from services.miniprogram_moment_image_service import upload_moment_image
        result = upload_moment_image(image_file, user_id="pipeline-admin")
        cover_url = result.get("item", {}).get("url", "")
        item = update_idea(idea_id, cover_url=cover_url)
        return jsonify({"ok": True, "item": item, "cover_url": cover_url}), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
