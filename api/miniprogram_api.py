"""Endpoints consumed by the WeChat mini-program."""

from __future__ import annotations

import hashlib
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
from services.miniprogram_chat_service import (
    create_conversation as create_chat_conversation,
    delete_conversation as delete_chat_conversation,
    handle_message as handle_chat_message,
    list_conversations as list_chat_conversations,
    list_messages as list_chat_messages,
)
from services.miniprogram_food_change_service import (
    analyze_and_store,
    get_catalog_product_ingredients,
    list_catalog_products_by_brand,
    search_catalog_products,
)
from services.miniprogram_food_submission_service import (
    cancel_food_submission,
    create_food_submission,
    get_food_submission,
    get_food_submission_status,
    get_submission_image,
    list_food_submissions,
)
from services.miniprogram_diet_history_service import list_diet_history, add_diet_history, delete_diet_history
from services import miniprogram_reminder_service as reminders
from services import miniprogram_daily_record_service as daily_records
from services.soft_stool_assistant_service import (
    find_products as find_soft_stool_products,
    handle_turn as handle_soft_stool_turn,
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
    record_moment_view,
    set_moment_like,
)
from services.miniprogram_moment_image_service import (
    get_moment_image,
    upload_moment_image,
)
from services.miniprogram_pet_image_service import get_pet_image, upload_and_recognize_pet
from services.miniprogram_content_review_service import handle_wechat_safety_callback
from services.miniprogram_moment_report_service import (
    create_moment_report,
    list_moment_reports,
    list_report_reasons,
    review_moment_report,
)

from services.miniprogram_idea_service import (
    create_idea,
    get_category_presets,
    get_idea,
    get_idea_support_count,
    init_idea_tables,
    list_ideas,
    list_ideas_admin,
    set_idea_status,
    support_idea,
    update_idea,
)
from services.miniprogram_activity_service import (
    PostVerificationError,
    claim_activity_code,
    get_activity_stats,
    get_my_activity_code,
    import_activity_codes,
)


miniprogram_api = Blueprint("miniprogram_api", __name__, url_prefix="/api/miniprogram")


def _daily_record_response(action):
    try:
        return jsonify(action(_current_user_id(required=True))), 200
    except PermissionError as error:
        return _auth_error_response(error)
    except LookupError as error:
        return jsonify({'ok': False, 'error': str(error)}), 404
    except ValueError as error:
        return jsonify({'ok': False, 'error': str(error)}), 400
    except Exception:
        return jsonify({'ok': False, 'error': '日常记录保存或读取失败，请稍后重试'}), 500


@miniprogram_api.get('/cat-profiles/<pet_id>/daily-records')
def list_daily_records_endpoint(pet_id):
    return _daily_record_response(lambda user: daily_records.list_daily_records(user, pet_id))


@miniprogram_api.put('/cat-profiles/<pet_id>/daily-records/<day>')
def save_daily_record_endpoint(pet_id, day):
    return _daily_record_response(lambda user: daily_records.save_daily_record(user, pet_id, {**_json_payload(), 'day': day}))


@miniprogram_api.get('/cat-profiles/<pet_id>/health-attention')
def health_attention_endpoint(pet_id):
    def load(user):
        from services.miniprogram_cat_profile_service import get_cat_profile
        from services.pet_health_engine_service import display_attention

        get_cat_profile(user, pet_id)
        return {'ok': True, **display_attention(str(pet_id))}

    return _daily_record_response(load)


def _reminder_response(action, status=200):
    try:
        return jsonify(action(_current_user_id(required=True))), status
    except PermissionError as error:
        return _auth_error_response(error)
    except LookupError as error:
        return jsonify({'ok': False, 'error': str(error)}), 404
    except ValueError as error:
        return jsonify({'ok': False, 'error': str(error)}), 400
    except Exception:
        return jsonify({'ok': False, 'error': '提醒保存或读取失败，请稍后重试'}), 500


@miniprogram_api.get('/reminders')
def list_reminders_endpoint():
    return _reminder_response(reminders.list_reminders)


@miniprogram_api.post('/reminders')
def create_reminder_endpoint():
    return _reminder_response(lambda user: reminders.create_reminder(user, _json_payload()), 201)


@miniprogram_api.patch('/reminders/<reminder_id>')
def update_reminder_endpoint(reminder_id):
    return _reminder_response(lambda user: reminders.update_reminder(user, reminder_id, _json_payload()))


@miniprogram_api.delete('/reminders/<reminder_id>')
def delete_reminder_endpoint(reminder_id):
    return _reminder_response(lambda user: reminders.delete_reminder(user, reminder_id))


