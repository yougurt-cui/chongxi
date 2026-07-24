"""Endpoints consumed by the WeChat mini-program."""

from __future__ import annotations

import urllib.request

from flask import Blueprint, Response, jsonify, request, send_file

from services.miniprogram_auth_service import wechat_login
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


miniprogram_api = Blueprint("miniprogram_api", __name__, url_prefix="/api/miniprogram")


def _json_payload() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return payload


def _user_id_from_request(payload: dict | None = None) -> str:
    payload = payload or {}
    return (
        payload.get("user_id")
        or request.args.get("user_id")
        or request.headers.get("X-User-Id")
        or ""
    )


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
        return jsonify(create_cat_profile(_json_payload())), 201
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/cat-profiles")
def list_cat_profiles_endpoint():
    try:
        return jsonify(list_cat_profiles(
            _user_id_from_request(),
            limit=request.args.get("limit") or 50,
        )), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/cat-profiles/<profile_id>")
def get_cat_profile_endpoint(profile_id: str):
    try:
        return jsonify({"ok": True, "item": get_cat_profile(_user_id_from_request(), profile_id)}), 200
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
        return jsonify(update_cat_profile(_user_id_from_request(payload), profile_id, payload)), 200
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
        return jsonify(delete_cat_profile(_user_id_from_request(payload), profile_id)), 200
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
        return jsonify(create_moment(_json_payload())), 201
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


@miniprogram_api.post("/moments/<post_id>/like")
def like_moment_endpoint(post_id: str):
    try:
        return jsonify(set_moment_like(post_id, _json_payload(), liked=True)), 200
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
        return jsonify(set_moment_like(post_id, payload, liked=False)), 200
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
        return jsonify(create_moment_comment(post_id, _json_payload())), 201
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
        return jsonify(delete_moment_comment(_user_id_from_request(payload), comment_id)), 200
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
        return jsonify(delete_moment(_user_id_from_request(payload), post_id)), 200
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
        user_id = request.form.get("user_id") or request.headers.get("X-User-Id") or ""
        return jsonify(upload_moment_image(image_file, user_id=user_id)), 201
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
