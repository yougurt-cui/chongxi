"""Public enquiry intake and existing-admin-only retrieval."""
from flask import Blueprint, current_app, jsonify, request
from api.enterprise_api import admin_required
from services.merchant_lead_service import TooManyLeads, list_leads, save_lead

merchant_lead_api = Blueprint('merchant_lead_api', __name__, url_prefix='/api')


@merchant_lead_api.post('/merchant-leads')
def submit_merchant_lead():
    if not request.is_json:
        return jsonify(ok=False, message='请使用 JSON 提交'), 415
    # Bounded read also covers streamed requests without Content-Length.
    raw = request.stream.read(16385)
    if len(raw) > 16384:
        return jsonify(ok=False, message='提交内容过长'), 413
    try:
        payload = current_app.json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return jsonify(ok=False, message='提交格式不正确'), 400
    try:
        lead_id = save_lead(payload, request.remote_addr or '')
    except TooManyLeads as exc:
        return jsonify(ok=False, message=str(exc)), 429
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    except Exception:
        return jsonify(ok=False, message='暂时无法保存，请稍后重试'), 503
    return jsonify(ok=True, id=lead_id), 201


@merchant_lead_api.get('/admin/merchant-leads')
@admin_required
def merchant_leads_list():
    try:
        limit = min(500, max(1, int(request.args.get('limit', 100))))
    except ValueError:
        return jsonify(ok=False, message='limit 必须为整数'), 400
    response = jsonify(ok=True, items=list_leads(limit))
    response.headers['Cache-Control'] = 'private, no-store'
    return response