@miniprogram_api.post('/reminders/<reminder_id>/complete')
def complete_reminder_endpoint(reminder_id):
    return _reminder_response(lambda user: reminders.complete_reminder(user, reminder_id, _json_payload()))


@miniprogram_api.put('/cat-profiles/<pet_id>/birthday-reminder')
def sync_birthday_reminder_endpoint(pet_id):
    return _reminder_response(lambda user: reminders.sync_birthday_reminder(user, pet_id))


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


def _wechat_callback_token() -> str:
    return (
        os.getenv("WECHAT_MINIPROGRAM_CALLBACK_TOKEN")
        or os.getenv("MINIPROGRAM_WECHAT_CALLBACK_TOKEN")
        or os.getenv("WECHAT_CALLBACK_TOKEN")
        or ""
    ).strip()


def _verify_wechat_callback_signature() -> None:
    token = _wechat_callback_token()
    if not token:
        raise PermissionError("WECHAT_MINIPROGRAM_CALLBACK_TOKEN 未配置")
    signature = (request.args.get("signature") or "").strip()
    timestamp = (request.args.get("timestamp") or "").strip()
    nonce = (request.args.get("nonce") or "").strip()
    if not signature or not timestamp or not nonce:
        raise PermissionError("微信回调签名参数缺失")
    expected = hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode("utf-8")).hexdigest()
    if signature != expected:
        raise PermissionError("微信回调签名校验失败")


@miniprogram_api.get("/wechat-safety-callback")
def wechat_safety_callback_verify():
    try:
        _verify_wechat_callback_signature()
        return request.args.get("echostr") or "ok", 200
    except PermissionError as exc:
        return str(exc), 403


@miniprogram_api.post("/wechat-safety-callback")
def wechat_safety_callback_endpoint():
    try:
        _verify_wechat_callback_signature()
        result = handle_wechat_safety_callback(request.get_data() or b"", request.content_type or "")
        return jsonify(result), 200
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
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


@miniprogram_api.route("/cat-profiles/<profile_id>/diet-history", methods=["GET", "POST"])
def diet_history_endpoint(profile_id):
    try:
        user_id = _current_user_id(required=True)
        if request.method == "POST":
            return jsonify(add_diet_history(user_id, profile_id, _json_payload())), 201
        return jsonify(list_diet_history(user_id, profile_id, limit=request.args.get("limit", 50))), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "暂时无法读取或保存吃粮记录，请稍后重试"}), 500


@miniprogram_api.delete("/cat-profiles/<profile_id>/diet-history/<record_id>")
def delete_diet_history_endpoint(profile_id, record_id):
    try:
        return jsonify(delete_diet_history(_current_user_id(required=True), profile_id, record_id)), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "暂时无法删除吃粮记录，请稍后重试"}), 500


