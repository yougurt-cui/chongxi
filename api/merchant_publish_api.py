"""ZIP-compatible merchant publishing endpoints using existing mini-program sessions."""
from functools import wraps
from flask import Blueprint, current_app, jsonify, request
from services.miniprogram_auth_service import require_user_by_token
from services import merchant_product_service as products

merchant_publish_api = Blueprint('merchant_publish_api', __name__)


def endpoint(*, login=True, success_status=200):
    def decorate(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            try:
                user_id = None
                if login:
                    scheme, _, token = (request.headers.get('Authorization') or '').partition(' ')
                    if scheme.lower() != 'bearer' or not token.strip():
                        raise PermissionError('请先登录')
                    user_id = require_user_by_token(token.strip())['id']
                value = handler(user_id, *args, **kwargs)
                status = success_status
                if isinstance(value, tuple):
                    value, status = value
                return jsonify({'ok': True, 'code': 0, 'message': '操作成功', 'data': value}), status
            except products.MerchantForbidden as exc:
                status, message = 403, str(exc)
            except PermissionError as exc:
                status, message = 401, str(exc)
            except LookupError as exc:
                status, message = 404, str(exc)
            except ValueError as exc:
                status, message = 400, str(exc)
            except Exception:
                current_app.logger.exception('merchant_product_operation_failed')
                status, message = 500, '操作失败，请稍后重试'
            return jsonify({'ok': False, 'code': status, 'message': message, 'detail': message}), status
        return wrapped
    return decorate


@merchant_publish_api.get('/merchant/identity')
@merchant_publish_api.get('/miniprogram/merchant/identity')
@endpoint()
def merchant_identity(user_id):
    return products.identity(user_id)


@merchant_publish_api.post('/images/upload')
@merchant_publish_api.post('/merchant/images/upload')
@merchant_publish_api.post('/miniprogram/merchant/images/upload')
@endpoint(success_status=201)
def upload_merchant_image(user_id):
    return products.upload_image(request.files.get('file') or request.files.get('image'), user_id)


@merchant_publish_api.post('/merchant/products/publish')
@merchant_publish_api.post('/miniprogram/merchant/products/publish')
@endpoint(success_status=201)
def publish_merchant_product(user_id):
    result, created = products.publish_product(user_id, request.get_json(silent=True), idempotency_key=request.headers.get('Idempotency-Key'))
    return result, 201 if created else 200


@merchant_publish_api.get('/merchant/products/mine')
@merchant_publish_api.get('/miniprogram/merchant/products/mine')
@endpoint()
def my_merchant_products(user_id):
    return products.list_my_products(user_id, page=request.args.get('page', '1'), page_size=request.args.get('page_size', '20'))


@merchant_publish_api.get('/merchant/products/<product_id>')
@merchant_publish_api.get('/miniprogram/merchant/products/<product_id>')
@endpoint(login=False)
def merchant_product_detail(user_id, product_id):
    return products.product_detail(product_id)


@merchant_publish_api.post('/merchant/products/<product_id>/bind')
@merchant_publish_api.post('/miniprogram/merchant/products/<product_id>/bind')
@endpoint()
def bind_merchant_product(user_id, product_id):
    return products.bind_product(user_id, product_id, request.get_json(silent=True))
