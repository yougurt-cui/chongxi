"""Endpoints consumed by the WeChat mini-program."""

from __future__ import annotations

import os
import urllib.request

from flask import Blueprint, Response, jsonify, request, send_file

from services.miniprogram_auth_service import get_user_by_token, require_user_by_token, wechat_login
from services.miniprogram_cat_profile_service import (
    create_cat_profile,
    delete_cat_profile,
    get_cat_profile,
    list_cat_profiles,
    update_cat_profile,
)
from services.miniprogram_food_change_service import (
    analyze_and_store,
    get_catalog_product_ingredients,
    list_catalog_products_by_brand,
)
from services.miniprogram_moment_service import (
    create_moment_comment,
    create_moment,
    delete_moment_comment,
    delete_moment,
    get_moment,
    list_moment_comments,
    list_moment_categories,
    list_moments,
    set_moment_like,
)
from services.miniprogram_moment_image_service import (
    get_moment_image,
    upload_moment_image,
)
from services.miniprogram_content_review_service import handle_wechat_safety_callback
from services.miniprogram_moment_report_service import (
    create_moment_report,
    list_moment_reports,
    list_report_reasons,
    review_moment_report,
)


miniprogram_api = Blueprint("miniprogram_api", __name__, url_prefix="/api/miniprogram")


def _json_payload() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return payload


