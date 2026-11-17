"""Reference-photo image generation routes for authenticated mini-program users."""
import logging

from flask import Blueprint, jsonify, request
from services.miniprogram_auth_service import require_user_by_token
from services import miniprogram_image_generation_service as service

image_generation_api = Blueprint('image_generation_api', __name__, url_prefix='/api/miniprogram/image-generations')
logger = logging.getLogger(__name__)


def _user_id():
    scheme, _, token = request.headers.get('Authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not token.strip():
        raise PermissionError('请先登录')
    return require_user_by_token(token.strip())['id']


def _respond(work, status=200):
    try:
        user_id = _user_id()
        return jsonify(ok=True, item=work(user_id)), status
    except PermissionError:
        return jsonify(ok=False, error='请先登录'), 401
    except LookupError:
        return jsonify(ok=False, error='任务不存在'), 404
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400
    except service.BusyError as error:
        return jsonify(ok=False, error=str(error)), 429
    except service.ConfigurationError as error:
        return jsonify(ok=False, error=str(error)), 503
    except Exception:
        logger.warning('Image generation API failed')
        return jsonify(ok=False, error='生图服务暂时不可用，请稍后重试'), 503


@image_generation_api.post('')
def create_image_generation():
    def create(user_id):
        if request.content_length and request.content_length > service.MAX_BYTES + 64 * 1024:
            raise ValueError('图片不能超过 10MB')
        return service.create_task(user_id, request.files.get('image'), request.form.get('prompt'), request.form.get('request_id'))
    return _respond(create, 202)


@image_generation_api.get('/<task_id>')
def get_image_generation(task_id):
    return _respond(lambda user_id: service.get_task(user_id, task_id))