@miniprogram_api.post("/chat/conversations")
def create_chat_conversation_endpoint():
    try:
        return jsonify(create_chat_conversation(
            _user_id_from_request(required=True), _json_payload(),
        )), 201
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/chat/conversations")
def list_chat_conversations_endpoint():
    try:
        return jsonify(list_chat_conversations(
            _user_id_from_request(required=True), limit=request.args.get("limit") or 50,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/chat/conversations/<conversation_id>/messages")
def send_chat_message_endpoint(conversation_id: str):
    try:
        return jsonify(handle_chat_message(
            _user_id_from_request(required=True), conversation_id, _json_payload(),
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/chat/conversations/<conversation_id>/messages")
def list_chat_messages_endpoint(conversation_id: str):
    try:
        return jsonify(list_chat_messages(
            _user_id_from_request(required=True), conversation_id,
            limit=request.args.get("limit") or 100,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.delete("/chat/conversations/<conversation_id>")
def delete_chat_conversation_endpoint(conversation_id: str):
    try:
        return jsonify(delete_chat_conversation(
            _user_id_from_request(required=True), conversation_id,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/soft-stool-assistant/turn")
def soft_stool_assistant_turn_endpoint():
    try:
        payload = _json_payload()
        return jsonify(handle_soft_stool_turn(
            _user_id_from_request(payload, required=True), payload,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/soft-stool-assistant/products")
def soft_stool_assistant_products_endpoint():
    try:
        _user_id_from_request(required=True)
        return jsonify(find_soft_stool_products(
            request.args.get("q"), request.args.get("limit") or 15,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
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
        result = create_moment(payload)
        # 活动帖：发帖事务已提交后再分配活动码，避免"码发了但帖子没存下来"。
        # 领取失败不回滚发帖，仅在响应里带上失败原因，前端可用独立接口重试。
        activity_id = payload.get("activity_id")
        if activity_id not in (None, "", 0, "0"):
            result["activity"] = _claim_code_after_publish(result, str(payload["user_id"]), activity_id)
        return jsonify(result), 201
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
            sort=request.args.get("sort") or 'latest',
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


@miniprogram_api.post('/moments/<post_id>/view')
def record_moment_view_endpoint(post_id: str):
    try:
        payload = _json_payload()
        return jsonify(record_moment_view(post_id, user_id=_current_user_id(),
                                         visitor_id=payload.get('visitor_id'))), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception:
        return jsonify({'ok': False, 'error': '点击统计暂时不可用'}), 500


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


@miniprogram_api.post("/pet-images/recognize")
def recognize_pet_image_endpoint():
    """Store one private pet photo and return AI suggestions for profile fields."""
    try:
        image_file = request.files.get("image") or request.files.get("file")
        return jsonify(upload_and_recognize_pet(
            image_file,
            user_id=_user_id_from_request(required=True),
        )), 201
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/pet-images/<image_id>")
def get_pet_image_endpoint(image_id: str):
    """Read a private pet photo owned by the authenticated user."""
    try:
        image = get_pet_image(image_id, user_id=_user_id_from_request(required=True))
        if image.get("redirect_url"):
            with urllib.request.urlopen(image["redirect_url"], timeout=15) as response:
                image_bytes = response.read()
            return Response(
                image_bytes,
                mimetype=image.get("content_type") or "image/jpeg",
                headers={
                    "Cache-Control": "private, max-age=3600",
                    "ETag": image.get("sha256") or "",
                },
            )
        return send_file(
            image["storage_path"],
            mimetype=image.get("content_type") or "image/jpeg",
            conditional=True,
            max_age=3600,
        )
    except PermissionError as exc:
        return _auth_error_response(exc)
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
        response = jsonify(list_catalog_products_by_brand(
            request.args.get("brand", ""),
            query=request.args.get("q", ""),
            limit=int(request.args.get("limit") or 50),
        ))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response, 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/products/search")
def search_products():
    try:
        response = jsonify(search_catalog_products(
            request.args.get("q", ""),
            limit=int(request.args.get("limit") or 20),
        ))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response, 200
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


@miniprogram_api.post("/food-submissions")
def create_food_submission_endpoint():
    try:
        result = create_food_submission(
            user_id=_user_id_from_request(required=True),
            brand_name=request.form.get("brand_name") or request.form.get("brand"),
            product_name=request.form.get("product_name"),
            remark=request.form.get("remark"),
            image_files=request.files.getlist("images") or request.files.getlist("image"),
        )
        return jsonify(result), 202
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/food-submissions")
def list_food_submissions_endpoint():
    try:
        return jsonify(list_food_submissions(
            _user_id_from_request(required=True), limit=request.args.get("limit") or 50,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/food-submissions/<submission_id>")
def get_food_submission_endpoint(submission_id: str):
    try:
        return jsonify(get_food_submission(
            _user_id_from_request(required=True), submission_id,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/food-submissions/<submission_id>/status")
def get_food_submission_status_endpoint(submission_id: str):
    try:
        return jsonify(get_food_submission_status(
            _user_id_from_request(required=True), submission_id,
        )), 200, {"Cache-Control": "no-store"}
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/food-submissions/<submission_id>/result-subscription")
def subscribe_food_result(submission_id):
    from services.submission_notifications import subscribe
    try:
        return jsonify(subscribe(_user_id_from_request(required=True), submission_id,
                                 (request.get_json(silent=True) or {}).get("decision"))), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "订阅关联暂时失败，请稍后重试"}), 500


@miniprogram_api.get("/food-submissions/<submission_id>/images/<image_id>")
def get_food_submission_image_endpoint(submission_id: str, image_id: str):
    try:
        image = get_submission_image(
            _user_id_from_request(required=True), submission_id, image_id,
        )
        response = send_file(
            image["storage_path"], mimetype=image.get("content_type") or "image/jpeg",
            conditional=True, max_age=3600,
        )
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.delete("/food-submissions/<submission_id>")
def cancel_food_submission_endpoint(submission_id: str):
    try:
        return jsonify(cancel_food_submission(
            _user_id_from_request(required=True), submission_id,
        )), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ===========================================================================
# Idea crowdfunding endpoints
# ===========================================================================

@miniprogram_api.get("/ideas")
def list_ideas_endpoint():
    try:
        user_id = _current_user_id(required=False)
        limit = int(request.args.get("limit") or 50)
        items = list_ideas(user_id=user_id, limit=limit)
        return jsonify({"ok": True, "items": items, "total": len(items)}), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/ideas/<idea_id>")
def get_idea_endpoint(idea_id: str):
    try:
        user_id = _current_user_id(required=False)
        item = get_idea(idea_id, user_id=user_id)
        return jsonify({"ok": True, "item": item}), 200
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/ideas/<idea_id>/support")
def support_idea_endpoint(idea_id: str):
    try:
        user_id = _current_user_id(required=True)
        item = support_idea(idea_id, user_id)
        return jsonify({"ok": True, "item": item}), 200
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/idea-categories")
def list_idea_categories_endpoint():
    return jsonify({"ok": True, "items": get_category_presets()}), 200


# ---------------------------------------------------------------------------
# Admin idea management
# ---------------------------------------------------------------------------

@miniprogram_api.post("/admin/ideas")
def create_idea_endpoint():
    try:
        _require_admin_token()
        payload = _json_payload()
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
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.put("/admin/ideas/<idea_id>")
@miniprogram_api.patch("/admin/ideas/<idea_id>")
def update_idea_endpoint(idea_id: str):
    try:
        _require_admin_token()
        payload = _json_payload()
        item = update_idea(idea_id, **payload)
        return jsonify({"ok": True, "item": item}), 200
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.patch("/admin/ideas/<idea_id>/status")
def set_idea_status_endpoint(idea_id: str):
    try:
        _require_admin_token()
        payload = _json_payload()
        status = payload.get("status", "")
        item = set_idea_status(idea_id, status)
        return jsonify({"ok": True, "item": item}), 200
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.get("/admin/ideas")
def list_ideas_admin_endpoint():
    try:
        _require_admin_token()
        status = request.args.get("status", "")
        items = list_ideas_admin(status=status)
        return jsonify({"ok": True, "items": items, "total": len(items)}), 200
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@miniprogram_api.post("/admin/ideas/<idea_id>/cover")
def upload_idea_cover_endpoint(idea_id: str):
    try:
        _require_admin_token()
        image_file = request.files.get("file")
        if not image_file:
            raise ValueError("请上传封面图片")
        from services.miniprogram_moment_image_service import upload_moment_image
        user_id = _current_user_id(required=False) or "admin"
        result = upload_moment_image(image_file, user_id=user_id)
        cover_url = result.get("item", {}).get("url", "")
        item = update_idea(idea_id, cover_url=cover_url)
        return jsonify({"ok": True, "item": item, "cover_url": cover_url}), 200
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# User PETKIT endpoints are separate from the existing administrator collector.
@miniprogram_api.route('/device-capabilities', methods=['GET'])
@miniprogram_api.route('/device-policy', methods=['GET'])
@miniprogram_api.route('/device-connections', methods=['GET'])
@miniprogram_api.route('/device-connections/petkit', methods=['POST'])
@miniprogram_api.route('/device-connections/<ident>', methods=['DELETE'])
@miniprogram_api.route('/device-connections/<ident>/devices', methods=['GET'])
@miniprogram_api.route('/device-connections/<ident>/devices/refresh', methods=['POST'])
@miniprogram_api.route('/device-bindings', methods=['GET', 'POST'])
@miniprogram_api.route('/device-bindings/<ident>', methods=['GET', 'DELETE'])
@miniprogram_api.route('/device-bindings/<ident>/events', methods=['GET'])
@miniprogram_api.route('/device-bindings/<ident>/sync', methods=['POST'])
@miniprogram_api.route('/device-sync-jobs/<ident>', methods=['GET'])
def user_device_endpoint(ident=None):
    from services.miniprogram_device_service import handle, DeviceError
    import sqlite3
    try:
        path = request.path.split('/api/miniprogram/', 1)[-1]
        public = path in ('device-policy', 'device-capabilities')
        owner = '' if public else _current_user_id(required=True)
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict): raise DeviceError('INVALID_INPUT', '请求格式错误')
        result = handle(owner, path, request.method, data, request.args)
        response = jsonify(result)
        response.headers['Cache-Control'] = 'no-store'
        return response
    except PermissionError:
        return jsonify(ok=False, code='LOGIN_REQUIRED', error='请先登录'), 401
    except DeviceError as exc:
        return jsonify(ok=False, code=exc.code, error=exc.message), exc.status
    except LookupError:
        return jsonify(ok=False, code='NOT_FOUND', error='宠物或设备不存在'), 404
    except sqlite3.OperationalError:
        return jsonify(ok=False, code='BUSY', error='设备服务繁忙，请稍后重试'), 503
    except Exception:
        # Never interpolate exceptions: upstream payloads may contain credentials.
        return jsonify(ok=False, code='UNAVAILABLE', error='设备服务暂不可用'), 503


@miniprogram_api.post("/admin/url-links")
def generate_miniprogram_url_link_endpoint():
    """Explicit administrator action only; never called by public page loads."""
    from services.miniprogram_url_link_service import (
        LinkConfigurationError, WeChatLinkError, generate_url_link,
    )
    try:
        _require_admin_token()
        result = generate_url_link(_json_payload())
        status = 200
    except PermissionError as exc:
        result, status = {"ok": False, "error": str(exc)}, 401
    except ValueError as exc:
        result, status = {"ok": False, "error": str(exc)}, 400
    except LinkConfigurationError as exc:
        result, status = {"ok": False, "error": str(exc)}, 503
    except WeChatLinkError as exc:
        result, status = {"ok": False, "error": str(exc)}, 502
    except Exception:
        result, status = {"ok": False, "error": "小程序链接生成失败"}, 500
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response, status


# ---------------------------------------------------------------------------
# 活动码领取（发帖活动）
# ---------------------------------------------------------------------------

def _claim_code_after_publish(moment_result: dict, user_id: str, activity_id) -> dict:
    """发帖成功后的领码兜底：任何异常都只记录在响应里，不影响发帖结果。"""
    post_id = str(((moment_result or {}).get("item") or {}).get("id") or "")
    try:
        return claim_activity_code(user_id=user_id, activity_id=activity_id, post_id=post_id)
    except Exception as exc:  # 帖子已落库，这里绝不向上抛
        return {
            "ok": False,
            "status": "ERROR",
            "message": str(exc) or "活动码领取失败",
            "activity_id": activity_id,
            "code": None,
        }


@miniprogram_api.post("/activity/claim")
def claim_activity_code_endpoint():
    """发帖成功后领取活动码。请求体：{"activity_id": 1001, "post_id": "<帖子ID>"}。"""
    try:
        payload = _json_payload()
        user_id = _user_id_from_request(payload, required=True)
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    try:
        result = claim_activity_code(
            user_id=user_id,
            activity_id=payload.get("activity_id"),
            post_id=payload.get("post_id") or payload.get("moment_id"),
        )
        return jsonify(result), 200
    except PostVerificationError as exc:
        # 帖子不属于当前用户 / 已删除 / 不存在：明确区别于未登录
        return jsonify({"ok": False, "status": "FORBIDDEN", "error": str(exc)}), 403
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "活动码领取失败，请稍后重试"}), 500


@miniprogram_api.get("/activity/<int:activity_id>/my-code")
def get_my_activity_code_endpoint(activity_id: int):
    """查询当前用户在指定活动下已领取的码，用于关闭弹窗后恢复展示。"""
    try:
        user_id = _user_id_from_request(required=True)
    except PermissionError as exc:
        return _auth_error_response(exc)
    try:
        return jsonify(get_my_activity_code(user_id=user_id, activity_id=activity_id)), 200
    except PermissionError as exc:
        return _auth_error_response(exc)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "活动码查询失败，请稍后重试"}), 500


@miniprogram_api.get("/admin/activity/<int:activity_id>/stats")
def get_activity_stats_endpoint(activity_id: int):
    """运营用：查看活动码总量、已领取与剩余。"""
    try:
        _require_admin_token()
        return jsonify(get_activity_stats(activity_id)), 200
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "活动码统计失败，请稍后重试"}), 500


@miniprogram_api.post("/admin/activity/<int:activity_id>/codes")
def import_activity_codes_endpoint(activity_id: int):
    """运营用：批量补码。请求体 {"codes": ["A","B"]} 或 {"text": "每行一个码"}。"""
    try:
        _require_admin_token()
        payload = _json_payload()
        codes = payload.get("codes")
        if isinstance(codes, str):
            codes = codes.splitlines()
        elif not isinstance(codes, list):
            codes = str(payload.get("text") or "").splitlines()
        result = import_activity_codes(
            activity_id=activity_id,
            codes=codes,
            batch=str(payload.get("batch") or ""),
            dry_run=bool(payload.get("dry_run")),
        )
        return jsonify({"ok": True, **result}), 200
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "活动码导入失败，请稍后重试"}), 500