def _bearer_token_from_request() -> str:
    authorization = request.headers.get("Authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _current_user_id(*, required: bool = False) -> str:
    token = _bearer_token_from_request()
    if token:
        user = require_user_by_token(token) if required else get_user_by_token(token)
        return (user or {}).get("id") or ""
    if required:
        raise PermissionError("请先登录")
    return ""


def _legacy_user_id_from_request(payload: dict | None = None) -> str:
    payload = payload or {}
    return (
        payload.get("user_id")
        or request.args.get("user_id")
        or request.headers.get("X-User-Id")
        or ""
    )


def _user_id_from_request(
    payload: dict | None = None,
    *,
    required: bool = False,
    allow_legacy_user_id: bool = False,
) -> str:
    user_id = _current_user_id(required=required)
    if user_id:
        return user_id
    if allow_legacy_user_id:
        return _legacy_user_id_from_request(payload)
    return ""


def _auth_error_response(exc: Exception):
    return jsonify({"ok": False, "error": str(exc)}), 401


def _require_admin_token() -> None:
    expected = (os.getenv("MINIPROGRAM_ADMIN_TOKEN") or "").strip()
    supplied = (request.headers.get("X-Admin-Token") or "").strip()
    if not expected:
        raise PermissionError("MINIPROGRAM_ADMIN_TOKEN 未配置")
    if supplied != expected:
        raise PermissionError("管理员权限不足")


@miniprogram_api.get("/wechat-safety-callback")
def wechat_safety_callback_verify():
    return request.args.get("echostr") or "ok", 200


@miniprogram_api.post("/wechat-safety-callback")
def wechat_safety_callback_endpoint():
    try:
        result = handle_wechat_safety_callback(request.get_data() or b"", request.content_type or "")
        return jsonify(result), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/auth/wechat-login")
def wechat_login_endpoint():
    try:
        return jsonify(wechat_login(_json_payload())), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/cat-profiles")
def create_cat_profile_endpoint():
    try:
        payload = _json_payload()
        payload["user_id"] = _user_id_from_request(payload, required=True)
        return jsonify(create_cat_profile(payload)), 201
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/cat-profiles")
def list_cat_profiles_endpoint():
    try:
        return jsonify(list_cat_profiles(
            _user_id_from_request(required=True),
            limit=request.args.get("limit") or 50,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/cat-profiles/<profile_id>")
def get_cat_profile_endpoint(profile_id: str):
    try:
        return jsonify({"ok": True, "item": get_cat_profile(_user_id_from_request(required=True), profile_id)}), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.put("/cat-profiles/<profile_id>")
@miniprogram_api.patch("/cat-profiles/<profile_id>")
def update_cat_profile_endpoint(profile_id: str):
    try:
        payload = _json_payload()
        return jsonify(update_cat_profile(_user_id_from_request(payload, required=True), profile_id, payload)), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.delete("/cat-profiles/<profile_id>")
def delete_cat_profile_endpoint(profile_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        return jsonify(delete_cat_profile(_user_id_from_request(payload, required=True), profile_id)), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/moment-categories")
def list_moment_categories_endpoint():
    return jsonify(list_moment_categories()), 200


@miniprogram_api.post("/moments")
def create_moment_endpoint():
    try:
        payload = _json_payload()
        payload["user_id"] = _user_id_from_request(payload, required=True)
        return jsonify(create_moment(payload)), 201
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/moments")
def list_moments_endpoint():
    try:
        return jsonify(list_moments(
            user_id=_user_id_from_request(),
            category_code=request.args.get("category_code") or request.args.get("category") or "",
            visibility=request.args.get("visibility") or "public",
            include_private=str(request.args.get("include_private") or "").lower() in {"1", "true", "yes"},
            limit=request.args.get("limit") or 20,
        )), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/moments/<post_id>")
def get_moment_endpoint(post_id: str):
    try:
        return jsonify({"ok": True, "item": get_moment(post_id, viewer_user_id=_user_id_from_request())}), 200
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/moment-report-reasons")
def list_moment_report_reasons_endpoint():
    return jsonify(list_report_reasons()), 200


@miniprogram_api.post("/moments/<post_id>/reports")
def create_moment_report_endpoint(post_id: str):
    try:
        payload = _json_payload()
        payload["reporter_user_id"] = _user_id_from_request(payload, required=True)
        return jsonify(create_moment_report(post_id, payload)), 201
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/admin/moment-reports")
def list_moment_reports_endpoint():
    try:
        _require_admin_token()
        return jsonify(list_moment_reports(
            status=request.args.get("status") or "",
            post_id=request.args.get("post_id") or "",
            limit=request.args.get("limit") or 50,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.patch("/admin/moment-reports/<report_id>")
def review_moment_report_endpoint(report_id: str):
    try:
        _require_admin_token()
        return jsonify(review_moment_report(report_id, _json_payload())), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/moments/<post_id>/like")
def like_moment_endpoint(post_id: str):
    try:
        payload = _json_payload()
        payload["user_id"] = _user_id_from_request(payload, required=True)
        return jsonify(set_moment_like(post_id, payload, liked=True)), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.delete("/moments/<post_id>/like")
def unlike_moment_endpoint(post_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        payload["user_id"] = _user_id_from_request(payload, required=True)
        return jsonify(set_moment_like(post_id, payload, liked=False)), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/moments/<post_id>/comments")
def list_moment_comments_endpoint(post_id: str):
    try:
        return jsonify(list_moment_comments(
            post_id,
            viewer_user_id=_user_id_from_request(),
            limit=request.args.get("limit") or 50,
        )), 200
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/moments/<post_id>/comments")
def create_moment_comment_endpoint(post_id: str):
    try:
        payload = _json_payload()
        payload["user_id"] = _user_id_from_request(payload, required=True)
        return jsonify(create_moment_comment(post_id, payload)), 201
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.delete("/moment-comments/<comment_id>")
def delete_moment_comment_endpoint(comment_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        return jsonify(delete_moment_comment(_user_id_from_request(payload, required=True), comment_id)), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.delete("/moments/<post_id>")
def delete_moment_endpoint(post_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        return jsonify(delete_moment(_user_id_from_request(payload, required=True), post_id)), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/moment-images")
def upload_moment_image_endpoint():
    try:
        image_file = request.files.get("image") or request.files.get("file")
        user_id = _user_id_from_request(required=True)
        return jsonify(upload_moment_image(image_file, user_id=user_id)), 201
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/moment-images/<file_id>")
def get_moment_image_endpoint(file_id: str):
    try:
        image = get_moment_image(file_id)
        if image.get("redirect_url"):
            with urllib.request.urlopen(image["redirect_url"], timeout=15) as response:
                image_bytes = response.read()
            return Response(
                image_bytes,
                mimetype=image["content_type"] or "application/octet-stream",
                headers={"Cache-Control": "public, max-age=3600"},
            )
        return send_file(image["storage_path"], mimetype=image["content_type"], max_age=86400)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/food-change/intent")
def food_change_intent():
    try:
        payload = _json_payload()
        user_id = _user_id_from_request(payload)
        if user_id:
            payload["user_id"] = user_id
        return jsonify(analyze_and_store(payload)), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/products")
def products_by_brand():
    try:
        return jsonify(list_catalog_products_by_brand(
            request.args.get("brand", ""),
            query=request.args.get("q", ""),
            limit=int(request.args.get("limit") or 50),
        )), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/products/ingredients")
def product_ingredients():
    try:
        payload = _json_payload()
        return jsonify(get_catalog_product_ingredients(payload)), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
